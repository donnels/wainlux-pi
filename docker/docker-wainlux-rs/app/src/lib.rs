// Copyright (c) 2026 Sean Donnellan
//! Standalone laser service. Web UI and HTTP API. One machine, one job at a time.
//! Every attempt is logged: goal first, then what the software saw, then the operator's verdict.
//!
//! GET  /api/machine          the machine: reach, settings, abilities
//! GET  /api/materials        materials with their settings for this machine
//! POST /api/jobs/preview     a job's stages and goals, nothing runs
//! POST /api/plan             fix one goal: {step, params, material} or {job, stage}
//! POST /api/run              run the planned attempt
//! GET  /api/state            progress, then what the software saw
//! POST /api/verdict          operator: {status: ok|deviation|skipped, comment}
//! POST /api/stop             stop now, even mid-burn
//! GET  /api/log              every attempt and verdict

pub mod job;
pub mod assets;
pub mod ladder;
pub mod materials;
pub mod templates;

use job::JobReq;
use base64::Engine;
use ladder::{Params, Step, LADDER};
use laser::{Info, Machine, Progress, ProgressPhase, Stop};
use materials::Material;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::fmt::Write as FmtWrite;
use std::io::Write;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use tiny_http::{Header, Method, Request, Response, Server};

pub struct Config {
    pub listen: String,
    pub data_dir: String,
    /// k6 or sim.
    pub driver: String,
    pub port_path: String,
    pub materials_path: String,
    pub templates_path: String,
}

impl Config {
    pub fn from_env() -> Self {
        let env = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.into());
        Config {
            listen: env("LISTEN", "0.0.0.0:8081"),
            data_dir: env("DATA_DIR", "data"),
            driver: env("MACHINE", "k6"),
            port_path: env("LASER_PORT", "/dev/ttyUSB0"),
            materials_path: env("MATERIALS", "materials.toml"),
            templates_path: env("USE_CASES", "use-cases"),
        }
    }
}

#[derive(Default)]
struct State {
    /// Planned or running attempt as logged: id, goal, redacted request.
    attempt: Option<Value>,
    /// What runs. Kept apart from `attempt` because a job holds secrets.
    work: Option<Work>,
    running: bool,
    progress: Option<Progress>,
    band_timings: Vec<Progress>,
    /// What the software saw. Waits here for the operator's verdict.
    outcome: Option<Value>,
    review: Option<Review>,
}

#[derive(Clone)]
struct Review {
    id: String,
    job: JobReq,
    stages: Vec<Arc<job::Stage>>,
    accepted: Vec<usize>,
}

#[derive(Clone)]
enum Work {
    Step(Step, Params),
    Job(JobReq, Arc<job::Stage>),
}

struct App {
    driver: String,
    port_path: String,
    info: Info,
    simulated: AtomicBool,
    cancel_requested: AtomicBool,
    materials: Vec<Material>,
    templates: Vec<templates::Template>,
    log_path: String,
    state: Mutex<State>,
    machine: Mutex<Option<Box<dyn Machine>>>,
    stopper: Mutex<Option<Box<dyn Stop>>>,
}

/// The only place that names a driver.
fn driver_info(driver: &str) -> Result<Info, String> {
    match driver {
        "k6" => Ok(k6::info()),
        "sim" => Ok(laser::sim::info()),
        other => Err(format!("unknown MACHINE '{other}' (k6, sim)")),
    }
}

