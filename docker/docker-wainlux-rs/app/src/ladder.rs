//! Test ladder. Each step states its goal before it runs.
//! The operator answers yes/no/comment after. Both land in the log.
//! Knows only the `laser` contract, never a model.

use compose::qr::qr;
use laser::{Done, Info, Job, Machine, Progress, Raster, Rect, Values};
use serde::{Deserialize, Serialize};
use std::thread::sleep;
use std::time::Duration;

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
}

pub const LADDER: [Step; 14] = [Step::Talk, Step::Home, Step::Pointer, Step::Frame, Step::Square, Step::Limits, Step::LimitSquares, Step::BlankRows, Step::Corners, Step::CornersContinuous, Step::VerticalComparison, Step::VerticalComparisonD, Step::Registration, Step::Qr];

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Params {
    pub x_mm: f32,
    pub y_mm: f32,
    pub size_mm: f32,
    pub text: String,
    /// Driver settings by key. Missing keys take the driver's defaults.
    pub values: Values,
    pub limit_start_mm: f32,
    pub limit_end_mm: f32,
    pub limit_width_mm: f32,
    #[serde(default = "default_border")]
    pub limit_border_mm: f32,
}

fn default_border() -> f32 { 77.45 }

impl Default for Params {
    fn default() -> Self {
        Params { x_mm: 0.0, y_mm: 0.0, size_mm: 19.0, text: "https://github.com/donnels".into(), values: Values::new(), limit_start_mm: 74.0, limit_end_mm: 80.0, limit_width_mm: 80.0, limit_border_mm: default_border() }
    }
}

impl Step {
    pub fn title(self) -> &'static str {
        match self {
            Step::Talk => "Talk",
            Step::Home => "Home",
            Step::Pointer => "Pointer",
            Step::Frame => "Frame",
            Step::Square => "First burn",
            Step::Limits => "Limit stairs",
            Step::LimitSquares => "Limit squares",
            Step::BlankRows => "Blank rows",
            Step::Corners => "Corner positions",
            Step::CornersContinuous => "9b: continuous perimeter",
            Step::VerticalComparison => "9b: three vertical strips",
            Step::VerticalComparisonD => "9b D: 75 mm continuous",
            Step::Registration => "Registration: short vs full line",
            Step::Qr => "QR",
        }
    }

    pub fn supported(self, i: &Info) -> bool {
        match self {
            Step::Pointer => i.pointer,
            Step::Frame => i.frame,
            Step::VerticalComparison => i.reach_mm.0 >= 32.0 && i.reach_mm.1 >= 77.45,
            _ => true,
        }
    }

    /// What should happen, in the operator's terms. Stated before running.
    pub fn goal(self, p: &Params, i: &Info) -> Result<String, String> {
        if !self.supported(i) {
            return Err(format!("{} cannot do {}", i.model, self.title()));
        }
        let v = i.resolve(&p.values).map_err(|e| e.to_string())?;
        let settings = describe(i, &v);
        let at = format!("top-left at {} mm, {} mm", p.x_mm, p.y_mm);
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
            Step::BlankRows => format!(
                "Burns two {s} mm squares in one image, {at}. The second {s} mm right of and below the first's corner. Both burn fully, nothing between. {settings}.",
                s = p.size_mm
            ),
            Step::Qr => format!(
                "Burns a {} mm QR of \"{}\", {at}. {settings}. Phone scans it.",
                p.size_mm, p.text
            ),
        })
    }

    /// Runs the step. Returns what the software saw.
    pub fn run(self, m: &mut dyn Machine, p: &Params, progress: &mut dyn FnMut(Progress)) -> Result<String, String> {
        let e = |e: laser::Error| e.to_string();
        let i = m.info();
        let square = Rect { x: p.x_mm, y: p.y_mm, w: p.size_mm, h: p.size_mm };
        match self {
            Step::Talk => m.connect().map_err(e),
            Step::Home => m.home().map(|_| "home acknowledged".into()).map_err(e),
            Step::Pointer => {
                // Off first: on the K6 the dot is already on when idle.
                m.pointer(false).map_err(e)?;
                sleep(Duration::from_secs(3));
                m.pointer(true).map_err(e)?;
                Ok("pointer off/on acknowledged".into())
            }
            Step::Frame => {
                m.frame(square).map_err(e)?;
                sleep(Duration::from_secs(8));
                m.stop().map_err(e)?;
                Ok("frame acknowledged, stop sent".into())
            }
            Step::Square => burn(m, &Raster::square(i.px(p.size_mm)), p, progress),
            Step::VerticalComparisonD => {
                let placement = Params { x_mm: 40.0, y_mm: 0.0, ..p.clone() };
                burn(m, &vertical_d(&i)?, &placement, progress)
            }
            Step::VerticalComparison => {
                let mut observations = Vec::new();
                for (name, x_mm, raster) in vertical_strips(&i)? {
                    m.home().map_err(|error| format!("{}; {name} HOME: {error}", observations.join("; ")))?;
                    let started = std::time::Instant::now();
                    let job = Job { raster: &raster, x_mm, y_mm: 0.0, values: &p.values };
                    let done = m.burn(&job, progress).map_err(|error| format!("{}; {name}: {error}", observations.join("; ")))?;
                    observations.push(format!("{name} {:.0} s: {}", started.elapsed().as_secs_f32(), done_text(&done)));
                }
                Ok(format!("{}. All three jobs returned; operator must confirm motion stopped, then inspect A/B/C together.", observations.join("; ")))
            }
            Step::Registration => {
                let (short, long) = registration(p, &i)?;
                let mut outcomes = Vec::new();
                for (name, raster, origin_y) in [("A", &short, p.y_mm), ("B", &long, 0.0)] {
                    m.home().map_err(e)?;
                    let job = Job { raster, x_mm: p.x_mm, y_mm: origin_y, values: &p.values };
                    let done = m.burn(&job, progress).map_err(e)?;
                    outcomes.push(format!("{name}: {}", done_text(&done)));
                }
                Ok(format!("{}. Operator must compare the overlaid square and witness line.", outcomes.join("; ")))
            }
            Step::Corners => burn(m, &corners(&i)?, p, progress),
            Step::CornersContinuous => burn(m, &continuous_corners(&i)?, p, progress),
            Step::LimitSquares => {
                let (raster, origin_y, _) = squares(p, &i)?;
                let job = Job { raster: &raster, x_mm: p.x_mm, y_mm: origin_y, values: &p.values };
                m.home().map_err(e)?;
                let done = m.calibrate_limits(&job, progress).map_err(e)?;
                Ok(format!("Seven limit squares in one raster: {}. Operator must confirm motion stopped and compare all seven.", done_text(&done)))
            }
            Step::Limits => {
                let (raster, origin_y, rows) = stairs(p, &i)?;
                let job = Job { raster: &raster, x_mm: p.x_mm, y_mm: origin_y, values: &p.values };
                m.home().map_err(e)?;
                let done = m.calibrate_limits(&job, progress).map_err(e)?;
                Ok(format!("Combined locator and stairs: {} stair rows, {} px per step: {}. Operator must confirm motion stopped and measure the kink.", rows, raster.width / rows, done_text(&done)))
            }
            Step::BlankRows => burn(m, &compose::pattern::blank_rows(i.px(p.size_mm), i.px(p.size_mm)), p, progress),
            Step::Qr => burn(m, &qr(&p.text, i.px(p.size_mm))?, p, progress),
        }
    }
}

