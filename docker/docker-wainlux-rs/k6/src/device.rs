//! Device sequences over a serial port. Timings follow the first working burn.

use crate::protocol::{self as p, ACK, HEARTBEAT, NACK};
use laser::{Done, Job, Machine, Progress, ProgressPhase, Raster, Rect};
use std::io::{self, Write};
use std::sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}};
use std::thread::sleep;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub enum Error {
    Io(io::Error),
    Timeout(&'static str),
    NoAck(&'static str, Vec<u8>),
    Rejected { offset: usize, rx: Vec<u8> },
    TooBig,
    Cancelled,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io: {e}"),
            Error::Timeout(c) => write!(f, "timeout on {c}"),
            Error::NoAck(c, rx) => write!(f, "no ACK on {c}: {rx:02x?}"),
            Error::Rejected { offset, rx } => write!(f, "DATA rejected at byte {offset}: {rx:02x?}"),
            Error::TooBig => write!(f, "job outside the bed"),
            Error::Cancelled => write!(f, "execution cancelled; confirm the device has stopped"),
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// Byte pipe to the laser. Serial in production, scripted in tests.
pub trait Port: Send {
    fn send(&mut self, b: &[u8]) -> io::Result<()>;
    /// One byte, or None after `wait`.
    fn recv(&mut self, wait: Duration) -> io::Result<Option<u8>>;
    /// Second write handle, so STOP works while a burn holds the device.
    fn stopper(&self) -> io::Result<Box<dyn Write + Send>>;
}

impl Port for Box<dyn serialport::SerialPort> {
    fn send(&mut self, b: &[u8]) -> io::Result<()> {
        self.write_all(b)?;
        self.flush()
    }

    fn recv(&mut self, wait: Duration) -> io::Result<Option<u8>> {
        self.set_timeout(wait)?;
        let mut b = [0u8; 1];
        match self.read(&mut b) {
            Ok(1) => Ok(Some(b[0])),
            Ok(_) => Ok(None),
            Err(e) if e.kind() == io::ErrorKind::TimedOut => Ok(None),
            Err(e) => Err(e),
        }
    }

    fn stopper(&self) -> io::Result<Box<dyn Write + Send>> {
        Ok(Box::new(self.try_clone()?))
    }
}

pub struct Device<P: Port> {
    port: P,
    cancelled: Arc<AtomicBool>,
    write_gate: Arc<Mutex<()>>,
}

/// Bed size the firmware accepts, in pixels (0.05 mm). Mechanics reach less.
pub const BED: u16 = 1600;
const SILENT: Duration = Duration::from_secs(90);

impl<P: Port> Device<P> {
    pub fn new(port: P) -> Self {
        Device { port, cancelled: Arc::new(AtomicBool::new(false)), write_gate: Arc::new(Mutex::new(())) }
    }

    fn check_cancelled(&self) -> Result<()> {
        if self.cancelled.load(Ordering::SeqCst) { Err(Error::Cancelled) } else { Ok(()) }
    }

    fn send(&mut self, packet: &[u8]) -> Result<()> {
        let _guard = self.write_gate.lock().unwrap();
        self.check_cancelled()?;
        Ok(self.port.send(packet)?)
    }

    pub fn stopper(&self) -> Result<Box<dyn Write + Send>> {
        Ok(self.port.stopper()?)
    }

    /// Send and collect until ACK (or any frame if `ack` is false) or timeout.
    fn cmd(&mut self, name: &'static str, pkt: &[u8], wait: Duration, ack: bool) -> Result<Vec<u8>> {
        self.send(pkt)?;
        let end = Instant::now() + wait;
        let mut rx = Vec::new();
        while Instant::now() < end {
            self.check_cancelled()?;
            let Some(b) = self.port.recv(Duration::from_millis(50))? else { continue };
            rx.push(b);
            if ack && (b == ACK || b == NACK) {
                break;
            }
            if !ack && is_frame(&rx) {
                break;
            }
        }
        self.check_cancelled()?;
        match (ack, rx.last()) {
            (_, None) => Err(Error::Timeout(name)),
            (true, Some(&ACK)) => Ok(rx),
            (true, _) => Err(Error::NoAck(name, rx)),
            (false, _) => Ok(rx),
        }
    }

    fn drain(&mut self) -> Result<()> {
        loop {
            self.check_cancelled()?;
            if self.port.recv(Duration::from_millis(50))?.is_none() { break; }
        }
        Ok(())
    }

    /// STOP, then VERSION. Proves the laser talks. No motion.
    pub fn version(&mut self) -> Result<(u8, u8, u8)> {
        self.send(&p::STOP)?;
        sleep(Duration::from_millis(200));
        self.drain()?;
        self.send(&p::VERSION)?;
        let mut v = Vec::new();
        let end = Instant::now() + Duration::from_secs(1);
        while v.len() < 3 && Instant::now() < end {
            self.check_cancelled()?;
            if let Some(b) = self.port.recv(Duration::from_millis(50))? {
                v.push(b);
            }
        }
        if v.len() < 3 {
            return Err(Error::Timeout("VERSION"));
        }
        Ok((v[0], v[1], v[2]))
    }

    pub fn connect(&mut self) -> Result<()> {
        self.cmd("CONNECT", &p::CONNECT, Duration::from_secs(1), true)?;
        self.cmd("CONNECT", &p::CONNECT, Duration::from_secs(1), true)?;
        Ok(())
    }

    pub fn home(&mut self) -> Result<()> {
        self.cmd("HOME", &p::HOME, Duration::from_secs(10), true).map(|_| ())
    }

    pub fn stop(&mut self) -> Result<()> {
        self.send(&p::STOP)?;
        sleep(Duration::from_millis(200));
        self.drain()
    }

    /// Laser-off preview of a box with top-left (x, y). Traces until stop().
    pub fn frame(&mut self, x: u16, y: u16, w: u16, h: u16) -> Result<()> {
        let (cx, cy) = p::centre(x, y, w, h);
        self.cmd("BOUNDS", &p::bounds_packet(w, h, cx, cy), Duration::from_secs(2), true).map(|_| ())
    }

    /// Upload and burn. Top-left of the raster at (x, y). Blocks until done.
    pub fn burn(
        &mut self,
        r: &Raster,
        x: u16,
        y: u16,
        power: u16,
        depth: u16,
        progress: &mut dyn FnMut(Progress),
        band: (usize, usize, u32, u32, bool),
    ) -> Result<Done> {
        let band_started = Instant::now();
        let (w, h) = match (u16::try_from(r.width), u16::try_from(r.height)) {
            (Ok(w), Ok(h)) if x as u32 + w as u32 <= BED as u32 && y as u32 + h as u32 <= BED as u32 => (w, h),
            _ => return Err(Error::TooBig),
        };
        let data = p::pack(r);
        let (cx, cy) = p::centre(x, y, w, h);

        self.cmd("FRAMING", &p::FRAMING, Duration::from_secs(1), true)?;
        let hdr = p::job_header(w, h, power, depth, cx, cy);
        self.cmd("JOB_HEADER", &hdr, Duration::from_secs(10), false)?;
        sleep(Duration::from_millis(40 * (data.len() as u64 / 4094 + 1)));
        self.cmd("CONNECT", &p::CONNECT, Duration::from_secs(1), true)?;
        sleep(Duration::from_millis(500));
        self.cmd("CONNECT", &p::CONNECT, Duration::from_secs(1), true)?;
        sleep(Duration::from_millis(500));

        let transfer_started = Instant::now();
        for (i, chunk) in data.chunks(p::DATA_CHUNK).enumerate() {
            self.send_chunk(chunk, i * p::DATA_CHUNK)?;
        }
        let transfer_ms = transfer_started.elapsed().as_millis() as u64;

        sleep(Duration::from_millis(200));
        self.cmd("INIT", &p::INIT, Duration::from_secs(3), false)?;
        sleep(Duration::from_millis(500));
        self.cmd("INIT", &p::INIT, Duration::from_secs(3), false)?;
        let setup_ms = (band_started.elapsed().as_millis() as u64).saturating_sub(transfer_ms);
        self.wait_done(progress, band, band_started, transfer_ms, setup_ms)
    }

    fn send_chunk(&mut self, chunk: &[u8], offset: usize) -> Result<()> {
        let pkt = p::data_packet(chunk);
        let mut last = Vec::new();
        for attempt in 0..3u64 {
            match self.cmd("DATA", &pkt, Duration::from_secs(2), true) {
                Ok(_) => return Ok(()),
                Err(Error::NoAck(_, rx)) => last = rx,
                Err(Error::Timeout(_)) => {}
                Err(e) => return Err(e),
            }
            sleep(Duration::from_millis(100 << attempt));
        }
        Err(Error::Rejected { offset, rx: last })
    }

    /// Watch status frames FF FF 00 <pct>. The device goes quiet near the end.
    fn wait_done(&mut self, progress: &mut dyn FnMut(Progress), band: (usize, usize, u32, u32, bool), band_started: Instant, transfer_ms: u64, setup_ms: u64) -> Result<Done> {
        let mut buf: Vec<u8> = Vec::new();
        let mut last_pct: Option<u8> = None;
        let mut last_rx = Instant::now();
        let wait_started = Instant::now();
        progress(Progress::band(ProgressPhase::Waiting, band.0, band.1, band.2, band.3, None, band.4).timings(transfer_ms, setup_ms, 0, band_started.elapsed().as_millis() as u64));
        loop {
            self.check_cancelled()?;
            if let Some(b) = self.port.recv(Duration::from_millis(200))? {
                last_rx = Instant::now();
                buf.push(b);
                if buf.len() > 8 {
                    buf.remove(0);
                }
                if let [.., 0xFF, 0xFF, 0x00, pct] = buf[..] {
                    if last_pct != Some(pct) {
                        last_pct = Some(pct);
                        progress(Progress::band(if pct >= 100 { ProgressPhase::ReportedComplete } else { ProgressPhase::Waiting }, band.0, band.1, band.2, band.3, Some(pct), band.4).timings(transfer_ms, setup_ms, wait_started.elapsed().as_millis() as u64, band_started.elapsed().as_millis() as u64));
                    }
                    self.check_cancelled()?;
                    if pct >= 100 {
                        return Ok(Done::Complete);
                    }
                }
                continue;
            }
            if let Some(done) = silence_completion(last_pct, last_rx.elapsed()) {
                if matches!(done, Done::Silent { .. }) { progress(Progress::band(ProgressPhase::AssumedComplete, band.0, band.1, band.2, band.3, last_pct, band.4).timings(transfer_ms, setup_ms, wait_started.elapsed().as_millis() as u64, band_started.elapsed().as_millis() as u64)); }
                return Ok(done);
            }
        }
    }
}

fn silence_completion(last_pct: Option<u8>, quiet: Duration) -> Option<Done> {
    (quiet >= SILENT).then_some(Done::Silent { last_pct })
}

fn is_frame(rx: &[u8]) -> bool {
    rx.ends_with(&HEARTBEAT) || matches!(rx, [.., 0xFF, 0xFF, 0x00, _])
}

impl From<Error> for laser::Error {
    fn from(e: Error) -> Self {
        match e {
            Error::TooBig => laser::Error::Refused(e.to_string()),
            _ => laser::Error::Device(e.to_string()),
        }
    }
}

/// STOP over the second write handle.
struct Stopper {
    writer: Box<dyn Write + Send>,
    cancelled: Arc<AtomicBool>,
    write_gate: Arc<Mutex<()>>,
}

impl laser::Stop for Stopper {
    fn stop(&mut self) -> laser::Result<()> {
        self.cancelled.store(true, Ordering::SeqCst);
        let _guard = self.write_gate.lock().unwrap();
        self.writer.write_all(&p::STOP).and_then(|_| self.writer.flush()).map_err(|e| laser::Error::Device(e.to_string()))
    }
}

fn u16_px(info: &laser::Info, mm: f32) -> laser::Result<u16> {
    u16::try_from(info.px(mm)).map_err(|_| laser::Error::Refused(format!("{mm} mm is off the bed")))
}

impl<P: Port + 'static> Machine for Device<P> {
    fn info(&self) -> laser::Info {
        crate::info()
    }

    fn connect(&mut self) -> laser::Result<String> {
        let v = self.version()?;
        Device::connect(self)?;
        Ok(format!("K6 firmware v{}.{}.{}", v.0, v.1, v.2))
    }

    fn home(&mut self) -> laser::Result<()> {
        Ok(Device::home(self)?)
    }

    fn frame(&mut self, r: Rect) -> laser::Result<()> {
        let i = crate::info();
        i.check_fits(r)?;
        Ok(Device::frame(self, u16_px(&i, r.x)?, u16_px(&i, r.y)?, u16_px(&i, r.w)?, u16_px(&i, r.h)?)?)
    }

    fn stop(&mut self) -> laser::Result<()> {
        // STOP alone does not end a BOUNDS preview; FRAMING then HOME does.
        Device::stop(self)?;
        self.cmd("FRAMING", &p::FRAMING, Duration::from_secs(2), true)?;
        Ok(Device::home(self)?)
    }

    fn stopper(&self) -> laser::Result<Box<dyn laser::Stop>> {
        Ok(Box::new(Stopper { writer: Device::stopper(self)?, cancelled: self.cancelled.clone(), write_gate: self.write_gate.clone() }))
    }

    fn burn(&mut self, job: &Job, progress: &mut dyn FnMut(Progress)) -> laser::Result<Done> {
        let i = crate::info();
        i.check_fits(job.rect(&i))?;
        let v = i.resolve(job.values)?;
        let (x, y) = (u16_px(&i, job.x_mm)?, u16_px(&i, job.y_mm)?);
        let bands = p::bands(job.raster);
        if bands.is_empty() {
            return Err(laser::Error::Refused("nothing to burn: the raster is blank".into()));
        }
        let mut done = Done::Complete;
        let mut previous_assumed = false;
        for (index, (row, band)) in bands.iter().enumerate() {
            self.check_cancelled()?;
            if p::has_zero_chunk(&p::pack(band)) {
                return Err(laser::Error::Refused("an empty stretch fills a whole DATA chunk; the K6 would reject it".into()));
            }
            let row_end = *row + band.height;
            progress(Progress::band(ProgressPhase::Starting, index + 1, bands.len(), *row, row_end, None, previous_assumed));
            let band_done = Device::burn(self, band, x, y + *row as u16, v["power"] as u16, v["depth"] as u16, progress, (index + 1, bands.len(), *row, row_end, previous_assumed))?;
            self.check_cancelled()?;
            previous_assumed = matches!(band_done, Done::Silent { .. });
            if band_done != Done::Complete || done == Done::Complete { done = band_done; }
        }
        Ok(done)
    }

    fn calibrate_limits(&mut self, job: &Job, progress: &mut dyn FnMut(Progress)) -> laser::Result<Done> {
        let info = crate::info();
        let rect = job.rect(&info);
        if [rect.x, rect.y, rect.w, rect.h].iter().any(|value| !value.is_finite() || *value < 0.0)
            || rect.w == 0.0 || rect.h == 0.0
            || rect.x + rect.w > info.mm(BED as u32)
            || rect.y + rect.h > info.mm(BED as u32)
            || job.raster.bits.len() != job.raster.width as usize * job.raster.height as usize
        {
            return Err(laser::Error::Refused("calibration must stay inside the firmware envelope".into()));
        }
        let values = info.resolve(job.values)?;
        if p::has_zero_chunk(&p::pack(job.raster)) {
            return Err(laser::Error::Refused("calibration contains an all-zero DATA chunk".into()));
        }
        progress(Progress::band(ProgressPhase::Starting, 1, 1, 0, job.raster.height, None, false));
        Ok(Device::burn(self, job.raster, u16_px(&info, rect.x)?, u16_px(&info, rect.y)?,
            values["power"] as u16, values["depth"] as u16, progress, (1, 1, 0, job.raster.height, false))?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    /// Answers each write with the next scripted reply.
    struct Script {
        replies: VecDeque<Vec<u8>>,
        pending: VecDeque<u8>,
        sent: Vec<Vec<u8>>,
        cancel_on: Option<(u8, Arc<AtomicBool>)>,
    }

    impl Port for Script {
        fn send(&mut self, b: &[u8]) -> io::Result<()> {
            self.sent.push(b.to_vec());
            if let Some((opcode, cancelled)) = &self.cancel_on {
                if b.first() == Some(opcode) { cancelled.store(true, Ordering::SeqCst); }
            }
            if let Some(r) = self.replies.pop_front() {
                self.pending.extend(r);
            }
            Ok(())
        }
        fn recv(&mut self, _: Duration) -> io::Result<Option<u8>> {
            Ok(self.pending.pop_front())
        }
        fn stopper(&self) -> io::Result<Box<dyn Write + Send>> {
            Ok(Box::new(io::sink()))
        }
    }

    fn script(replies: &[&[u8]]) -> Device<Script> {
        Device::new(Script {
            replies: replies.iter().map(|r| r.to_vec()).collect(),
            pending: VecDeque::new(),
            sent: Vec::new(),
            cancel_on: None,
        })
    }

    #[test]
    fn version_reads_three_bytes() {
        let mut d = script(&[&[], &[9, 4, 1]]);
        assert_eq!(d.version().unwrap(), (9, 4, 1));
    }

    #[test]
    fn nack_is_an_error() {
        let mut d = script(&[&[NACK]]);
        assert!(matches!(d.home(), Err(Error::NoAck("HOME", _))));
    }

    #[test]
    fn assumed_completion_requires_90_seconds_at_every_percentage() {
        for last_pct in [None, Some(0), Some(65), Some(90), Some(99)] {
            assert_eq!(silence_completion(last_pct, Duration::from_secs(20)), None);
            assert_eq!(silence_completion(last_pct, Duration::from_millis(89_999)), None);
            assert_eq!(silence_completion(last_pct, Duration::from_secs(90)), Some(Done::Silent { last_pct }));
            assert_eq!(silence_completion(last_pct, Duration::from_secs(91)), Some(Done::Silent { last_pct }));
        }
    }

    #[test]
    fn stop_before_connect_prevents_further_commands() {
        let mut device = script(&[]);
        let mut stopper = Machine::stopper(&device).unwrap();
        stopper.stop().unwrap();
        assert!(matches!(device.version(), Err(Error::Cancelled)));
        assert!(matches!(device.home(), Err(Error::Cancelled)));
        assert!(device.port.sent.is_empty());
    }

    #[test]
    fn cancellation_during_upload_prevents_init() {
        let mut device = script(&[&[ACK], &HEARTBEAT, &[ACK], &[ACK], &[ACK]]);
        device.port.cancel_on = Some((0x22, device.cancelled.clone()));
        assert!(matches!(device.burn(&Raster::square(8), 0, 0, 500, 10, &mut |_| {}, (1,1,0,8,false)), Err(Error::Cancelled)));
        assert!(!device.port.sent.iter().any(|packet| packet[0] == 0x24));
    }

    #[test]
    fn stop_during_progress_prevents_remaining_bands_and_home() {
        let done = [0xFF, 0xFF, 0x00, 50, 0xFF, 0xFF, 0x00, 100];
        let one: [&[u8]; 7] = [&[ACK], &HEARTBEAT, &[ACK], &[ACK], &[ACK], &HEARTBEAT, &done];
        let mut device = script(&[one, one].concat());
        let mut raster = Raster::new(16, 502);
        raster.set(0, 0, true); raster.set(0, 501, true);
        let values = laser::Values::new();
        let job = Job { raster: &raster, x_mm: 0.0, y_mm: 0.0, values: &values };
        let mut stopper = Machine::stopper(&device).unwrap();
        assert!(Machine::burn(&mut device, &job, &mut |event| {
            if event.percent.is_some() { stopper.stop().unwrap(); }
        }).is_err());
        assert_eq!(device.port.sent.iter().filter(|packet| packet[0] == 0x23).count(), 1);
        assert!(matches!(device.home(), Err(Error::Cancelled)));
    }

    #[test]
    fn burn_sends_sequence_and_finishes_on_100() {
        let done = [0xFF, 0xFF, 0x00, 50, 0xFF, 0xFF, 0x00, 100];
        let mut d = script(&[&[ACK], &HEARTBEAT, &[ACK], &[ACK], &[ACK], &HEARTBEAT, &done]);
        let mut seen = Vec::new();
        let r = Raster::square(8);
        let out = d.burn(&r, 0, 0, 500, 10, &mut |p| seen.push(p), (1,1,0,8,false)).unwrap();
        assert!(matches!(out, Done::Complete));
        assert!(seen.iter().any(|event| event.phase == ProgressPhase::Waiting && event.band == Some(1) && event.bands == Some(1)));
        assert!(seen.iter().any(|event| event.phase == ProgressPhase::ReportedComplete && event.percent == Some(100)));
        let completed = seen.iter().find(|event| event.phase == ProgressPhase::ReportedComplete).unwrap();
        assert!(completed.transfer_ms.is_some() && completed.setup_ms.is_some() && completed.wait_ms.is_some() && completed.elapsed_ms.is_some());
        let ops: Vec<u8> = d.port.sent.iter().map(|s| s[0]).collect();
        assert_eq!(ops, vec![0x21, 0x23, 0x0A, 0x0A, 0x22, 0x24, 0x24]);
    }

    #[test]
    fn refuses_jobs_off_the_bed() {
        let mut d = script(&[]);
        let r = Raster::square(10);
        assert!(matches!(d.burn(&r, BED - 5, 0, 500, 10, &mut |_| {}, (1,1,0,10,false)), Err(Error::TooBig)));
    }

    #[test]
    fn contract_refuses_outside_reach_before_sending() {
        let mut d = script(&[]);
        let r = Raster::square(400);
        let v = laser::Values::new();
        let job = Job { raster: &r, x_mm: 0.0, y_mm: 60.0, values: &v };
        assert!(matches!(Machine::burn(&mut d, &job, &mut |_| {}), Err(laser::Error::Refused(_))));
        assert!(d.port.sent.is_empty());
    }

    #[test]
    fn calibration_can_probe_past_reach_but_not_past_firmware_bounds() {
        let done = [0xFF, 0xFF, 0x00, 50, 0xFF, 0xFF, 0x00, 100];
        let mut device = script(&[&[ACK], &HEARTBEAT, &[ACK], &[ACK], &[ACK], &HEARTBEAT, &done]);
        let raster = Raster::filled(8, 120);
        let values = laser::Values::new();
        let mut job = Job { raster: &raster, x_mm: 0.0, y_mm: 74.0, values: &values };
        assert!(Machine::burn(&mut device, &job, &mut |_| {}).is_err());
        assert!(device.port.sent.is_empty());
        assert_eq!(Machine::calibrate_limits(&mut device, &job, &mut |_| {}).unwrap(), Done::Complete);
        let mut invalid_device = script(&[]);
        job.y_mm = 74.05;
        assert!(Machine::calibrate_limits(&mut invalid_device, &job, &mut |_| {}).is_err());
        assert!(invalid_device.port.sent.is_empty());
    }

    #[test]
    fn calibration_zero_line_prevents_empty_data_chunks() {
        let mut raster = Raster::new(1600, 120);
        for row in 0..120 {
            raster.set(0, row, true);
            for column in row * 13..(row + 1) * 13 {
                raster.set(column, row, true);
            }
        }
        assert!(!p::has_zero_chunk(&p::pack(&raster)));
        assert_eq!(p::bands(&raster).len(), 1);
    }

    #[test]
    fn contract_burns_each_band_as_its_own_job() {
        // INIT's own read takes the first status frame; wait_done needs the second.
        let done = [0xFF, 0xFF, 0x00, 50, 0xFF, 0xFF, 0x00, 100];
        let one: [&[u8]; 7] = [&[ACK], &HEARTBEAT, &[ACK], &[ACK], &[ACK], &HEARTBEAT, &done];
        let mut d = script(&[one, one].concat());
        let mut r = Raster::new(16, 502);
        r.set(0, 0, true);
        r.set(0, 501, true);
        let v = laser::Values::new();
        let job = Job { raster: &r, x_mm: 0.0, y_mm: 0.0, values: &v };
        let mut progress = Vec::new();
        assert_eq!(Machine::burn(&mut d, &job, &mut |event| progress.push(event)).unwrap(), Done::Complete);
        let starts: Vec<_> = progress.iter().filter(|event| event.phase == ProgressPhase::Starting).collect();
        assert_eq!(starts.iter().map(|event| (event.band,event.bands,event.row_start,event.row_end)).collect::<Vec<_>>(), [(Some(1),Some(2),Some(0),Some(1)),(Some(2),Some(2),Some(501),Some(502))]);
        let completed: Vec<_> = progress.iter().filter(|event| event.phase == ProgressPhase::ReportedComplete).collect();
        assert_eq!(completed.len(),2);
        assert!(completed.iter().all(|event|event.transfer_ms.is_some() && event.setup_ms.is_some() && event.wait_ms.is_some()));
        let headers: Vec<&Vec<u8>> = d.port.sent.iter().filter(|s| s[0] == 0x23).collect();
        assert_eq!(headers.len(), 2);
        assert_eq!(u16::from_be_bytes([headers[1][34], headers[1][35]]), 501, "second band one row tall at row 501");
    }
}