fn open(app: &App) -> laser::Result<Box<dyn Machine>> {
    if app.simulated.load(Ordering::SeqCst) {
        return Ok(Box::new(laser::sim::Sim::with_info(app.info.clone())));
    }
    match app.driver.as_str() {
        "k6" => k6::open(&app.port_path),
        _ => Ok(Box::new(laser::sim::Sim::default())),
    }
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Millimetres to 0.01 for clients: hides f32 noise such as 12.4399995.
fn r2(mm: f32) -> f64 {
    (mm as f64 * 100.0).round() / 100.0
}

/// Start serving in the background. Returns the bound address.
pub fn serve(cfg: Config) -> Result<SocketAddr, String> {
    let info = driver_info(&cfg.driver)?;
    let materials = materials::load(&cfg.materials_path)?;
    let templates = templates::load(&cfg.templates_path)?;
    std::fs::create_dir_all(&cfg.data_dir).map_err(|e| format!("{}: {e}", cfg.data_dir))?;
    let server = Server::http(&cfg.listen).map_err(|e| format!("bind {}: {e}", cfg.listen))?;
    let addr = server.server_addr().to_ip().ok_or("not an IP listener")?;
    let app = Arc::new(App {
        info,
        simulated: AtomicBool::new(true),
        cancel_requested: AtomicBool::new(false),
        materials,
        templates,
        driver: cfg.driver,
        port_path: cfg.port_path,
        log_path: format!("{}/runs.jsonl", cfg.data_dir),
        state: Mutex::new(State::default()),
        machine: Mutex::new(None),
        stopper: Mutex::new(None),
    });
    eprintln!("{} on {}, http://{addr}, log {}", app.info.model, app.port_path, app.log_path);
    std::thread::spawn(move || {
        for req in server.incoming_requests() {
            let app = app.clone();
            std::thread::spawn(move || handle(&app, req));
        }
    });
    Ok(addr)
}

fn handle(app: &Arc<App>, mut req: Request) {
    let mut body = String::new();
    let _ = req.as_reader().read_to_string(&mut body);
    let (code, out) = match (req.method(), req.url()) {
        (Method::Get, "/api") => {
            let content_type = Header::from_bytes("Content-Type", "text/plain; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_string(include_str!("../../README.adoc")).with_header(content_type));
            return;
        }
        (Method::Get, "/openapi.yaml") => {
            let content_type = Header::from_bytes("Content-Type", "application/yaml; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_string(include_str!("../openapi.yaml")).with_header(content_type));
            return;
        }
        (Method::Get, "/docs") | (Method::Get, "/docs/") => {
            let content_type = Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_string(include_str!("../swagger.html")).with_header(content_type));
            return;
        }
        (Method::Get, "/docs/swagger-ui.css") => {
            let content_type = Header::from_bytes("Content-Type", "text/css; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_data(include_bytes!("../assets/swagger-ui/swagger-ui.css").to_vec()).with_header(content_type));
            return;
        }
        (Method::Get, "/docs/swagger-ui-bundle.js") => {
            let content_type = Header::from_bytes("Content-Type", "text/javascript; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_data(include_bytes!("../assets/swagger-ui/swagger-ui-bundle.js").to_vec()).with_header(content_type));
            return;
        }
        (Method::Get, "/docs/favicon-16x16.png") => {
            let content_type = Header::from_bytes("Content-Type", "image/png").unwrap();
            let _ = req.respond(Response::from_data(include_bytes!("../assets/swagger-ui/favicon-16x16.png").to_vec()).with_header(content_type));
            return;
        }
        (Method::Get, "/docs/favicon-32x32.png") => {
            let content_type = Header::from_bytes("Content-Type", "image/png").unwrap();
            let _ = req.respond(Response::from_data(include_bytes!("../assets/swagger-ui/favicon-32x32.png").to_vec()).with_header(content_type));
            return;
        }
        (Method::Get, "/assets/vsr-logo.png") => {
            let content_type = Header::from_bytes("Content-Type", "image/png").unwrap();
            let _ = req.respond(Response::from_data(include_bytes!("../assets/vsr-logo.png").to_vec()).with_header(content_type));
            return;
        }
        (Method::Get, "/") => {
            let html = Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_string(include_str!("ui.html")).with_header(html));
            return;
        }
        (Method::Get, "/api/machine") => (200, machine(&app.info)),
        (Method::Get, "/api/mode") => (200, mode(app)),
        (Method::Post, "/api/mode") => set_mode(app, &body),
        (Method::Get, "/api/materials") => (200, materials_json(app)),
        (Method::Get, "/api/templates") => (200, json!(app.templates)),
        (Method::Get, "/api/assets") => (200, assets::catalogue()),
        (Method::Get, "/api/assets/tiger.png") => {
            let content_type = Header::from_bytes("Content-Type", "image/png").unwrap();
            let _ = req.respond(Response::from_data(assets::bytes("tiger").unwrap().to_vec()).with_header(content_type));
            return;
        }
        (Method::Get, "/api/steps") => (200, steps(&app.info)),
        (Method::Get, "/api/state") => (200, state(app)),
        (Method::Get, "/api/attempt/preview") => active_preview(app),
        (Method::Get, "/api/log") => (200, json!(read_log(app))),
        (Method::Get, "/api/pending-verdicts") => (200, json!(pending_verdicts(app))),
        (Method::Post, "/api/jobs/preview") => preview(app, &body),
        (Method::Post, "/api/jobs/review") => submit_review(app, &body),
        (Method::Get, "/api/jobs/review") => current_review(app),
        (Method::Post, "/api/jobs/form") => edit_form(app, &body),
        (Method::Delete, "/api/jobs/review") => discard_review(app),
        (Method::Post, "/api/cards/import") => import_card(app, &body),
        (Method::Post, "/api/tests/preview") => test_preview(app, &body),
        (Method::Post, "/api/plan") => plan(app, &body),
        (Method::Post, "/api/run") => run(app),
        (Method::Post, "/api/verdict") => verdict(app, &body),
        (Method::Post, "/api/stop") => stop(app),
        _ => (404, json!({"error": "not found"})),
    };
    let ct = Header::from_bytes("Content-Type", "application/json").unwrap();
    let cache = Header::from_bytes("Cache-Control", "no-store").unwrap();
    let _ = req.respond(Response::from_string(out.to_string()).with_status_code(code).with_header(ct).with_header(cache));
}

fn machine(i: &Info) -> Value {
    json!({
        "model": i.model,
        "px_per_mm": i.px_per_mm,
        "reach_mm": [i.reach_mm.0, i.reach_mm.1],
        "pointer": i.pointer,
        "frame": i.frame,
        "settings": i.settings.iter().map(|s| json!({
            "key": s.key, "label": s.label, "min": s.min, "max": s.max, "default": s.default, "note": s.note,
        })).collect::<Vec<_>>(),
    })
}

fn materials_json(app: &App) -> Value {
    json!(app.materials.iter().map(|m| json!({
        "material": m,
        "values": m.values(&app.info).map(|v| json!(v)).unwrap_or_else(|e| json!({"error": e})),
    })).collect::<Vec<_>>())
}

fn steps(i: &Info) -> Value {
    let p = Params::default();
    json!({
        "machine": machine(i),
        "defaults": p,
        "steps": LADDER.iter().filter(|s| s.supported(i)).map(|s| json!({
            "step": s, "title": s.title(), "goal": s.goal(&p, i).unwrap_or_else(|e| e),
        })).collect::<Vec<_>>(),
    })
}

fn state(app: &App) -> Value {
    let s = app.state.lock().unwrap();
    json!({"attempt": s.attempt, "running": s.running, "progress": s.progress.map(progress_json), "band_timings": s.band_timings.iter().copied().map(progress_json).collect::<Vec<_>>(), "outcome": s.outcome, "mode": mode(app), "review_id":s.review.as_ref().map(|review| &review.id), "accepted_stages":s.review.as_ref().map(|review| &review.accepted)})
}

fn progress_json(progress: Progress) -> Value {
    let phase = match progress.phase {
        ProgressPhase::Starting => "starting",
        ProgressPhase::Waiting => "waiting",
        ProgressPhase::ReportedComplete => "reported_complete",
        ProgressPhase::AssumedComplete => "assumed_complete",
    };
    json!({"phase":phase,"percent":progress.percent,"band":progress.band,"bands":progress.bands,"row_start":progress.row_start,"row_end":progress.row_end,"previous_assumed":progress.previous_assumed,"transfer_ms":progress.transfer_ms,"setup_ms":progress.setup_ms,"wait_ms":progress.wait_ms,"elapsed_ms":progress.elapsed_ms})
}

fn mode(app: &App) -> Value {
    let real = !app.simulated.load(Ordering::SeqCst);
    json!({"real": real, "mode": if real { "real" } else { "simulator" }, "real_available": app.driver != "sim"})
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModeReq { real: bool }

fn set_mode(app: &App, body: &str) -> (u16, Value) {
    let req: ModeReq = match serde_json::from_str(body) {
        Ok(req) => req,
        Err(error) => return (400, json!({"error": error.to_string()})),
    };
    let mut state = app.state.lock().unwrap();
    if state.running || state.attempt.is_some() || state.outcome.is_some() {
        return (409, json!({"error": "finish or skip the planned attempt before changing mode"}));
    }
    if req.real && app.driver == "sim" {
        return (400, json!({"error": "this installation has no real driver configured"}));
    }
    *app.machine.lock().unwrap() = None;
    *app.stopper.lock().unwrap() = None;
    if mode(app)["real"] != req.real {
        if let Some(review) = state.review.as_mut() { review.accepted.clear(); }
    }
    app.simulated.store(!req.real, Ordering::SeqCst);
    let result = mode(app);
    log(app, json!({"event": "mode", "mode": result["mode"], "at": now()}));
    (200, result)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PreviewReq {
    job: JobReq,
}

fn preview(app: &App, body: &str) -> (u16, Value) {
    let req: PreviewReq = match serde_json::from_str(body) {
        Ok(r) => r,
        Err(e) => return (400, json!({"error": e.to_string()})),
    };
    if let Err(error) = display_for(app, &req.job) { return (400, json!({"error":error})); }
    let placement = match job::placement(&req.job, &app.materials, &app.info) {
        Ok(placement) => placement,
        Err(error) => return (400, json!({"error": error})),
    };
    match job::stages(&req.job, &app.materials, &app.info) {
        Ok(stages) => match job_preview(app, &req.job, placement, stages.iter()) {
            Ok(value) => (200, value),
            Err(error) => (500, json!({"error":error})),
        },
        Err(e) => (400, json!({"error": e})),
    }
}

fn display_for(app: &App, job: &JobReq) -> Result<templates::Display, String> {
    match &job.template {
        None => Ok(templates::Display::default()),
        Some(id) => app.templates.iter().find(|template| &template.id == id).map(|template| template.display.clone()).ok_or_else(|| format!("unknown template '{id}'")),
    }
}

fn job_preview<'a>(app: &App, job: &JobReq, placement: compose::place::Placement, stages: impl IntoIterator<Item = &'a job::Stage>) -> Result<Value, String> {
    let stages = stages.into_iter().map(|stage| {
        let burns = stage.burns.iter().map(|burn| Ok(json!({
            "x_mm":r2(burn.x_mm), "y_mm":r2(burn.y_mm),
            "w_mm":r2(app.info.mm(burn.raster.width)), "h_mm":r2(app.info.mm(burn.raster.height)),
            "width_px":burn.raster.width, "height_px":burn.raster.height,
            "svg":raster_svg(&burn.raster), "png":raster_png(&burn.raster)?,
        }))).collect::<Result<Vec<Value>, String>>()?;
        Ok(json!({"name":stage.name,"side":stage.side,"goal":stage.goal,"values":stage.values,"burns":burns}))
    }).collect::<Result<Vec<Value>, String>>()?;
    Ok(json!({"display": display_for(app, job).unwrap_or_default(), "layout": {
            "reach_mm": [r2(app.info.reach_mm.0), r2(app.info.reach_mm.1)],
            "piece": rect_json(placement.piece),
            "etch": placement.etch_area().map(rect_json),
        }, "stages":stages}))
}

fn raster_png(raster: &laser::Raster) -> Result<String, String> {
    let stride = (raster.width as usize + 7) / 8;
    let mut pixels = vec![255u8; stride * raster.height as usize];
    for row in 0..raster.height {
        for column in 0..raster.width {
            if raster.get(column, row) { pixels[row as usize * stride + column as usize / 8] &= !(1 << (7 - column % 8)); }
        }
    }
    let mut bytes = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut bytes, raster.width, raster.height);
        encoder.set_color(png::ColorType::Grayscale);
        encoder.set_depth(png::BitDepth::One);
        let mut writer = encoder.write_header().map_err(|error| error.to_string())?;
        writer.write_image_data(&pixels).map_err(|error| error.to_string())?;
    }
    Ok(format!("data:image/png;base64,{}", base64::engine::general_purpose::STANDARD.encode(bytes)))
}

fn submit_review(app: &App, body: &str) -> (u16, Value) {
    let request: PreviewReq = match serde_json::from_str(body) {
        Ok(request) => request,
        Err(_) => return (400, json!({"error":"expected a job object"})),
    };
    let template = match selected_template(app, &request.job) { Ok(template) => template, Err(error) => return (400, json!({"error":error})) };
    let job = match template.values(&request.job).and_then(|values| template.compose(&request.job, &values)) {
        Ok(job) => job,
        Err(error) => return (400, json!({"error":error})),
    };
    store_review(app, job, None)
}

fn selected_template<'a>(app: &'a App, job: &JobReq) -> Result<&'a templates::Template, String> {
    app.templates.iter().find(|template| Some(&template.id) == job.template.as_ref()).ok_or_else(|| "select a declared use-case template first".into())
}

