//! Test ladder. Each step states its goal before it runs.
//! The operator answers yes/no/comment after. Both land in the log.
//! Knows only the `laser` contract, never a model.

use compose::qr::qr;
use laser::{Info, Machine, Progress, Raster, Rect, Values};
use crate::prepared::{Burn, Operation, Prepared};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Step {
    Talk,
    Home,
    Pointer,
    Frame,
    Square,
    Limits,
    LimitSquares,
    BlankRows,
    Corners,
    CornersContinuous,
    VerticalComparison,
    VerticalComparisonD,
    Registration,
    Qr,
    PowerSweep,
    AudioPowerSweepLow,
    AudioPowerSweepFull,
}

const POWER_SWEEP_COUNT: u8 = 10;
const POWER_SWEEP_PITCH_MM: f32 = 2.0;
const POWER_REFERENCE_SIZE_MM: f32 = 20.0;
const POWER_SWEEP_X_INSET_MM: f32 = 0.5;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Params {
    pub x_mm: f32,
    pub y_mm: f32,
    pub size_mm: f32,
    pub text: String,
    /// Driver settings by key. Missing keys take the driver's defaults.
    pub values: Values,
    #[serde(skip)]
    pub power_values: Vec<u32>,
    pub limit_start_mm: f32,
    pub limit_end_mm: f32,
    pub limit_width_mm: f32,
    #[serde(default = "default_border")]
    pub limit_border_mm: f32,
}

fn default_border() -> f32 { 77.45 }

impl Default for Params {
    fn default() -> Self {
        Params { x_mm: 0.0, y_mm: 0.0, size_mm: 19.0, text: "https://github.com/donnels".into(), values: Values::new(), power_values: Vec::new(), limit_start_mm: 74.0, limit_end_mm: 80.0, limit_width_mm: 80.0, limit_border_mm: default_border() }
    }
}