fn burn(m: &mut dyn Machine, r: &Raster, p: &Params, progress: &mut dyn FnMut(Progress)) -> Result<String, String> {
    let job = Job { raster: r, x_mm: p.x_mm, y_mm: p.y_mm, values: &p.values };
    let i = m.info();
    i.check_fits(job.rect(&i)).map_err(|e| e.to_string())?;
    m.home().map_err(|e| e.to_string())?;
    let t = std::time::Instant::now();
    let done = m.burn(&job, progress).map_err(|e| e.to_string())?;
    Ok(format!("{}x{} px, done after {:.0} s: {}", r.width, r.height, t.elapsed().as_secs_f32(), done_text(&done)))
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

pub fn preview_raster(step: Step, params: &Params, info: &Info) -> Result<Option<Raster>, String> {
    Ok(match step {
        Step::Square => Some(Raster::square(info.px(params.size_mm))),
        Step::Qr => Some(qr(&params.text, info.px(params.size_mm))?),
        Step::BlankRows => Some(compose::pattern::blank_rows(info.px(params.size_mm), info.px(params.size_mm))),
        Step::Limits => Some(stairs(params, info)?.0),
        Step::LimitSquares => Some(squares(params, info)?.0),
        Step::Corners => Some(corners(info)?),
        Step::CornersContinuous => Some(continuous_corners(info)?),
        Step::VerticalComparison => None,
        Step::VerticalComparisonD => Some(vertical_d(info)?),
        Step::Registration => Some(registration(params, info)?.1),
        _ => None,
    })
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

/// Settings in the operator's words: "Power 1000, Depth 10".
pub fn describe(i: &Info, v: &Values) -> String {
    i.settings.iter().map(|s| format!("{} {}", s.label, v.get(s.key).copied().unwrap_or(s.default))).collect::<Vec<_>>().join(", ")
}

pub fn done_text(d: &Done) -> String {
    match d {
        Done::Complete => "status reached 100%".into(),
        Done::Stalled { last_pct } => format!("status stalled at {last_pct}%"),
        Done::Silent { last_pct } => format!("assumed complete after 90 seconds without a response; last status {last_pct:?}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use laser::sim::Sim;

    #[test]
    fn silence_outcome_is_labelled_as_an_assumption() {
        let text = done_text(&Done::Silent { last_pct: Some(99) });
        assert!(text.contains("assumed complete") && text.contains("90 seconds"));
        assert!(!text.contains("reached 100%"));
    }

    #[test]
    fn every_step_has_a_goal() {
        for s in LADDER {
            let params = Params { y_mm: 20.0, ..Params::default() };
            if s == Step::Corners || s == Step::CornersContinuous {
                assert!(s.goal(&Params::default(), &laser::sim::info()).is_ok());
            } else {
                assert!(s.goal(&params, &laser::sim::info()).is_ok());
            }
        }
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