fn store_review(app: &App, job: JobReq, expected_review: Option<Option<String>>) -> (u16, Value) {
    let result = job::stages(&job, &app.materials, &app.info);
    let stages = match result {
        Ok(stages) => stages.into_iter().map(Arc::new).collect(),
        Err(error) => return (400, json!({"error":error})),
    };
    let id = format!("review-{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos());
    let review = Review {id, job, stages, accepted:vec![]};
    let value = match review_json(app, &review) {
        Ok(value) => value,
        Err(error) => return (500, json!({"error":error})),
    };
    let mut state = app.state.lock().unwrap();
    if state.running || state.attempt.is_some() { return (409, json!({"error":"finish the active attempt before replacing the review"})); }
    if expected_review.as_ref().map(|expected| state.review.as_ref().map(|review| &review.id) != expected.as_ref()).unwrap_or(false) { return (409, json!({"error":"review changed; reload before editing"})); }
    state.review = Some(review);
    (200, value)
}

fn review_json(app: &App, review: &Review) -> Result<Value, String> {
    let placement = job::placement(&review.job, &app.materials, &app.info)?;
    let mut value = job_preview(app, &review.job, placement, review.stages.iter().map(Arc::as_ref))?;
    value["review_id"] = json!(review.id);
    value["accepted_stages"] = json!(review.accepted);
    let template = selected_template(app, &review.job)?;
    value["fields"] = template.project(&template.values(&review.job)?);
    value["template"] = json!(template);
    let mut job = json!(review.job);
    for item in job["items"].as_array_mut().unwrap() { item.as_object_mut().unwrap().remove("content"); }
    value["job"] = job;
    Ok(value)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FormReq {
    template: String,
    review_id: Option<String>,
    fields: std::collections::BTreeMap<String, Value>,
    piece: String,
    backing: Option<String>,
    placement: job::Where,
    guide: bool,
}

fn edit_form(app: &App, body: &str) -> (u16, Value) {
    let request: FormReq = match serde_json::from_str(body) { Ok(request) => request, Err(_) => return (400, json!({"error":"invalid template form"})) };
    let Some(template) = app.templates.iter().find(|template| template.id == request.template) else { return (400, json!({"error":"unknown template"})); };
    let previous = app.state.lock().unwrap().review.clone();
    let (mut base, mut values) = match (&request.review_id, previous) {
        (Some(id), Some(review)) if &review.id == id && review.job.template.as_ref() == Some(&request.template) => {
            let values = match template.values(&review.job) { Ok(values) => values, Err(error) => return (400, json!({"error":error})) };
            (review.job, values)
        }
        (None, None) => (JobReq {template:Some(request.template.clone()),piece:request.piece.clone(),backing:request.backing.clone(),placement:request.placement,guide:request.guide,items:vec![],values:laser::Values::new()}, template.defaults()),
        _ => return (409, json!({"error":"review changed; reload it or explicitly discard before selecting another type"})),
    };
    if let Err(error) = template.edit(&mut values, &request.fields) { return (403, json!({"error":error})); }
    base.piece = request.piece; base.backing = request.backing; base.placement = request.placement; base.guide = request.guide;
    match template.compose(&base, &values) {
        Ok(job) => store_review(app, job, Some(request.review_id)),
        Err(error) => (400, json!({"error":error})),
    }
}

fn discard_review(app: &App) -> (u16, Value) {
    let mut state = app.state.lock().unwrap();
    if state.running || state.attempt.is_some() { return (409, json!({"error":"finish the active attempt first"})); }
    state.review = None;
    (200, json!({"discarded":true}))
}

fn current_review(app: &App) -> (u16, Value) {
    let review = app.state.lock().unwrap().review.clone();
    match &review {
        None => (404, json!({"error":"no submitted review"})),
        Some(review) => match review_json(app, review) {
            Ok(value) => (200, value),
            Err(error) => (500, json!({"error":error})),
        },
    }
}

fn active_preview(app: &App) -> (u16, Value) {
    let (attempt, work) = {
        let state = app.state.lock().unwrap();
        (state.attempt.clone(), state.work.clone())
    };
    let (Some(attempt), Some(Work::Job(job, stage))) = (attempt, work) else {
        return (404, json!({"error":"no card attempt to preview"}));
    };
    match job::placement(&job, &app.materials, &app.info) {
        Ok(placement) => {
            let mut preview = match job_preview(app, &job, placement, std::iter::once(stage.as_ref())) {
                Ok(preview) => preview,
                Err(error) => return (500, json!({"error":error})),
            };
            preview["attempt_id"] = attempt["id"].clone();
            (200, preview)
        }
        Err(error) => (400, json!({"error":error})),
    }
}

fn rect_json(rect: laser::Rect) -> Value {
    json!({"x": r2(rect.x), "y": r2(rect.y), "w": r2(rect.w), "h": r2(rect.h)})
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ImportReq { toml: String, template: String }

fn import_card(app: &App, body: &str) -> (u16, Value) {
    let req: ImportReq = match serde_json::from_str(body) {
        Ok(req) => req,
        Err(_) => return (400, json!({"error": "expected a JSON object with a toml string"})),
    };
    let Some(template) = app.templates.iter().find(|template| template.id == req.template) else { return (400, json!({"error":"select a declared template first"})); };
    match job::from_toml(&req.toml, template, &app.materials, &app.info) {
        Ok(job) => (200, json!({"job": job})),
        Err(error) => (400, json!({"error": error})),
    }
}

fn raster_svg(raster: &laser::Raster) -> String {
    let mut path = String::new();
    for row in 0..raster.height {
        let mut column = 0;
        while column < raster.width {
            if !raster.get(column, row) { column += 1; continue; }
            let start = column;
            while column < raster.width && raster.get(column, row) { column += 1; }
            let length = column - start;
            let _ = write!(path, "M{start} {row}h{length}v1h-{length}z");
        }
    }
    format!("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 {} {}\"><rect width=\"100%\" height=\"100%\" fill=\"white\"/><path d=\"{path}\" fill=\"black\"/></svg>", raster.width, raster.height)
}

fn frame_preview(params: &Params, info: &Info) -> Result<laser::Raster, String> {
    if [params.x_mm, params.y_mm, params.size_mm].iter().any(|value| !value.is_finite()) || params.size_mm <= 0.0 {
        return Err("frame position and size must be finite; size must be positive".into());
    }
    info.check_fits(laser::Rect { x: params.x_mm, y: params.y_mm, w: params.size_mm, h: params.size_mm }).map_err(|error| error.to_string())?;
    let side = info.px(params.size_mm);
    if side == 0 { return Err("frame size is too small".into()); }
    let mut raster = laser::Raster::new(side, side);
    for offset in 0..side {
        raster.set(offset, 0, true);
        raster.set(offset, side - 1, true);
        raster.set(0, offset, true);
        raster.set(side - 1, offset, true);
    }
    Ok(raster)
}

fn test_preview(app: &App, body: &str) -> (u16, Value) {
    let req: PlanReq = match serde_json::from_str(body) {
        Ok(req) => req,
        Err(error) => return (400, json!({"error": error.to_string()})),
    };
    let Some(step) = req.step else { return (400, json!({"error": "step required"})); };
    let result = step.goal(&req.params, &app.info).and_then(|goal| {
        let raster = if step == Step::Frame {
            Some(frame_preview(&req.params, &app.info)?)
        } else {
            ladder::preview_raster(step, &req.params, &app.info)?
        };
        let svg = if step == Step::VerticalComparison {
            Some(include_str!("../assets/test-patterns/vertical-comparison.svg").to_string())
        } else {
            raster.as_ref().map(raster_svg)
        };
        let note = match step {
            Step::Frame => "Schematic of the laser-off BOUNDS frame; not a live projection or burn result.",
            Step::Registration => "Pass B shown; pass A omits the long leading line.",
            Step::VerticalComparison => "Schematic of the A/B/C comparison and separate D follow-up; inspect actual marks only after the supervised runs stop.",
            _ => "Pattern preview only; it is not a burn result.",
        };
        Ok(json!({"goal": goal, "svg": svg, "mode": mode(app), "note": note}))
    });
    match result { Ok(value) => (200, value), Err(error) => (400, json!({"error": error})) }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PlanReq {
    step: Option<Step>,
    #[serde(default)]
    params: Params,
    /// Material id for a ladder step. Its settings, under any explicit values.
    material: Option<String>,
    job: Option<JobReq>,
    review_id: Option<String>,
    #[serde(default)]
    placement_confirmed: bool,
    #[serde(default)]
    stage: usize,
}

/// Fix the goal before anything runs.
fn plan(app: &App, body: &str) -> (u16, Value) {
    let mut req: PlanReq = match serde_json::from_str(body) {
        Ok(r) => r,
        Err(e) => return (400, json!({"error": e.to_string()})),
    };
    if let Some(id) = req.review_id.take() {
        if req.step.is_some() || req.job.is_some() { return (400, json!({"error":"review_id cannot be combined with step or job"})); }
        let mut state = app.state.lock().unwrap();
        if state.running || state.attempt.is_some() { return (409, json!({"error":"finish the active attempt first"})); }
        let Some(review) = state.review.as_ref().filter(|review| review.id == id) else {
            return (409, json!({"error":"review is missing or stale; preview again"}));
        };
        let Some(stage) = review.stages.get(req.stage).cloned() else { return (400, json!({"error":"invalid review stage"})); };
        if (0..req.stage).any(|index| !review.accepted.contains(&index)) { return (409, json!({"error":"accept previous stage observations before continuing"})); }
        if stage.side == Some(job::Side::Back) && !req.placement_confirmed { return (409, json!({"error":"confirm the card is flipped and the back is placed correctly"})); }
        let work = Work::Job(review.job.clone(), stage.clone());
        let attempt = json!({"id":format!("{}-{}",SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos(),stage.name),"review_id":id,"job":review.job.redacted(),"stage":req.stage,"stage_name":stage.name,"goal":stage.goal,"machine":app.info.model,"mode":mode(app)["mode"],"planned":now()});
        state.review.as_mut().unwrap().accepted.retain(|index| *index < req.stage);
        state.attempt = Some(attempt.clone()); state.work = Some(work);
        return (200, attempt);
    }
    let planned = match (req.step, req.job.take()) {
        (Some(step), None) => {
            if let Some(id) = &req.material {
                if let Err(e) = materials::apply(&mut req.params, id, &app.materials, &app.info) {
                    return (400, json!({"error": e}));
                }
            }
            step.goal(&req.params, &app.info).map(|goal| {
                let a = json!({"step": step, "params": req.params, "material": req.material, "goal": goal});
                (a, Work::Step(step, req.params))
            })
        }
        (None, Some(j)) => job::stages(&j, &app.materials, &app.info).and_then(|s| {
            display_for(app, &j)?;
            let count = s.len();
            let st = s.into_iter().nth(req.stage).ok_or(format!("stage {} of {}", req.stage, count))?;
            if st.side == Some(job::Side::Back) { return Err("back-side burns require a reviewed job and flip confirmation".into()); }
            let a = json!({"job": j.redacted(), "stage": req.stage, "stage_name": st.name, "goal": st.goal});
            Ok((a, Work::Job(j, Arc::new(st))))
        }),
        _ => Err("give either step or job".into()),
    };
    let (mut attempt, work) = match planned {
        Ok(p) => p,
        Err(e) => return (400, json!({"error": e})),
    };
    let mut s = app.state.lock().unwrap();
    if s.running || s.outcome.is_some() {
        return (409, json!({"error": "finish the current attempt first (run or verdict)"}));
    }
    let id = now();
    attempt["id"] = json!(format!("{id}-{}", attempt.get("stage_name").or(attempt.get("step")).unwrap()).replace(['"', ' '], ""));
    attempt["machine"] = json!(app.info.model);
    attempt["mode"] = mode(app)["mode"].clone();
    attempt["planned"] = json!(id);
    s.attempt = Some(attempt.clone());
    s.work = Some(work);
    (200, attempt)
}

fn run(app: &Arc<App>) -> (u16, Value) {
    let (attempt, work) = {
        let mut s = app.state.lock().unwrap();
        if s.running || s.outcome.is_some() {
            return (409, json!({"error": "already running or awaiting verdict"}));
        }
        let (Some(a), Some(w)) = (s.attempt.clone(), s.work.clone()) else {
            return (409, json!({"error": "plan first"}));
        };
        s.running = true;
        s.progress = None;
        s.band_timings.clear();
        app.cancel_requested.store(false, Ordering::SeqCst);
        (a, w)
    };
    let app = app.clone();
    std::thread::spawn(move || {
        let started = now();
        let result = execute(&app, &work);
        let timings = app.state.lock().unwrap().band_timings.iter().copied().map(progress_json).collect::<Vec<_>>();
        let outcome = json!({
            "ok": result.is_ok(),
            "saw": match &result { Ok(m) | Err(m) => m },
            "started": started,
            "ended": now(),
            "band_timings": timings,
        });
        log(&app, json!({"event": "ran", "attempt": attempt, "outcome": outcome}));
        let mut s = app.state.lock().unwrap();
        s.running = false;
        s.outcome = Some(outcome);
    });
    (202, json!({"running": true}))
}

fn execute(app: &App, work: &Work) -> Result<String, String> {
    let mut slot = app.machine.lock().unwrap();
    if app.cancel_requested.load(Ordering::SeqCst) { return Err("execution cancelled before device access".into()); }
    let talk = matches!(work, Work::Step(Step::Talk, _));
    if talk || slot.is_none() {
        *slot = None;
        // The stop handle shares the port's exclusive lock: drop it too, or the reopen fails.
        *app.stopper.lock().unwrap() = None;
        let mut m = open(app).map_err(|e| e.to_string())?;
        *app.stopper.lock().unwrap() = m.stopper().ok();
        if app.cancel_requested.load(Ordering::SeqCst) { return Err("execution cancelled before connection".into()); }
        if !talk {
            m.connect().map_err(|e| e.to_string())?;
        }
        *slot = Some(m);
    }
    let m = slot.as_mut().unwrap();
    if app.cancel_requested.load(Ordering::SeqCst) { return Err("execution cancelled before stage".into()); }
    let mut progress = |progress: Progress| {
        let mut state = app.state.lock().unwrap();
        state.progress = Some(progress);
        if matches!(progress.phase, ProgressPhase::ReportedComplete | ProgressPhase::AssumedComplete) { state.band_timings.push(progress); }
    };
    let r = match work {
        Work::Step(step, p) => step.run(m.as_mut(), p, &mut progress),
        Work::Job(_, stage) => job::run(m.as_mut(), stage, &mut progress),
    };
    if r.is_err() {
        // Reopen next time; the link state is unknown after an error.
        *slot = None;
    }
    r
}

#[derive(Clone, Copy, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum VerdictStatus { Ok, Deviation, Skipped }

impl VerdictStatus {
    fn legacy_ok(self) -> Option<bool> {
        match self { Self::Ok => Some(true), Self::Deviation => Some(false), Self::Skipped => None }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct VerdictReq {
    id: Option<String>,
    status: Option<VerdictStatus>,
    ok: Option<bool>,
    #[serde(default)]
    comment: String,
}

fn verdict(app: &App, body: &str) -> (u16, Value) {
    let req: VerdictReq = match serde_json::from_str(body) {
        Ok(r) => r,
        Err(e) => return (400, json!({"error": e.to_string()})),
    };
    let status = match (req.status, req.ok) {
        (Some(status), None) => status,
        (None, Some(true)) => VerdictStatus::Ok,
        (None, Some(false)) => VerdictStatus::Deviation,
        (Some(status), Some(ok)) if status.legacy_ok() == Some(ok) => status,
        _ => return (400, json!({"error": "give status: ok, deviation or skipped; legacy ok must agree if supplied"})),
    };
    if status != VerdictStatus::Ok && req.comment.trim().is_empty() {
        return (400, json!({"error": "deviation and skipped need a comment"}));
    }
    let mut s = app.state.lock().unwrap();
    if let Some(id) = &req.id {
        if s.attempt.as_ref().and_then(|attempt| attempt["id"].as_str()) != Some(id.as_str()) {
            let entries = read_log(app);
            if entries.iter().any(|entry| entry["event"] == "verdict" && entry["id"] == id.as_str()) {
                return (409, json!({"error": "this attempt already has a verdict"}));
            }
            let Some(ran) = entries.iter().find(|entry| entry["event"] == "ran" && entry["attempt"]["id"] == id.as_str()) else {
                return (404, json!({"error": "no completed attempt with this id"}));
            };
            if status == VerdictStatus::Skipped {
                return (409, json!({"error": "the test ran; record OK or Deviation, not Skipped"}));
            }
            let entry = json!({"event": "verdict", "id": id, "goal": ran["attempt"]["goal"], "status": status, "ok": status.legacy_ok(), "comment": req.comment, "at": now()});
            log(app, entry.clone());
            return (200, entry);
        }
    }
    if s.running {
        return (409, json!({"error": "still running"}));
    }
    if s.attempt.is_none() {
        return (409, json!({"error": "nothing planned"}));
    }
    if status == VerdictStatus::Skipped && s.outcome.is_some() {
        return (409, json!({"error": "the test ran; record OK or Deviation, not Skipped"}));
    }
    if status != VerdictStatus::Skipped && s.outcome.is_none() {
        return (409, json!({"error": "run first"}));
    }
    let a = s.attempt.take().unwrap_or_default();
    if status == VerdictStatus::Ok && s.outcome.as_ref().map(|outcome| outcome["ok"] == true).unwrap_or(false) {
        if let Some(review) = s.review.as_mut().filter(|review| a["review_id"].as_str() == Some(review.id.as_str())) {
            if let Some(index) = a["stage"].as_u64() { review.accepted.push(index as usize); }
        }
    }
    let entry = json!({"event": "verdict", "id": a["id"], "goal": a["goal"], "status": status, "ok": status.legacy_ok(), "comment": req.comment, "at": now()});
    s.work = None;
    s.outcome = None;
    s.progress = None;
    drop(s);
    log(app, entry.clone());
    (200, entry)
}

/// Works mid-burn: uses the stop handle, not the machine lock.
fn stop(app: &App) -> (u16, Value) {
    let state = app.state.lock().unwrap();
    app.cancel_requested.store(true, Ordering::SeqCst);
    let sent = match app.stopper.lock().unwrap().as_mut() {
        Some(s) => s.stop().is_ok(),
        None => false,
    };
    let cancel_requested = state.running;
    drop(state);
    log(app, json!({"event": "stop", "sent": sent, "cancel_requested": cancel_requested, "at": now()}));
    (200, json!({"sent": sent, "cancel_requested": cancel_requested}))
}

fn log(app: &App, mut entry: Value) {
    if let Some(attempt) = entry.get_mut("attempt").and_then(Value::as_object_mut) {
        attempt.remove("job");
        attempt.remove("params");
    }
    let line = format!("{entry}\n");
    let file = OpenOptions::new().create(true).append(true).open(&app.log_path);
    if let Err(e) = file.and_then(|mut f| f.write_all(line.as_bytes())) {
        eprintln!("log write failed: {e}: {line}");
    }
}

fn read_log(app: &App) -> Vec<Value> {
    std::fs::read_to_string(&app.log_path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .map(|mut entry: Value| {
            if entry["event"] == "verdict" && entry.get("status").is_none() {
                if let Some(ok) = entry["ok"].as_bool() {
                    entry["status"] = json!(if ok { "ok" } else { "deviation" });
                }
            }
            entry
        })
        .collect()
}

fn pending_verdicts(app: &App) -> Vec<Value> {
    let entries = read_log(app);
    entries.iter().filter(|entry| entry["event"] == "ran" && !entries.iter().any(|verdict| verdict["event"] == "verdict" && verdict["id"] == entry["attempt"]["id"])).cloned().collect()
}
