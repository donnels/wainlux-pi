//! A prepared program contains reviewed pixels and explicit machine operations.
//! It owns no input text or template logic. Once retained behind Arc, execution
//! consumes these operations without composing content again.

use laser::{done_text, Info, Job, Machine, Progress, Raster, Rect, Values};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct Burn {
    pub raster: Arc<Raster>,
    pub x_mm: f32,
    pub y_mm: f32,
}

impl Burn {
    pub fn new(raster: Raster, x_mm: f32, y_mm: f32) -> Self { Self { raster:Arc::new(raster), x_mm, y_mm } }
}

#[derive(Debug)]
pub(crate) enum Operation {
    Talk,
    Home,
    Pointer,
    Frame(Rect),
    Burn(Burn, Values),
    Calibrate(Burn, Values),
}

#[derive(Debug)]
pub(crate) struct Prepared { operations: Vec<Operation> }

impl Prepared {
    pub fn new(operations: Vec<Operation>, info: &Info) -> Result<Self, String> {
        for operation in &operations {
            if let Operation::Burn(burn, values) | Operation::Calibrate(burn, values) = operation {
                let job = Job { raster:&burn.raster, x_mm:burn.x_mm, y_mm:burn.y_mm, values };
                let rect = job.rect(info);
                if [rect.x, rect.y, rect.w, rect.h].iter().any(|value| !value.is_finite() || *value < 0.0) || rect.w == 0.0 || rect.h == 0.0 {
                    return Err("prepared geometry must be finite and positive".into());
                }
                info.resolve(values).map_err(|error| error.to_string())?;
                if matches!(operation, Operation::Burn(..)) { info.check_fits(rect).map_err(|error| error.to_string())?; }
            }
        }
        Ok(Self { operations })
    }

    pub fn preview(&self, info: &Info) -> Result<Option<Burn>, String> {
        let burns: Vec<_> = self.operations.iter().filter_map(|operation| match operation {
            Operation::Burn(burn, _) | Operation::Calibrate(burn, _) => Some(burn.clone()),
            _ => None,
        }).collect();
        if burns.is_empty() { Ok(None) } else { merge(&burns, info).map(Some) }
    }

    pub fn run(&self, machine: &mut dyn Machine, progress: &mut dyn FnMut(Progress), cancelled: &dyn Fn() -> bool) -> Result<String, String> {
        let started = Instant::now();
        let mut observations = Vec::new();
        for (index, operation) in self.operations.iter().enumerate() {
            if cancelled() { return Err("execution cancelled between operations".into()); }
            let result = match operation {
                Operation::Talk => machine.connect(),
                Operation::Home => machine.home().map(|_| "home acknowledged".into()),
                Operation::Pointer => machine.pointer(false).and_then(|_| {
                    std::thread::sleep(Duration::from_secs(3));
                    if cancelled() { return Err(laser::Error::Refused("pointer operation cancelled".into())); }
                    machine.pointer(true).map(|_| "pointer off/on acknowledged".into())
                }),
                Operation::Frame(rect) => machine.frame(*rect).and_then(|_| {
                    std::thread::sleep(Duration::from_secs(8));
                    machine.stop().map(|_| "frame acknowledged, stop sent".into())
                }),
                Operation::Burn(burn, values) | Operation::Calibrate(burn, values) => {
                    let job = Job { raster:&burn.raster, x_mm:burn.x_mm, y_mm:burn.y_mm, values };
                    let done = if matches!(operation, Operation::Calibrate(..)) { machine.calibrate_limits(&job, progress) } else { machine.burn(&job, progress) };
                    done.map(|done| format!("{}x{} px at ({:.2}, {:.2}) mm, {}: {}", burn.raster.width, burn.raster.height, burn.x_mm, burn.y_mm, machine.info().describe(values), done_text(&done)))
                }
            };
            let observation = result.map_err(|error| format!("operation {} failed after [{}]: {error}", index + 1, observations.join("; ")))?;
            if cancelled() { return Err("execution cancelled during operation".into()); }
            observations.push(observation);
        }
        Ok(format!("{} operations returned after {:.0} s: {}. Confirm physical rest before inspection.", self.operations.len(), started.elapsed().as_secs_f32(), observations.join("; ")))
    }
}