impl Step {
    fn check_size(self, p: &Params, info: &Info) -> Result<(), String> {
        if matches!(self, Step::Frame | Step::Square | Step::Qr | Step::BlankRows) {
            if [p.x_mm, p.y_mm, p.size_mm].iter().any(|value| !value.is_finite()) || p.size_mm <= 0.0 {
                return Err("test position and size must be finite; size must be positive".into());
            }
            let scale = if self == Step::BlankRows { 3 } else { 1 };
            let extent = p.size_mm * scale as f32;
            info.check_fits(Rect { x: p.x_mm, y: p.y_mm, w: extent, h: extent }).map_err(|error| error.to_string())?;
            let side = info.px(p.size_mm);
            if side == 0 { return Err("test size is smaller than one pixel".into()); }
            let extent = info.mm(side * scale);
            info.check_fits(Rect { x: p.x_mm, y: p.y_mm, w: extent, h: extent }).map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    pub fn supported(self, i: &Info) -> bool {
        match self {
            Step::Pointer => i.pointer,
            Step::Frame => i.frame,
            Step::VerticalComparison => i.reach_mm.0 >= 32.0 && i.reach_mm.1 >= 77.45,
            Step::PowerSweep | Step::AudioPowerSweepLow | Step::AudioPowerSweepFull => i.settings.iter().any(|setting| setting.key == "power" && setting.min == 0 && setting.max > 0),
            _ => true,
        }
    }

    /// What should happen, in the operator's terms. Stated before running.
    pub fn goal(self, p: &Params, i: &Info) -> Result<String, String> {
        if !self.supported(i) {
            return Err(format!("{} cannot execute {:?}", i.model, self));
        }
        self.check_size(p, i)?;
        let mut values = p.values.clone();
        if matches!(self, Step::PowerSweep | Step::AudioPowerSweepLow | Step::AudioPowerSweepFull) {
            let power = p.power_values.first().copied().ok_or("audio test has no configured power value")?;
            values.insert("power".into(), power);
        }
        let v = i.resolve(&values).map_err(|e| e.to_string())?;
        let settings = i.describe(&v);
        let at = format!("top-left at {} mm, {} mm", p.x_mm, p.y_mm);
        if self == Step::PowerSweep {
            let jobs = sweep_jobs(p, i)?;
            let (frame, cases) = jobs.split_first().unwrap();
            let powers = cases.iter().map(|case| case.power.to_string()).collect::<Vec<_>>().join(", ");
            let (frame_x, frame_y, frame_power) = (frame.x_mm, frame.y_mm, frame.power);
            return Ok(format!("HOME first. One run starts with a {:.1} x {:.1} mm full-power frame at ({frame_x:.1}, {frame_y:.1}) mm, power {frame_power} ({settings}). Its top edge replaces the duplicate full-power line. Then submit {} separately initialized {:.2} mm one-row lines, starting at X={:.2} mm on a 2 mm vertical pitch. Commanded powers: {powers}. Keep unit and stock in place. Do not inspect or intervene between jobs; a device/software error stops the remaining submissions. After physical rest, inspect and judge the full pattern. Power 0 is not assumed laser-off; every mark may be active.", i.mm(frame.raster.width), i.mm(frame.raster.height), cases.len(), i.mm(cases[0].raster.width), cases[0].x_mm));
        }
        if matches!(self, Step::AudioPowerSweepLow | Step::AudioPowerSweepFull) {
            let jobs = sweep_jobs(p, i)?;
            let line_mm = i.mm(jobs[1].raster.width);
            let full_power = self == Step::AudioPowerSweepFull;
            let power = p.power_values.first().copied().ok_or("audio test has no configured power value")?;
            let profile = if full_power { "full power; marking expected" } else { "minimum nonzero power; nominally no-mark, not guaranteed" };
            return Ok(format!("HOME first. One audio-recorded {profile} comparison run at power {power}/{}, using the same fixed power for the 20 mm box and nine sequential {line_mm} mm one-row lines. Lines start at X={:.1} mm, Y=2, 4, ..., 18 mm. Keep stock in place, inspect only after all jobs physically stop, and compare the recording/log with the other profile. A device/software error stops remaining jobs. {settings}.", i.settings.iter().find(|setting| setting.key == "power").map(|setting| setting.max).unwrap_or(power), jobs[1].x_mm));
        }
        if self == Step::VerticalComparisonD {
            vertical_d(i)?;
            return Ok(format!("HOME first. D only on stock base at X=40 mm, Y=0: a continuous 75 mm line with four filled 2 mm squares. Inner marker edges align with A/B/C at Y=28.70 and 48.70 mm. {settings}. Expected total height 75 mm, top outer span 28.70 mm, middle inner span 20 mm, middle outer span 24 mm, bottom outer span 26.30 mm. Compare D with C after motion stops; do not reposition between their burns. No working-limit change."));
        }
        if self == Step::VerticalComparison {
            vertical_strips(i)?;
            return Ok(format!("HOME before A, B and C. Three separate jobs on the stock base, no inspection or repositioning until C stops. A at X=10 mm: 77.45 mm tall with a 20 mm gap. B at X=20 mm: 77.45 mm tall with a continuous line. C at X=30 mm: 75 mm tall with a 20 mm gap. All start at Y=0, use four 2 mm squares, and align inner marker edges at Y=28.70 and 48.70 mm. {settings}. Expected: A/B total height 77.45 mm, C 75 mm; top marker span 28.70 mm, middle outer span 24 mm, bottom span 28.75 mm on A/B or 26.30 mm on C. Compare bottom-square compression. The test height does not change working limits."));
        }
        if self == Step::Registration {
            registration(p, i)?;
            return Ok(format!("Two passes, one session, no repositioning or inspection between. HOME before each pass. A: 2 mm filled square at ({}, {}) mm with a 3 mm horizontal witness line. B: same square and witness at the same requested coordinates, plus a vertical line from Y=0 to the square. {settings}. Goal: one deeper square and one witness line; two witness lines or displaced edges reveal drift. Stay away from the mechanical stop.", p.x_mm, p.y_mm));
        }
        if self == Step::Corners || self == Step::CornersContinuous {
            if p.x_mm != 0.0 || p.y_mm != 0.0 {
                return Err("the working-area corner test must start at X=0, Y=0".into());
            }
            if self == Step::CornersContinuous {
                continuous_corners(i)?;
                return Ok(format!("HOME first. Burn a continuous one-row {:.2} x {:.2} mm perimeter with four filled 2 mm corner squares and no gaps. {settings}. Every row has ink: one K6 upload/burn cycle. Inspect only after all motion stops. Goal: four intact corner squares; compare bottom placement with the gapped test before changing working limits.", i.reach_mm.0, i.reach_mm.1));
            }
            corners(i)?;
            return Ok(format!("HOME first. Burn a one-row perimeter of the {:.2} x {:.2} mm working area, four filled 2 mm corner squares inside it, and a 20 mm centred gap on each side. {settings}. One planned attempt; the K6 uses two upload/burn cycles for the blank middle rows. Do not move or inspect between cycles. Goal: four intact equal squares, straight parallel edges and centred gaps, no clipping or bottom-edge reburning. Inspect only after all motion stops.", i.reach_mm.0, i.reach_mm.1));
        }
        if self == Step::LimitSquares {
            let (_, origin_y, bottoms) = squares(p, i)?;
            let edges = bottoms.iter().map(|edge| format!("{edge:.2}")).collect::<Vec<_>>().join(", ");
            return Ok(format!("HOME first. One combined burn: seven filled 2 mm squares left to right, 1 mm gaps, starting at X {} mm, Y {origin_y:.2} mm. Requested bottom edges: {edges} mm. Square 4 ends at the provisional border. No locator. {settings}. Inspect only after all motion stops; compare heights and bottom-edge reburning. Calibration deliberately enters the mechanical-stop region.", p.x_mm));
        }
        if self == Step::Limits {
            let (raster, origin_y, rows) = stairs(p, i)?;
            let length = raster.width / rows;
            return Ok(format!("HOME first. One combined burn: 2 mm locator square at X {} mm, Y {origin_y} mm; left zero line connects it across a 2 mm gap to Y limit stairs from {} to {} mm. {} rows, {} px per step ({:.2} mm). {settings}. Do not inspect or reposition between marks; inspect only after the whole burn stops. Expect a diagonal turning flat at the mechanical stop; measure the kink from the zero line. This deliberately tests beyond normal reach; the zero line may overburn one spot.", p.x_mm, p.limit_start_mm, p.limit_end_mm, rows, length, i.mm(length)));
        }
        Ok(match self {
            Step::Talk => format!("{} answers and says who it is. Nothing moves.", i.model),
            Step::Home => "Head moves to the home corner and stops.".into(),
            Step::Pointer => "Aiming dot goes off for 3 s, then back on. Head does not move.".into(),
            Step::Frame => format!("Dot traces a {} mm square, {at}, for ~8 s, then stops and goes home. Nothing burns.", p.size_mm),
            Step::Square => format!("Burns a filled {} mm square, {at}. {settings}.", p.size_mm),
            Step::Limits => unreachable!(),
            Step::LimitSquares => unreachable!(),
            Step::Corners => unreachable!(),
            Step::CornersContinuous => unreachable!(),
            Step::VerticalComparison => unreachable!(),
            Step::VerticalComparisonD => unreachable!(),
            Step::Registration => unreachable!(),
            Step::PowerSweep => unreachable!(),
            Step::AudioPowerSweepLow => unreachable!(),
            Step::AudioPowerSweepFull => unreachable!(),
            Step::BlankRows => format!(
                "Burns two {s} mm squares in one image, {at}. The second {s} mm right of and below the first's corner. Both burn fully, nothing between. {settings}.",
                s = p.size_mm
            ),
            Step::Qr => format!(
                "Burns a {} mm QR of the supplied private payload, {at}. {settings}. Phone scans it.",
                p.size_mm
            ),
        })
    }

    pub(crate) fn prepare(self, p: &Params, info: &Info) -> Result<Prepared, String> {
        if !self.supported(info) { return Err(format!("{} cannot execute {:?}", info.model, self)); }
        self.check_size(p, info)?;
        let values = info.resolve(&p.values).map_err(|error| error.to_string())?;
        let burn = |raster, x_mm, y_mm| Operation::Burn(Burn::new(raster, x_mm, y_mm), values.clone());
        let operations = match self {
            Step::Talk => vec![Operation::Talk],
            Step::Home => vec![Operation::Home],
            Step::Pointer => vec![Operation::Pointer],
            Step::Frame => vec![Operation::Frame(Rect { x:p.x_mm, y:p.y_mm, w:p.size_mm, h:p.size_mm })],
            Step::Square => vec![burn(Raster::square(info.px(p.size_mm)), p.x_mm, p.y_mm)],
            Step::Qr => vec![burn(qr(&p.text, info.px(p.size_mm))?, p.x_mm, p.y_mm)],
            Step::BlankRows => vec![burn(compose::pattern::blank_rows(info.px(p.size_mm), info.px(p.size_mm)), p.x_mm, p.y_mm)],
            Step::Corners => vec![burn(corners(info)?, p.x_mm, p.y_mm)],
            Step::CornersContinuous => vec![burn(continuous_corners(info)?, p.x_mm, p.y_mm)],
            Step::VerticalComparisonD => vec![burn(vertical_d(info)?, 40.0, 0.0)],
            Step::VerticalComparison => vertical_strips(info)?.into_iter().map(|(_, x, raster)| burn(raster, x, 0.0)).collect(),
            Step::Registration => {
                let (short, long) = registration(p, info)?;
                vec![burn(short, p.x_mm, p.y_mm), burn(long, p.x_mm, 0.0)]
            }
            Step::PowerSweep | Step::AudioPowerSweepLow | Step::AudioPowerSweepFull => sweep_jobs(p, info)?.into_iter().map(|job| Operation::Burn(Burn::new(job.raster, job.x_mm, job.y_mm), job.values)).collect(),
            Step::Limits => {
                let (raster, y, _) = stairs(p, info)?;
                vec![Operation::Calibrate(Burn::new(raster, p.x_mm, y), values)]
            }
            Step::LimitSquares => {
                let (raster, y, _) = squares(p, info)?;
                vec![Operation::Calibrate(Burn::new(raster, p.x_mm, y), values)]
            }
        };
        let mut homed = Vec::new();
        for operation in operations {
            if matches!(operation, Operation::Burn(..) | Operation::Calibrate(..)) { homed.push(Operation::Home); }
            homed.push(operation);
        }
        Prepared::new(homed, info)
    }

    pub fn run(self, m: &mut dyn Machine, p: &Params, progress: &mut dyn FnMut(Progress)) -> Result<String, String> {
        self.prepare(p, &m.info())?.run(m, progress, &|| false)
    }
}

fn registration(p: &Params, info: &Info) -> Result<(Raster, Raster), String> {
    if !p.x_mm.is_finite() || !p.y_mm.is_finite() || p.x_mm < 0.0 || p.y_mm <= 0.0 {
        return Err("registration needs finite X>=0 and Y>0 inside normal reach".into());
    }
    info.check_fits(Rect { x: p.x_mm, y: 0.0, w: 5.0, h: p.y_mm + 2.0 }).map_err(|e| e.to_string())?;
    let short = compose::pattern::registration(info.px(2.0), info.px(3.0), 0)?;
    let long = compose::pattern::registration(info.px(2.0), info.px(3.0), info.px(p.y_mm))?;
    Ok((short, long))
}

fn corners(info: &Info) -> Result<Raster, String> {
    compose::pattern::corner_frame(info.px(info.reach_mm.0), info.px(info.reach_mm.1), info.px(2.0), info.px(20.0))
}

fn continuous_corners(info: &Info) -> Result<Raster, String> {
    compose::pattern::corner_frame(info.px(info.reach_mm.0), info.px(info.reach_mm.1), info.px(2.0), 0)
}

fn vertical_strips(info: &Info) -> Result<Vec<(&'static str, f32, Raster)>, String> {
    let mut out = Vec::new();
    for (name, x_mm, height, continuous) in [("A", 10.0, 77.45, false), ("B", 20.0, 77.45, true), ("C", 30.0, 75.0, false)] {
        info.check_fits(Rect { x: x_mm, y: 0.0, w: 2.0, h: height }).map_err(|error| error.to_string())?;
        let raster = compose::pattern::vertical_strip(info.px(height), info.px(2.0), info.px(28.70), info.px(20.0), continuous)?;
        out.push((name, x_mm, raster));
    }
    Ok(out)
}

fn vertical_d(info: &Info) -> Result<Raster, String> {
    info.check_fits(Rect { x: 40.0, y: 0.0, w: 2.0, h: 75.0 }).map_err(|error| error.to_string())?;
    compose::pattern::vertical_strip(info.px(75.0), info.px(2.0), info.px(28.70), info.px(20.0), true)
}

pub fn preview_raster(step: Step, params: &Params, info: &Info) -> Result<Option<Arc<Raster>>, String> {
    step.check_size(params, info)?;
    if step == Step::Frame { return frame_raster(params, info).map(|raster| Some(Arc::new(raster))); }
    Ok(step.prepare(params, info)?.preview(info)?.map(|burn| burn.raster))
}

struct PowerSweepCase {
    power: u32,
    x_mm: f32,
    y_mm: f32,
    raster: Raster,
    values: Values,
}

fn sweep_jobs(p: &Params, info: &Info) -> Result<Vec<PowerSweepCase>, String> {
    if !p.x_mm.is_finite() || p.x_mm < 0.0 || p.y_mm != 0.0 || !p.size_mm.is_finite() || p.size_mm <= 0.0 {
        return Err("power sweep block X must be finite and nonnegative, Y is fixed at 0; line length must be finite and positive".into());
    }
    if ![1, usize::from(POWER_SWEEP_COUNT)].contains(&p.power_values.len()) {
        return Err("sweep needs one fixed power or exactly 10 configured power values".into());
    }
    let width = info.px(p.size_mm);
    if width == 0 { return Err("power sweep line is smaller than one pixel".into()); }
    info.check_fits(Rect { x: p.x_mm + POWER_SWEEP_X_INSET_MM, y: f32::from(POWER_SWEEP_COUNT - 1) * POWER_SWEEP_PITCH_MM, w: info.mm(width), h: info.mm(1) }).map_err(|error| error.to_string())?;
    let mut jobs = Vec::with_capacity(usize::from(POWER_SWEEP_COUNT));
    for index in 0..POWER_SWEEP_COUNT {
        let x_mm = p.x_mm + if index == 0 { 0.0 } else { POWER_SWEEP_X_INSET_MM };
        let y_mm = f32::from(index) * POWER_SWEEP_PITCH_MM;
        let power = p.power_values[if p.power_values.len() == 1 { 0 } else { usize::from(index) }];
        let mut values = p.values.clone();
        values.insert("power".into(), power);
        let values = info.resolve(&values).map_err(|error| error.to_string())?;
        let raster = if index == 0 { frame_raster(&Params { size_mm: POWER_REFERENCE_SIZE_MM, ..p.clone() }, info)? } else { Raster::filled(width, 1) };
        jobs.push(PowerSweepCase { power, x_mm, y_mm, raster, values });
    }
    Ok(jobs)
}

fn frame_raster(p: &Params, info: &Info) -> Result<Raster, String> {
    Step::Frame.check_size(p, info)?;
    let side = info.px(p.size_mm);
    let mut raster = Raster::new(side, side);
    for position in 0..side {
        raster.set(position, 0, true);
        raster.set(position, side - 1, true);
        raster.set(0, position, true);
        raster.set(side - 1, position, true);
    }
    Ok(raster)
}

fn squares(p: &Params, info: &Info) -> Result<(Raster, f32, Vec<f32>), String> {
    if !p.limit_border_mm.is_finite() || p.limit_border_mm <= 0.0 || !p.x_mm.is_finite() || p.x_mm < 0.0 {
        return Err("border and X must be finite valid coordinates".into());
    }
    let side = info.px(2.0);
    let drop = info.px(0.10);
    let origin = info.px(p.limit_border_mm).checked_sub(side + 3 * drop).ok_or("border is too low to fit the squares")?;
    let raster = compose::pattern::limit_squares(side, info.px(1.0), drop)?;
    let bottoms = (0..7).map(|index| info.mm(origin + side + index * drop)).collect();
    Ok((raster, info.mm(origin), bottoms))
}

fn stairs(p: &Params, info: &Info) -> Result<(Raster, f32, u32), String> {
    if [p.x_mm, p.limit_start_mm, p.limit_end_mm, p.limit_width_mm].iter().any(|value| !value.is_finite() || *value < 0.0)
        || p.limit_end_mm <= p.limit_start_mm || p.limit_width_mm == 0.0
    {
        return Err("limit range and width must be finite, positive and increasing".into());
    }
    let side = info.px(2.0);
    let prefix = side * 2;
    let start = info.px(p.limit_start_mm);
    let origin = start.checked_sub(prefix).ok_or("stairs need room above for a 2 mm locator and 2 mm gap")?;
    let rows = info.px(p.limit_end_mm) - start;
    let raster = compose::pattern::located_limit_stairs(info.px(p.limit_width_mm), rows, side, side)?;
    Ok((raster, info.mm(origin), rows))
}

#[cfg(test)]
mod tests {
    use super::*;
    use laser::{Done, done_text};
    use laser::sim::Sim;

    fn configured_params(step: Step, info: &Info, mut params: Params) -> Params {
        let definition = crate::test_catalog::load(concat!(env!("CARGO_MANIFEST_DIR"), "/../tests.toml"), info).unwrap()
            .into_iter().find(|definition| definition.step == step).unwrap();
        params.power_values = definition.power_values_for(info).to_vec();
        if let Some(power) = params.power_values.first() { params.values.insert("power".into(), *power); }
        params
    }

    #[test]
    fn invalid_sizes_are_rejected_before_rendering_or_machine_calls() {
        for step in [Step::Frame, Step::Square, Step::Qr, Step::BlankRows] {
            for size_mm in [f32::NAN, f32::INFINITY, f32::MAX, -1.0, 0.0, 0.00001] {
                let mut machine = Sim::default();
                let params = Params { size_mm, ..Params::default() };
                let info = machine.info();
                assert!(step.goal(&params, &info).is_err());
                assert!(preview_raster(step, &params, &info).is_err());
                assert!(step.run(&mut machine, &params, &mut |_| {}).is_err());
                assert!(machine.calls.is_empty());
            }
        }
    }

    #[test]
    fn silence_outcome_is_labelled_as_an_assumption() {
        let text = done_text(&Done::Silent { last_pct: Some(99) });
        assert!(text.contains("assumed complete") && text.contains("90 seconds"));
        assert!(!text.contains("reached 100%"));
    }

    #[test]
    fn power_sweep_levels_come_from_catalog_and_include_both_endpoints() {
        let info = k6::info();
        let levels = configured_params(Step::PowerSweep, &info, Params::default()).power_values;
        assert_eq!(levels, [1000, 889, 778, 667, 556, 444, 333, 222, 111, 0]);
    }

    #[test]
    fn power_sweep_runs_twenty_mm_frame_and_nine_remaining_lines_in_one_attempt() {
        let info = k6::info();
        let params = configured_params(Step::PowerSweep, &info, Params { x_mm: 20.0, values: [("power".into(), 500), ("depth".into(), 10)].into(), ..Params::default() });
        let jobs = sweep_jobs(&params, &info).unwrap();
        let (frame, cases) = jobs.split_first().unwrap();
        let reference = &frame.raster;
        let (reference_x, reference_y, reference_power) = (frame.x_mm, frame.y_mm, frame.power);
        assert_eq!(cases.iter().map(|case| case.power).collect::<Vec<_>>(), [889, 778, 667, 556, 444, 333, 222, 111, 0]);
        assert_eq!(cases.iter().map(|case| case.y_mm).collect::<Vec<_>>(), [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0]);
        assert!(cases.iter().all(|case| case.x_mm == 20.5));
        assert!(cases.iter().all(|case| case.raster.width == 380 && case.raster.height == 1 && case.raster.get(0, 0)));
        assert_eq!(cases[0].values["power"], 889);
        assert_eq!((reference.width, reference.height, reference_x, reference_y, reference_power), (400, 400, 20.0, 0.0, 1000));
        assert!((0..reference.width).all(|x| reference.get(x, 0) && reference.get(x, reference.height - 1)));
        assert!((0..reference.height).all(|y| reference.get(0, y) && reference.get(reference.width - 1, y)));
        assert!(!reference.get(100, 100));

        let mut machine = Sim::with_info(info);
        Step::PowerSweep.run(&mut machine, &params, &mut |_| {}).unwrap();
        assert_eq!(machine.calls.len(), 20);
        assert_eq!(machine.calls[0], "home");
        assert_eq!(machine.calls[1], "burn 400x400 power 1000");
        for (index, power) in [889, 778, 667, 556, 444, 333, 222, 111, 0].into_iter().enumerate() {
            assert_eq!(machine.calls[index * 2 + 2], "home");
            assert_eq!(machine.calls[index * 2 + 3], format!("burn 380x1 power {power}"));
        }
    }

    #[test]
    fn audio_profiles_run_identical_box_and_nine_lines_at_fixed_power() {
        let info = k6::info();
        let base = Params { x_mm:20.0, y_mm:0.0, size_mm:19.0, ..Params::default() };
        let params = configured_params(Step::AudioPowerSweepLow, &info, base.clone());
        let raster = preview_raster(Step::AudioPowerSweepLow, &params, &info).unwrap().unwrap();
        assert_eq!((raster.width, raster.height), (400, 400));
        assert!((0..400).all(|x| raster.get(x, 0) && raster.get(x, 399)));
        assert!((0..400).all(|y| raster.get(0, y) && raster.get(399, y)));
        for y in [40,80,120,160,200,240,280,320,360] {
            assert!((10..390).all(|x| raster.get(x, y)));
            assert!(!raster.get(9, y) && !raster.get(390, y));
        }

        for (step, power) in [(Step::AudioPowerSweepLow, 1), (Step::AudioPowerSweepFull, 1000)] {
            let params = configured_params(step, &info, base.clone());
            assert!(sweep_jobs(&params, &info).unwrap().iter().all(|job| job.power == power));
            let mut machine = Sim::with_info(info.clone());
            let outcome = step.run(&mut machine, &params, &mut |_| {}).unwrap();
            assert_eq!(machine.calls.len(), 20);
            assert_eq!(machine.calls[0], "home");
            assert_eq!(machine.calls[1], format!("burn 400x400 power {power}"));
            for index in 0..9 {
                assert_eq!(machine.calls[index * 2 + 3], format!("burn 380x1 power {power}"));
            }
            assert!(outcome.contains(&format!("Power {power}")));
        }
    }

    #[test]
    fn power_sweep_refuses_invalid_indices_and_out_of_reach_layouts() {
        let info = k6::info();
        let invalid_y = configured_params(Step::PowerSweep, &info, Params { y_mm: 0.5, ..Params::default() });
        assert!(Step::PowerSweep.goal(&invalid_y, &info).unwrap_err().contains("Y is fixed at 0"));
        let params = configured_params(Step::PowerSweep, &info, Params { x_mm: 61.0, ..Params::default() });
        assert!(Step::PowerSweep.goal(&params, &info).is_err());
    }

    #[test]
    fn sweep_preview_matches_job_pixels_at_offsets_and_variable_lengths() {
        let info = k6::info();
        for x_mm in [0.0, 20.0, 40.025] { for size_mm in [10.0, 19.0, 25.0] {
            let params = configured_params(Step::PowerSweep, &info, Params { x_mm, size_mm, ..Params::default() });
            let preview = preview_raster(Step::PowerSweep, &params, &info).unwrap().unwrap();
            let mut expected = Raster::new(preview.width, preview.height);
            for job in sweep_jobs(&params, &info).unwrap() {
                let column = info.px(job.x_mm) - info.px(x_mm);
                let row = info.px(job.y_mm);
                for y in 0..job.raster.height { for x in 0..job.raster.width {
                    if job.raster.get(x, y) { expected.set(column + x, row + y, true); }
                } }
            }
            assert_eq!(preview.as_ref(), &expected);
            assert_eq!(preview.width, info.px(20.0).max(info.px(x_mm + 0.5) - info.px(x_mm) + info.px(size_mm)));
            assert!((0..preview.width).filter(|x| preview.get(*x, 40)).count() >= info.px(size_mm) as usize);
            assert!(!preview.get(10, 41));
        } }
    }

    #[test]
    fn every_step_has_a_goal() {
        let info = laser::sim::info();
        for definition in crate::test_catalog::load(concat!(env!("CARGO_MANIFEST_DIR"), "/../tests.toml"), &info).unwrap() {
            let s = definition.step;
            let mut params = Params { y_mm: if matches!(s, Step::PowerSweep | Step::AudioPowerSweepLow | Step::AudioPowerSweepFull) { 0.0 } else { 20.0 }, ..Params::default() };
            params.power_values = definition.power_values_for(&info).to_vec();
            if let Some(power) = params.power_values.first() { params.values.insert("power".into(), *power); }
            if s == Step::Corners || s == Step::CornersContinuous {
                assert!(s.goal(&Params::default(), &info).is_ok());
            } else {
                assert!(s.goal(&params, &info).is_ok());
            }
        }
    }

    #[test]
    fn power_sweep_goal_describes_one_run_and_all_power_levels() {
        let params = configured_params(Step::PowerSweep, &k6::info(), Params { x_mm: 20.0, ..Params::default() });
        let goal = Step::PowerSweep.goal(&params, &k6::info()).unwrap();
        assert!(goal.contains("20.0 x 20.0 mm full-power frame at (20.0, 0.0)"));
        assert!(goal.contains("Its top edge replaces the duplicate full-power line."));
        assert!(goal.contains("Then submit 9 separately initialized"));
        assert!(goal.contains("error stops the remaining submissions"));
    }

    #[test]
    fn registration_is_inside_reach_and_needs_no_band_split() {
        let info = k6::info();
        let params = Params { x_mm: 10.0, y_mm: 20.0, ..Params::default() };
        let (short, long) = registration(&params, &info).unwrap();
        for raster in [short, long] {
            assert_eq!(k6::protocol::bands(&raster).len(), 1);
            assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(&raster)));
        }
    }

