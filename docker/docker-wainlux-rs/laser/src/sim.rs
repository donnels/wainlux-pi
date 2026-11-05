//! Simulated machine. Runs the app and its tests without a laser.

use crate::{Done, Error, Info, Job, Machine, Progress, Rect, Result, Setting, Stop};

pub fn info() -> Info {
    Info {
        model: "Simulator",
        px_per_mm: 20.0,
        reach_mm: (80.0, 80.0),
        settings: vec![Setting { key: "power", label: "Power %", min: 0, max: 100, default: 50, note: "simulated" }],
        pointer: true,
        frame: true,
    }
}

#[derive(Default)]
pub struct Sim {
    /// Every call, in order.
    pub calls: Vec<String>,
    profile: Option<Info>,
}

impl Sim {
    pub fn with_info(profile: Info) -> Self {
        Self { calls: Vec::new(), profile: Some(profile) }
    }
}

struct NoStop;

impl Stop for NoStop {
    fn stop(&mut self) -> Result<()> {
        Ok(())
    }
}

impl Machine for Sim {
    fn info(&self) -> Info {
        self.profile.clone().unwrap_or_else(info)
    }

    fn connect(&mut self) -> Result<String> {
        self.calls.push("connect".into());
        Ok("simulator".into())
    }

    fn home(&mut self) -> Result<()> {
        self.calls.push("home".into());
        Ok(())
    }

    fn pointer(&mut self, on: bool) -> Result<()> {
        self.calls.push(format!("pointer {on}"));
        Ok(())
    }

    fn frame(&mut self, r: Rect) -> Result<()> {
        self.info().check_fits(r)?;
        self.calls.push(format!("frame {}x{} at {},{}", r.w, r.h, r.x, r.y));
        Ok(())
    }

    fn stop(&mut self) -> Result<()> {
        self.calls.push("stop".into());
        Ok(())
    }

    fn stopper(&self) -> Result<Box<dyn Stop>> {
        Ok(Box::new(NoStop))
    }

        fn burn(&mut self, job: &Job, progress: &mut dyn FnMut(Progress)) -> Result<Done> {
        let i = self.info();
        i.check_fits(job.rect(&i))?;
        let v = i.resolve(job.values)?;
        if job.raster.width == 0 || job.raster.height == 0 {
            return Err(Error::Refused("empty raster".into()));
        }
        self.calls.push(format!("burn {}x{} power {}", job.raster.width, job.raster.height, v["power"]));
            progress(Progress::whole(100));
        Ok(Done::Complete)
    }
}