pub(crate) fn merge(burns: &[Burn], info: &Info) -> Result<Burn, String> {
    let left = burns.iter().map(|burn| info.px(burn.x_mm)).min().ok_or("nothing to compose")?;
    let top = burns.iter().map(|burn| info.px(burn.y_mm)).min().unwrap();
    let right = burns.iter().map(|burn| info.px(burn.x_mm) + burn.raster.width).max().unwrap();
    let bottom = burns.iter().map(|burn| info.px(burn.y_mm) + burn.raster.height).max().unwrap();
    let mut raster = Raster::new(right - left, bottom - top);
    for burn in burns {
        let offset_x = info.px(burn.x_mm) - left;
        let offset_y = info.px(burn.y_mm) - top;
        for row in 0..burn.raster.height { for column in 0..burn.raster.width {
            if burn.raster.get(column, row) { raster.set(offset_x + column, offset_y + row, true); }
        } }
    }
    Ok(Burn::new(raster, info.mm(left), info.mm(top)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use laser::sim::Sim;

    struct Recorder { rasters: Vec<Raster>, homes: usize, fail_on: usize }
    impl Machine for Recorder {
        fn info(&self) -> Info { laser::sim::info() }
        fn connect(&mut self) -> laser::Result<String> { Ok("recorder".into()) }
        fn home(&mut self) -> laser::Result<()> { self.homes += 1; Ok(()) }
        fn stop(&mut self) -> laser::Result<()> { Ok(()) }
        fn stopper(&self) -> laser::Result<Box<dyn laser::Stop>> { Err(laser::Error::Unsupported("recorder stop handle")) }
        fn burn(&mut self, job: &Job, progress: &mut dyn FnMut(Progress)) -> laser::Result<laser::Done> {
            self.rasters.push(job.raster.clone());
            progress(Progress::whole(100));
            if self.rasters.len() == self.fail_on { Err(laser::Error::Device("synthetic failure".into())) } else { Ok(laser::Done::Complete) }
        }
    }

    #[test]
    fn prepared_pixels_are_retained_and_failure_stops_later_operations() {
        use crate::ladder::{Params, Step};
        let mut machine = Recorder { rasters:vec![], homes:0, fail_on:usize::MAX };
        let mut params = Params { size_mm:2.0, ..Params::default() };
        let prepared = Step::Square.prepare(&params, &machine.info()).unwrap();
        let preview = prepared.preview(&machine.info()).unwrap().unwrap();
        params.size_mm = 7.0;
        prepared.run(&mut machine, &mut |_| {}, &|| false).unwrap();
        assert_eq!(machine.rasters[0], *preview.raster);
        assert_ne!(machine.rasters[0].width, machine.info().px(params.size_mm));

        let burn = Burn::new(Raster::square(20), 0.0, 0.0);
        let operations = (0..3).flat_map(|_| [Operation::Home, Operation::Burn(burn.clone(), Values::new())]).collect();
        let prepared = Prepared::new(operations, &machine.info()).unwrap();
        let mut machine = Recorder { rasters:vec![], homes:0, fail_on:2 };
        assert!(prepared.run(&mut machine, &mut |_| {}, &|| false).unwrap_err().contains("synthetic failure"));
        assert_eq!((machine.homes, machine.rasters.len()), (2, 2));

        let cancelled = std::cell::Cell::new(false);
        let prepared = Step::Square.prepare(&params, &machine.info()).unwrap();
        let mut machine = Recorder { rasters:vec![], homes:0, fail_on:usize::MAX };
        assert!(prepared.run(&mut machine, &mut |_| cancelled.set(true), &|| cancelled.get()).unwrap_err().contains("cancelled during operation"));
    }

    #[test]
    fn cancellation_prevents_all_operations_and_invalid_burns_never_prepare() {
        let mut machine = Sim::default();
        let program = Prepared::new(vec![Operation::Home], &machine.info()).unwrap();
        assert!(program.run(&mut machine, &mut |_| {}, &|| true).is_err());
        assert!(machine.calls.is_empty());
        let burn = Burn::new(Raster::square(100), 100.0, 0.0);
        assert!(Prepared::new(vec![Operation::Burn(burn, Values::new())], &machine.info()).is_err());
    }
}