    #[test]
    fn continuous_corner_test_has_only_one_band() {
        let raster = continuous_corners(&k6::info()).unwrap();
        assert_eq!(k6::protocol::bands(&raster).len(), 1);
        assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(&raster)));
    }

    #[test]
    fn vertical_comparison_matches_diagram_and_band_counts() {
        let historical = Info { reach_mm: (80.0, 77.45), ..k6::info() };
        let cases = vertical_strips(&historical).unwrap();
        assert_eq!(cases.iter().map(|(_, x, raster)| (*x, raster.height)).collect::<Vec<_>>(), [(10.0, 1549), (20.0, 1549), (30.0, 1500)]);
        assert_eq!(cases.iter().map(|(_, _, raster)| k6::protocol::bands(raster).len()).collect::<Vec<_>>(), [2, 1, 2]);
        for (_, _, raster) in cases {
            for (_, band) in k6::protocol::bands(&raster) { assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(&band))); }
        }
    }

    #[test]
    fn vertical_comparison_homes_before_each_independent_job() {
        let mut machine = Sim::with_info(Info { reach_mm: (80.0, 77.45), ..k6::info() });
        Step::VerticalComparison.run(&mut machine, &Params::default(), &mut |_| {}).unwrap();
        assert_eq!(machine.calls.len(), 6);
        for pair in machine.calls.chunks(2) {
            assert_eq!(pair[0], "home");
            assert!(pair[1].starts_with("burn"));
        }
    }

    #[test]
    fn d_is_one_band_and_runs_only_one_homed_job() {
        let info = k6::info();
        let d = vertical_d(&info).unwrap();
        assert_eq!((d.width, d.height), (40, 1500));
        assert_eq!(k6::protocol::bands(&d).len(), 1);
        assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(&d)));
        for start in [0, 534, 974, 1460] { assert!(d.get(39, start) && d.get(39, start + 39)); }
        let mut machine = Sim::with_info(info);
        Step::VerticalComparisonD.run(&mut machine, &Params::default(), &mut |_| {}).unwrap();
        assert_eq!(machine.calls.len(), 2);
        assert_eq!(machine.calls[0], "home");
        assert!(machine.calls[1].starts_with("burn"));
    }

    #[test]
    fn limit_goal_declares_homing_range_and_line_length() {
        let goal = Step::Limits.goal(&Params::default(), &k6::info()).unwrap();
        assert!(goal.contains("HOME first") && goal.contains("74 to 80 mm"));
        assert!(goal.contains("120 rows, 13 px per step"));
        assert!(goal.contains("One combined burn") && goal.contains("inspect only after"));
        let invalid = Params { limit_start_mm: 80.0, limit_end_mm: 74.0, ..Params::default() };
        assert!(Step::Limits.goal(&invalid, &k6::info()).is_err());
    }

    #[test]
    fn fourth_square_ends_at_border_in_one_nonblank_payload() {
        let (raster, origin, bottoms) = squares(&Params::default(), &k6::info()).unwrap();
        assert!((origin - 75.15).abs() < 0.001);
        for (index, edge) in bottoms.iter().enumerate() {
            assert!((*edge - (77.15 + index as f32 * 0.10)).abs() < 0.001);
        }
        assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(&raster)));
        assert!(Step::LimitSquares.goal(&Params::default(), &k6::info()).unwrap().contains("No locator"));
    }

    #[test]
    fn corner_test_uses_normal_reach_and_two_nonblank_bands() {
        let info = k6::info();
        let raster = corners(&info).unwrap();
        assert_eq!((raster.width, raster.height), (1600, 1500));
        let bands = k6::protocol::bands(&raster);
        assert_eq!(bands.iter().map(|(row, band)| (*row, band.height)).collect::<Vec<_>>(), [(0, 550), (950, 550)]);
        assert!(bands.iter().all(|(_, band)| !k6::protocol::has_zero_chunk(&k6::protocol::pack(band))));
        assert!(Step::Corners.goal(&Params::default(), &info).unwrap().contains("20 mm centred gap"));
    }

    #[test]
    fn historical_77mm_comparison_is_not_a_normal_75mm_job() {
        assert!(!Step::VerticalComparison.supported(&k6::info()));
        assert!(Step::VerticalComparison.goal(&Params::default(), &k6::info()).is_err());
        assert!(Step::VerticalComparisonD.supported(&k6::info()));
    }

    #[test]
    fn combined_locator_has_correct_origin_and_no_blank_chunks() {
        let params = Params { x_mm: 5.0, limit_start_mm: 77.0, limit_end_mm: 78.0, limit_width_mm: 20.0, ..Params::default() };
        let (raster, origin_y, rows) = stairs(&params, &k6::info()).unwrap();
        assert_eq!((origin_y, rows, raster.height), (73.0, 20, 100));
        assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(&raster)));
    }

    #[test]
    fn goal_refuses_unknown_setting() {
        let mut p = Params::default();
        p.values.insert("depth".into(), 10);
        assert!(Step::Qr.goal(&p, &laser::sim::info()).is_err());
    }

    #[test]
    fn ladder_runs_on_the_simulator() {
        let mut m = Sim::default();
        for s in [Step::Talk, Step::Home, Step::Square, Step::Qr] {
            s.run(&mut m, &Params::default(), &mut |_| {}).unwrap();
        }
        assert_eq!(m.calls[0], "connect");
        assert!(m.calls.last().unwrap().starts_with("burn"));
    }

    #[test]
    fn outside_reach_never_reaches_the_machine() {
        let mut m = Sim::default();
        let p = Params { y_mm: 70.0, ..Params::default() };
        assert!(Step::Square.run(&mut m, &p, &mut |_| {}).is_err());
        assert!(m.calls.is_empty());
    }
}
