//! Contract between a laser driver and its users.
//! Placement is in millimetres, top-left origin. Pixels exist only inside a Raster.
//! A driver declares what it can do and which settings it takes. Users never name a model.

mod raster;
pub mod sim;

pub use raster::Raster;

use std::collections::BTreeMap;
use std::fmt;

#[derive(Debug)]
pub enum Error {
    /// The machine cannot do this at all.
    Unsupported(&'static str),
    /// The request is wrong: outside reach, bad setting.
    Refused(String),
    /// The machine or its link failed.
    Device(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Error::Unsupported(what) => write!(f, "not supported: {what}"),
            Error::Refused(why) => write!(f, "refused: {why}"),
            Error::Device(why) => write!(f, "device: {why}"),
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

/// A driver-specific knob, such as power. Users show what the driver declares.
#[derive(Clone, Debug)]
pub struct Setting {
    pub key: &'static str,
    pub label: &'static str,
    pub min: u32,
    pub max: u32,
    pub default: u32,
    /// Where the default comes from.
    pub note: &'static str,
}

pub type Values = BTreeMap<String, u32>;

#[derive(Clone, Debug)]
pub struct Info {
    pub model: &'static str,
    pub px_per_mm: f32,
    /// What the head really reaches, not what the firmware accepts.
    pub reach_mm: (f32, f32),
    pub settings: Vec<Setting>,
    pub pointer: bool,
    pub frame: bool,
}

impl Info {
    pub fn px(&self, mm: f32) -> u32 {
        (mm * self.px_per_mm).round().max(0.0) as u32
    }

    pub fn mm(&self, px: u32) -> f32 {
        px as f32 / self.px_per_mm
    }

    /// Fill gaps with defaults. Refuse unknown keys and out-of-range values.
    pub fn resolve(&self, given: &Values) -> Result<Values> {
        if let Some(k) = given.keys().find(|k| !self.settings.iter().any(|s| s.key == k.as_str())) {
            return Err(Error::Refused(format!("{} has no setting '{k}'", self.model)));
        }
        let mut out = Values::new();
        for s in &self.settings {
            let v = given.get(s.key).copied().unwrap_or(s.default);
            if v < s.min || v > s.max {
                return Err(Error::Refused(format!("{} {v} outside {}..={}", s.label, s.min, s.max)));
            }
            out.insert(s.key.into(), v);
        }
        Ok(out)
    }

    /// Whole rectangle inside reach. Nothing is ever clipped.
    pub fn check_fits(&self, r: Rect) -> Result<()> {
        let (w, h) = self.reach_mm;
        if r.x < 0.0 || r.y < 0.0 || r.x + r.w > w || r.y + r.h > h {
            return Err(Error::Refused(format!(
                "{:.1}x{:.1} mm at ({:.1}, {:.1}) leaves the {w}x{h} mm reach",
                r.w, r.h, r.x, r.y
            )));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

/// One raster at one place. The raster is at the machine's px_per_mm.
pub struct Job<'a> {
    pub raster: &'a Raster,
    pub x_mm: f32,
    pub y_mm: f32,
    pub values: &'a Values,
}

impl Job<'_> {
    pub fn rect(&self, info: &Info) -> Rect {
        Rect { x: self.x_mm, y: self.y_mm, w: info.mm(self.raster.width), h: info.mm(self.raster.height) }
    }
}

/// How a burn was judged finished. The operator confirms which is true.
#[derive(Clone, Debug, PartialEq)]
pub enum Done {
    Complete,
    Stalled { last_pct: u8 },
    Silent { last_pct: Option<u8> },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProgressPhase { Starting, Waiting, ReportedComplete, AssumedComplete }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Progress {
    pub phase: ProgressPhase,
    pub percent: Option<u8>,
    pub band: Option<u16>,
    pub bands: Option<u16>,
    pub row_start: Option<u32>,
    pub row_end: Option<u32>,
    pub previous_assumed: bool,
    pub transfer_ms: Option<u64>,
    pub setup_ms: Option<u64>,
    pub wait_ms: Option<u64>,
    pub elapsed_ms: Option<u64>,
}

impl Progress {
    pub fn whole(percent: u8) -> Self {
        Progress { phase: if percent >= 100 { ProgressPhase::ReportedComplete } else { ProgressPhase::Waiting }, percent: Some(percent), band: None, bands: None, row_start: None, row_end: None, previous_assumed: false, transfer_ms:None, setup_ms:None, wait_ms:None, elapsed_ms:None }
    }

    pub fn band(phase: ProgressPhase, band: usize, bands: usize, row_start: u32, row_end: u32, percent: Option<u8>, previous_assumed: bool) -> Self {
        Progress { phase, percent, band: Some(band as u16), bands: Some(bands as u16), row_start: Some(row_start), row_end: Some(row_end), previous_assumed, transfer_ms:None, setup_ms:None, wait_ms:None, elapsed_ms:None }
    }

    pub fn timings(mut self, transfer_ms: u64, setup_ms: u64, wait_ms: u64, elapsed_ms: u64) -> Self {
        self.transfer_ms=Some(transfer_ms); self.setup_ms=Some(setup_ms); self.wait_ms=Some(wait_ms); self.elapsed_ms=Some(elapsed_ms); self
    }
}

/// Stops the machine while another thread is inside burn().
pub trait Stop: Send {
    fn stop(&mut self) -> Result<()>;
}

pub trait Machine: Send {
    fn info(&self) -> Info;
    /// Prove the machine answers. Returns its identity. No motion.
    fn connect(&mut self) -> Result<String>;
    fn home(&mut self) -> Result<()>;
    /// Low-power aiming dot.
    fn pointer(&mut self, _on: bool) -> Result<()> {
        Err(Error::Unsupported("pointer"))
    }
    /// Laser-off trace of a rectangle. Runs until stop().
    fn frame(&mut self, _r: Rect) -> Result<()> {
        Err(Error::Unsupported("frame"))
    }
    fn stop(&mut self) -> Result<()>;
    fn stopper(&self) -> Result<Box<dyn Stop>>;
    /// Upload and burn. Blocks until done.
    fn burn(&mut self, job: &Job, progress: &mut dyn FnMut(Progress)) -> Result<Done>;
    fn calibrate_limits(&mut self, _job: &Job, _progress: &mut dyn FnMut(Progress)) -> Result<Done> {
        Err(Error::Unsupported("limit calibration"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn info() -> Info {
        sim::info()
    }

    #[test]
    fn resolve_fills_defaults_and_checks_range() {
        let v = info().resolve(&Values::new()).unwrap();
        assert_eq!(v["power"], 50);
        let bad: Values = [("power".to_string(), 101)].into();
        assert!(info().resolve(&bad).is_err());
        let unknown: Values = [("depth".to_string(), 1)].into();
        assert!(info().resolve(&unknown).is_err());
    }

    #[test]
    fn fits_inside_reach_only() {
        let i = info();
        assert!(i.check_fits(Rect { x: 0.0, y: 0.0, w: 20.0, h: 20.0 }).is_ok());
        assert!(i.check_fits(Rect { x: 70.0, y: 0.0, w: 20.0, h: 20.0 }).is_err());
        assert!(i.check_fits(Rect { x: -1.0, y: 0.0, w: 20.0, h: 20.0 }).is_err());
    }
}
