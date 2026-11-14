// Copyright (c) 2026 Sean Donnellan
//! Standalone laser service. Web UI and HTTP API. One machine, one job at a time.
//! Every attempt is logged: goal first, then what the software saw, then the operator's verdict.
//!
//! Complete method/path inventory: app/openapi.yaml and the API guide in README.adoc.

pub mod job;
pub mod assets;
pub mod ladder;
pub mod materials;
pub mod test_catalog;
pub mod templates;
mod workflow;
mod prepared;

use job::JobReq;
use base64::Engine;
use ladder::{Params, Step};
use laser::{Info, Machine, Progress, ProgressPhase, ReceiveTrace, Stop};
use materials::Material;
use workflow::{Attempt, AttemptRecord, Outcome, Target, VerdictStatus, Work, Workflow};
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::fmt::Write as FmtWrite;
use std::io::{Read, Write};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::sync::mpsc::{sync_channel, SyncSender};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use tiny_http::{Header, Method, Request, Response, Server};

pub struct Config {
    pub listen: String,
    pub data_dir: String,
    /// k6 or sim.
    pub driver: String,
    pub port_path: String,
    pub materials_path: String,
    pub templates_path: String,
    pub test_catalog_path: String,
    pub audio_analyzer_url: Option<String>,
    pub audio_analyzer_token: Option<String>,
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
            test_catalog_path: env("TEST_CATALOG", "tests.toml"),
            audio_analyzer_url: std::env::var("AUDIO_ANALYZER_URL").ok().filter(|url| !url.trim().is_empty()),
            audio_analyzer_token: std::env::var("AUDIO_ANALYZER_TOKEN").ok().filter(|token| !token.is_empty()),
        }
    }
}

#[derive(Default)]
struct State {
    workflow: Workflow,
    progress: Option<Progress>,
    band_timings: Vec<Progress>,
    receive_trace: Option<Value>,
    capture_receive_trace: bool,
    review: Option<Review>,
}

#[derive(Clone)]
struct Review {
    id: String,
    job: JobReq,
    stages: Vec<Arc<job::Stage>>,
    accepted: Vec<usize>,
    raster_generation_ms: u64,
}

struct App {
    driver: String,
    port_path: String,
    info: Info,
    simulated: AtomicBool,
    cancel_requested: AtomicBool,
    materials: Vec<Material>,
    templates: Vec<templates::Template>,
    test_catalog: Vec<test_catalog::TestDefinition>,
    log_path: String,
    audio_analyzer_url: Option<String>,
    audio_analyzer_token: Option<String>,
    state: Mutex<State>,
    executor: SyncSender<DeviceCommand>,
    stopper: Mutex<Option<Box<dyn Stop>>>,
}

enum DeviceCommand { Run(Attempt), Reset(SyncSender<()>) }

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
    let test_catalog = test_catalog::load(&cfg.test_catalog_path, &info)?;
    std::fs::create_dir_all(&cfg.data_dir).map_err(|e| format!("{}: {e}", cfg.data_dir))?;
    let server = Server::http(&cfg.listen).map_err(|e| format!("bind {}: {e}", cfg.listen))?;
    let addr = server.server_addr().to_ip().ok_or("not an IP listener")?;
    let (executor, commands) = sync_channel(1);
    let app = Arc::new(App {
        info,
        simulated: AtomicBool::new(true),
        cancel_requested: AtomicBool::new(false),
        materials,
        templates,
        test_catalog,
        driver: cfg.driver,
        port_path: cfg.port_path,
        log_path: format!("{}/runs.jsonl", cfg.data_dir),
        audio_analyzer_url: cfg.audio_analyzer_url,
        audio_analyzer_token: cfg.audio_analyzer_token,
        state: Mutex::new(State::default()),
        executor,
        stopper: Mutex::new(None),
    });
    let worker_app = app.clone();
    std::thread::spawn(move || {
        let mut machine = None;
        for command in commands {
            match command {
                DeviceCommand::Reset(done) => {
                    machine = None;
                    *worker_app.stopper.lock().unwrap_or_else(|error| error.into_inner()) = None;
                    let _ = done.send(());
                }
                DeviceCommand::Run(attempt) => {
                    let started = now();
                    let operation_started = Instant::now();
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| execute(&worker_app, &attempt.work, &mut machine)))
                        .unwrap_or_else(|_| Err("execution panicked; confirm physical rest before further work".into()));
                    if result.is_err() {
                        machine = None;
                        *worker_app.stopper.lock().unwrap_or_else(|error| error.into_inner()) = None;
                    }
                    let (timings, receive_trace) = {
                        let mut state = worker_app.state.lock().unwrap();
                        state.capture_receive_trace = false;
                        (state.band_timings.iter().copied().map(progress_json).collect(), state.receive_trace.clone())
                    };
                    let outcome = Outcome { ok:result.is_ok(), saw:match result { Ok(message) | Err(message) => message }, started, ended:now(), elapsed_ms:operation_started.elapsed().as_millis() as u64, band_timings:timings, receive_trace };
                    log(&worker_app, json!({"event":"ran","attempt":attempt.record,"outcome":outcome}));
                    if let Err(error) = worker_app.state.lock().unwrap().workflow.finish(&attempt.record.id, outcome) { eprintln!("workflow completion rejected: {error}"); }
                    if attempt.record.mode == "real" && worker_app.audio_analyzer_url.is_some() && worker_app.audio_analyzer_token.is_some() {
                        let analysis_app = worker_app.clone();
                        let attempt_id = attempt.record.id.clone();
                        std::thread::spawn(move || { let _ = analyze_run_log(&analysis_app, &attempt_id); });
                    }
                }
            }
        }
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
    if req.method() == &Method::Post {
        if let Some(attempt_id) = req.url().strip_prefix("/api/audio-analyses/").and_then(|path| path.strip_suffix("/report")).map(str::to_owned) {
            match analyze_run_log(app, &attempt_id) {
                Ok(result) => respond_json(req, 200, result),
                Err((status, error)) => respond_json(req, status, json!({"error":error})),
            }
            return;
        }
    }
    if req.method() == &Method::Get && req.url() == "/api/audio-analyses/availability" {
        audio_analyzer_availability(app, req);
        return;
    }
    if req.method() == &Method::Post && req.url() == "/api/audio-analyses" {
        upload_audio(app, req);
        return;
    }
    if req.method() == &Method::Get {
        let url = req.url().to_string();
        if let Some(attempt_id) = url.strip_prefix("/audio-upload/") {
            audio_upload_page(app, req, attempt_id.to_string());
            return;
        }
        if let Some(attempt_id) = url.strip_prefix("/api/audio-analyses/").and_then(|path| path.strip_suffix("/upload-qr.svg")) {
            audio_upload_qr(app, req, attempt_id.to_string());
            return;
        }
        if let Some(attempt_id) = url.strip_prefix("/api/audio-analyses/").and_then(|path| path.strip_suffix("/report")) {
            get_audio_report(app, req, attempt_id.to_string());
            return;
        }
    }
    let mut body = String::new();
    let _ = req.as_reader().read_to_string(&mut body);
    let (code, out) = match (req.method(), req.url()) {
        (Method::Get, "/examples/audio-analysis") => {
            let html = Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
            let cache = Header::from_bytes("Cache-Control", "public, max-age=3600").unwrap();
            let csp = Header::from_bytes("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; media-src data: blob:; img-src data:").unwrap();
            let _ = req.respond(Response::from_string(include_str!("../assets/audio-analysis-example.html")).with_header(html).with_header(cache).with_header(csp));
            return;
        }
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
        (Method::Get, "/manifest.webmanifest") => {
            let content_type = Header::from_bytes("Content-Type", "application/manifest+json; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_string(include_str!("../manifest.webmanifest")).with_header(content_type));
            return;
        }
        (Method::Get, "/") => {
            let html = Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
            let _ = req.respond(Response::from_string(include_str!("ui.html")).with_header(html));
            return;
        }
        (Method::Get, "/api/machine") => (200, machine(&app.info)),
        (Method::Get, "/api/health") => (200, health(app)),
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
        (Method::Get, "/api/steps") => (200, steps(app)),
        (Method::Get, "/api/state") => (200, state(app)),
        (Method::Delete, "/api/state") => clear_state(app),
        (Method::Get, "/api/attempt/preview") => active_preview(app),
        (Method::Get, "/api/log") => (200, json!(read_log(app))),
        (Method::Get, "/api/pending-verdicts") => (200, json!(pending_verdicts(app))),
        (Method::Post, "/api/jobs/preview") => preview(app, &body),
        (Method::Post, "/api/jobs/review") => submit_review(app, &body),
        (Method::Get, "/api/jobs/review") => current_review(app),
        (Method::Post, "/api/jobs/form") => edit_form(app, &body),
        (Method::Delete, "/api/jobs/review") => discard_review(app),
        (Method::Post, "/api/tests/preview") => test_preview(app, &body),
        (Method::Post, "/api/plan") => plan(app, &body),
        (Method::Post, "/api/run") => run(app, &body),
        (Method::Post, "/api/verdict") => verdict(app, &body),
        (Method::Post, "/api/stop") => stop(app),
        _ => (404, json!({"error": "not found"})),
    };
    let ct = Header::from_bytes("Content-Type", "application/json").unwrap();
    let cache = Header::from_bytes("Cache-Control", "no-store").unwrap();
    let _ = req.respond(Response::from_string(out.to_string()).with_status_code(code).with_header(ct).with_header(cache));
}

const MAX_AUDIO_UPLOAD_BYTES: usize = 64 * 1024 * 1024;
const MAX_ANALYSIS_REPORT_BYTES: usize = 56 * 1024 * 1024;

fn audio_analyzer_availability(app: &App, req: Request) {
    let available = analyzer_is_available(app);
    respond_json(req, 200, json!({"available":available}));
}

fn analyzer_is_available(app: &App) -> bool {
    match (app.audio_analyzer_url.as_deref(), app.audio_analyzer_token.as_deref()) {
        (Some(url), Some(token)) => url.strip_suffix("/api/analyze").is_some_and(|base| {
            ureq::AgentBuilder::new()
                .timeout_connect(std::time::Duration::from_secs(1))
                .timeout_read(std::time::Duration::from_secs(1))
                .build()
                .get(&format!("{base}/health"))
                .set("Authorization", &format!("Bearer {token}"))
                .call()
                .is_ok_and(|response| response.status() == 200)
        }),
        _ => false,
    }
}

fn valid_attempt_id(attempt_id: &str) -> bool {
    !attempt_id.is_empty() && attempt_id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn analyze_run_log(app: &App, attempt_id: &str) -> Result<Value, (u16, &'static str)> {
    if !valid_attempt_id(attempt_id) { return Err((400, "invalid attempt ID")); }
    let entries = read_log(app);
    let ran = entries.iter().find(|entry| entry["event"] == "ran" && entry["attempt"]["id"] == attempt_id && entry["attempt"]["mode"] == "real")
        .ok_or((409, "analysis requires a completed Real run"))?;
    if let Some(existing) = entries.iter().rev().find(|entry| entry["event"] == "audio_analysis" && entry["attempt_id"] == attempt_id && entry["report_url"].is_string()) {
        return Ok(existing.clone());
    }
    let (Some(url), Some(token)) = (app.audio_analyzer_url.as_deref(), app.audio_analyzer_token.as_deref()) else {
        return Err((503, "analysis container is not configured"));
    };
    if !analyzer_is_available(app) { return Err((503, "analysis container is unavailable")); }
    let started = Instant::now();
    let response = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(2))
        .timeout_read(std::time::Duration::from_secs(15))
        .build().post(url)
        .set("Content-Type", "application/json")
        .set("Authorization", &format!("Bearer {token}"))
        .send_string(&json!({"attempt_id":attempt_id,"run_log":[ran]}).to_string())
        .map_err(|_| (502, "analysis request failed"))?;
    if !response.header("Content-Type").is_some_and(|value| value.starts_with("text/html")) {
        return Err((502, "analysis container did not return HTML"));
    }
    let timing: Value = response.header("X-Audio-Timing").and_then(|value| serde_json::from_str(value).ok()).unwrap_or_else(|| json!({}));
    let mut report = Vec::new();
    response.into_reader().take((MAX_ANALYSIS_REPORT_BYTES + 1) as u64).read_to_end(&mut report).map_err(|_| (502, "could not read analysis report"))?;
    if report.is_empty() || report.len() > MAX_ANALYSIS_REPORT_BYTES { return Err((502, "invalid analysis report size")); }
    let result = json!({"event":"audio_analysis","attempt_id":attempt_id,"report_url":format!("/api/audio-analyses/{attempt_id}/report"),"has_audio":false,"timing":timing,"pi_total_ms":started.elapsed().as_millis() as u64,"at":now()});
    log(app, result.clone());
    Ok(result)
}

fn request_header<'a>(req: &'a Request, name: &str) -> Option<&'a str> {
    req.headers().iter().find(|header| header.field.to_string().eq_ignore_ascii_case(name)).map(|header| header.value.as_str())
}

fn audio_upload_events(app: &App, attempt_id: &str) -> Option<(String, Value)> {
    let entries = read_log(app);
    let ran = entries.iter().find(|entry| entry["event"] == "ran" && entry["attempt"]["id"] == attempt_id)?;
    if ran["attempt"]["mode"] != "real" { return None; }
    let expected_id = attempt_recording_id(attempt_id)?;
    let marker = entries.iter().find(|entry| entry["event"] == "sync_marker" && entry["attempt_id"] == attempt_id && entry["recording_id"] == expected_id)?;
    Some((expected_id, json!([marker, ran])))
}

fn audio_upload_page(app: &App, req: Request, attempt_id: String) {
    if !valid_attempt_id(&attempt_id) || audio_upload_events(app, &attempt_id).is_none() {
        respond_json(req, 404, json!({"error":"upload link is not valid for a completed Real attempt"}));
        return;
    }
    let attempt_json = serde_json::to_string(&attempt_id).unwrap();
    let html = format!(r#"<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Upload Voice Memo</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:640px;margin:24px auto;padding:0 18px;color:#172633;background:#f8f9fc;line-height:1.5}}
h1{{font-size:24px}} label{{display:block;margin:20px 0}} input,button{{font:inherit;min-height:48px;max-width:100%}} button{{padding:8px 16px}} .meta{{color:#566978;font-size:13px}} #status{{min-height:48px}} a{{overflow-wrap:anywhere}}
</style>
<main>
<h1>Upload Voice Memo</h1>
<p class="meta">Attempt: <code>{attempt_id}</code></p>
<p>In Voice Memos, share the recording to Files first. Then choose the exported .m4a below.</p>
<label>Voice Memo <input id="recording" type="file" accept=".m4a,audio/mp4,.wav,audio/wav" required></label>
<button id="upload" type="button" disabled>Upload recording</button>
<p id="status" role="status" aria-live="polite"></p>
<a id="report" hidden>Open run analysis</a>
</main>
<script>
const attemptId={attempt_json}, input=document.getElementById('recording'), button=document.getElementById('upload'), status=document.getElementById('status'), report=document.getElementById('report');
input.addEventListener('change',()=>{{button.disabled=!input.files||!input.files.length;}});
button.addEventListener('click',async()=>{{const file=input.files&&input.files[0];if(!file)return;if(file.size>64*1024*1024){{status.textContent='Recording exceeds the 64 MiB limit.';return;}}const type=file.type||(file.name.toLowerCase().endsWith('.wav')?'audio/wav':'audio/mp4');button.disabled=true;status.textContent='Uploading and analyzing…';try{{const response=await fetch('/api/audio-analyses',{{method:'POST',headers:{{'Content-Type':type,'X-Attempt-ID':attemptId}},body:file}});const result=await response.json();if(!response.ok)throw new Error(result.error||('HTTP '+response.status));status.textContent='Upload complete. The annotated run report is ready.';report.href=result.report_url;report.hidden=false;}}catch(error){{status.textContent='Upload failed: '+error.message;button.disabled=false;}}}});
</script></html>"#);
    let content_type = Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
    let cache = Header::from_bytes("Cache-Control", "no-store").unwrap();
    let csp = Header::from_bytes("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'").unwrap();
    let _ = req.respond(Response::from_string(html).with_header(content_type).with_header(cache).with_header(csp));
}

fn audio_upload_qr(app: &App, req: Request, attempt_id: String) {
    if !valid_attempt_id(&attempt_id) || audio_upload_events(app, &attempt_id).is_none() {
        respond_json(req, 404, json!({"error":"upload link is not valid for a completed Real attempt"}));
        return;
    }
    let host = request_header(&req, "Host").unwrap_or("");
    if host.is_empty() || host.len() > 255 || !host.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b':' | b'[' | b']')) {
        respond_json(req, 400, json!({"error":"invalid request host"}));
        return;
    }
    let upload_url = format!("http://{host}/audio-upload/{attempt_id}");
    let raster = match compose::qr::qr(&upload_url, 800) {
        Ok(raster) => raster,
        Err(_) => { respond_json(req, 500, json!({"error":"could not create upload QR"})); return; }
    };
    let content_type = Header::from_bytes("Content-Type", "image/svg+xml; charset=utf-8").unwrap();
    let cache = Header::from_bytes("Cache-Control", "no-store").unwrap();
    let _ = req.respond(Response::from_string(raster_svg(&raster)).with_header(content_type).with_header(cache));
}

fn respond_json(req: Request, status: u16, value: Value) {
    let content_type = Header::from_bytes("Content-Type", "application/json; charset=utf-8").unwrap();
    let cache = Header::from_bytes("Cache-Control", "no-store").unwrap();
    let _ = req.respond(Response::from_string(value.to_string()).with_status_code(status).with_header(content_type).with_header(cache));
}

fn append_multipart_field(body: &mut Vec<u8>, boundary: &str, name: &str, content_type: &str, filename: Option<&str>, value: &[u8]) {
    body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"").as_bytes());
    if let Some(filename) = filename { body.extend_from_slice(format!("; filename=\"{filename}\"").as_bytes()); }
    body.extend_from_slice(format!("\r\nContent-Type: {content_type}\r\n\r\n").as_bytes());
    body.extend_from_slice(value);
    body.extend_from_slice(b"\r\n");
}

fn upload_audio(app: &Arc<App>, mut req: Request) {
    let pi_total_started = Instant::now();
    let attempt_id = request_header(&req, "X-Attempt-ID").map(str::to_owned).filter(|id| valid_attempt_id(id));
    let Some(attempt_id) = attempt_id else {
        respond_json(req, 400, json!({"error":"valid X-Attempt-ID header required"}));
        return;
    };
    let content_type = request_header(&req, "Content-Type").unwrap_or("").to_ascii_lowercase();
    let (suffix, audio_type) = if content_type.starts_with("audio/mp4") || content_type.starts_with("audio/m4a") || content_type.starts_with("audio/x-m4a") {
        ("m4a", "audio/mp4")
    } else if content_type.starts_with("audio/wav") || content_type.starts_with("audio/wave") {
        ("wav", "audio/wav")
    } else {
        respond_json(req, 415, json!({"error":"upload must be audio/mp4 or audio/wav"}));
        return;
    };
    let mut audio = Vec::new();
    let pi_receive_started = Instant::now();
    if req.as_reader().take((MAX_AUDIO_UPLOAD_BYTES + 1) as u64).read_to_end(&mut audio).is_err() {
        respond_json(req, 400, json!({"error":"could not read audio upload"}));
        return;
    }
    if audio.is_empty() || audio.len() > MAX_AUDIO_UPLOAD_BYTES {
        respond_json(req, 413, json!({"error":"audio must be non-empty and no larger than 64 MiB"}));
        return;
    }
    let pi_receive_ms = pi_receive_started.elapsed().as_millis() as u64;
    let (Some(analyzer_url), Some(analyzer_token)) = (app.audio_analyzer_url.as_deref(), app.audio_analyzer_token.as_deref()) else {
        respond_json(req, 503, json!({"error":"audio analyzer is not configured on the Pi"}));
        return;
    };
    let pi_prepare_started = Instant::now();
    let (expected_id, run_log) = {
        let state = app.state.lock().unwrap();
        if state.workflow.running() {
            respond_json(req, 409, json!({"error":"wait for the run to finish before uploading"}));
            return;
        }
        let entries = read_log(app);
        let ran = entries.iter().find(|entry| entry["event"] == "ran" && entry["attempt"]["id"] == attempt_id);
        if ran.is_none_or(|entry| entry["attempt"]["mode"] != "real") {
            respond_json(req, 409, json!({"error":"upload requires a completed Real attempt"}));
            return;
        }
        let Some(expected_id) = attempt_recording_id(&attempt_id) else {
            respond_json(req, 409, json!({"error":"attempt has no recording ID"}));
            return;
        };
        let marker = entries.iter().find(|entry| entry["event"] == "sync_marker" && entry["attempt_id"] == attempt_id && entry["recording_id"] == expected_id);
        let Some(marker) = marker else {
            respond_json(req, 409, json!({"error":"matching sync marker is required"}));
            return;
        };
        (expected_id, json!([marker, ran.unwrap()]))
    };
    let boundary = format!("wainlux-{}", now_ms());
    let mut body = Vec::with_capacity(audio.len() + 8192);
    append_multipart_field(&mut body, &boundary, "audio", audio_type, Some(&format!("recording.{suffix}")), &audio);
    append_multipart_field(&mut body, &boundary, "attempt_id", "text/plain; charset=utf-8", None, attempt_id.as_bytes());
    append_multipart_field(&mut body, &boundary, "expected_id", "text/plain; charset=utf-8", None, expected_id.as_bytes());
    let run_log = run_log.to_string();
    append_multipart_field(&mut body, &boundary, "run_log", "application/json", None, run_log.as_bytes());
    body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    let pi_prepare_ms = pi_prepare_started.elapsed().as_millis() as u64;
    let analyzer_started = Instant::now();
    let response = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout_read(std::time::Duration::from_secs(930))
        .build()
        .post(analyzer_url)
        .set("Content-Type", &format!("multipart/form-data; boundary={boundary}"))
        .set("Authorization", &format!("Bearer {analyzer_token}"))
        .send_bytes(&body);
    let response = match response {
        Ok(response) => response,
        Err(ureq::Error::Status(_, response)) => {
            let status = response.status();
            respond_json(req, 502, json!({"error":format!("audio analyzer returned HTTP {status}")}));
            return;
        }
        Err(_) => {
            respond_json(req, 502, json!({"error":"audio analyzer request failed"}));
            return;
        }
    };
    let mut analyzer_timing: Value = response.header("X-Audio-Timing").and_then(|value| serde_json::from_str(value).ok()).unwrap_or_else(|| json!({}));
    if !response.header("Content-Type").is_some_and(|value| value.to_ascii_lowercase().starts_with("text/html")) {
        respond_json(req, 502, json!({"error":"audio analyzer did not return an HTML report"}));
        return;
    }
    let mut report = Vec::new();
    if response.into_reader().take((MAX_ANALYSIS_REPORT_BYTES + 1) as u64).read_to_end(&mut report).is_err() || report.len() > MAX_ANALYSIS_REPORT_BYTES {
        respond_json(req, 502, json!({"error":"audio report is invalid or too large"}));
        return;
    }
    analyzer_timing["pi_receive_ms"] = json!(pi_receive_ms);
    analyzer_timing["pi_prepare_ms"] = json!(pi_prepare_ms);
    analyzer_timing["analyzer_http_ms"] = json!(analyzer_started.elapsed().as_millis() as u64);
    analyzer_timing["pi_total_ms"] = json!(pi_total_started.elapsed().as_millis() as u64);
    let report_url = format!("/api/audio-analyses/{attempt_id}/report");
    log(app, json!({"event":"audio_analysis","attempt_id":attempt_id,"report_url":report_url,"has_audio":true,"timing":analyzer_timing.clone(),"at":now()}));
    respond_json(req, 200, json!({"report_url":report_url,"timing":analyzer_timing}));
}

fn get_audio_report(app: &App, req: Request, attempt_id: String) {
    if !valid_attempt_id(&attempt_id) {
        respond_json(req, 404, json!({"error":"report not found"}));
        return;
    }
    let (Some(analyzer_url), Some(analyzer_token)) = (app.audio_analyzer_url.as_deref(), app.audio_analyzer_token.as_deref()) else {
        respond_json(req, 503, json!({"error":"audio analyzer is not configured on the Pi"}));
        return;
    };
    let Some(base_url) = analyzer_url.strip_suffix("/api/analyze") else {
        respond_json(req, 503, json!({"error":"audio analyzer URL must end with /api/analyze"}));
        return;
    };
    let report_url = format!("{base_url}/api/reports/{attempt_id}/report");
    let response = ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(5))
        .timeout_read(std::time::Duration::from_secs(10))
        .build()
        .get(&report_url)
        .set("Authorization", &format!("Bearer {analyzer_token}"))
        .call();
    let response = match response {
        Ok(response) => response,
        Err(ureq::Error::Status(status, _)) => {
            respond_json(req, if status == 404 { 404 } else { 502 }, json!({"error":format!("audio analyzer returned HTTP {status}")}));
            return;
        }
        Err(_) => {
            respond_json(req, 503, json!({"error":"audio analyzer is unavailable"}));
            return;
        }
    };
    if !response.header("Content-Type").is_some_and(|value| value.to_ascii_lowercase().starts_with("text/html")) {
        respond_json(req, 502, json!({"error":"audio analyzer did not return an HTML report"}));
        return;
    }
    let mut report = Vec::new();
    if response.into_reader().take((MAX_ANALYSIS_REPORT_BYTES + 1) as u64).read_to_end(&mut report).is_err() || report.len() > MAX_ANALYSIS_REPORT_BYTES {
        respond_json(req, 502, json!({"error":"audio report is invalid or too large"}));
        return;
    }
    let html = Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
    let cache = Header::from_bytes("Cache-Control", "no-store").unwrap();
    let csp = Header::from_bytes("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; media-src data: blob:; img-src data:").unwrap();
    let _ = req.respond(Response::from_data(report).with_header(html).with_header(cache).with_header(csp));
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

fn health(app: &App) -> Value {
    json!({
        "status": "healthy",
        "timestamp": now(),
        "model": app.info.model,
        "driver": app.driver,
        "simulated": app.simulated.load(Ordering::SeqCst),
        "port_path": app.port_path,
    })
}

fn materials_json(app: &App) -> Value {
    json!(app.materials.iter().map(|m| json!({
        "material": m,
        "values": m.values(&app.info).map(|v| json!(v)).unwrap_or_else(|e| json!({"error": e})),
    })).collect::<Vec<_>>())
}

fn prepare_test(app: &App, test: &test_catalog::TestDefinition, mut params: Params, material: Option<&str>) -> Result<(Params, String), String> {
    if let Some(id) = material { materials::apply(&mut params.values, id, &app.materials, &app.info)?; }
    if let Some(x) = test.fixed_x_mm { params.x_mm = x; }
    if let Some(y) = test.fixed_y_mm { params.y_mm = y; }
    if let Some(size) = test.fixed_size_mm { params.size_mm = size; }
    params.power_values = test.power_values_for(&app.info).to_vec();
    if let Some(power) = params.power_values.first() {
        params.values.insert("power".into(), *power);
    }
    let goal = test.step.goal(&params, &app.info)?;
    Ok((params, goal))
}

fn steps(app: &App) -> Value {
    let p = Params::default();
    let definitions = app.test_catalog.iter()
        .filter(|test| test.step.supported(&app.info))
        .map(|test| {
            let goal = prepare_test(app, test, p.clone(), None).map(|(_, goal)| goal).unwrap_or_else(|error| error);
            json!({
                "step": test.step,
                "title": test.title,
                "goal": goal,
                "controls": test.controls,
                "settings": test.settings,
                "materials": test.materials,
                "requires_real": test.requires_real,
                "requires_analyzer": test.requires_analyzer,
                "requires_sync_marker": test.requires_sync_marker,
                "requires_recording_ack": test.requires_recording_ack,
                "stock_confirmation": test.stock_confirmation,
                "fixed_x_mm": test.fixed_x_mm,
                "fixed_y_mm": test.fixed_y_mm,
                "fixed_size_mm": test.fixed_size_mm,
                "power_values": test.power_values_for(&app.info),
            })
        })
        .collect::<Vec<_>>();
    json!({
        "machine": machine(&app.info),
        "defaults": p,
        "steps": definitions,
    })
}

fn state(app: &App) -> Value {
    let s = app.state.lock().unwrap();
    json!({"phase":s.workflow.phase(), "attempt": s.workflow.attempt().map(Attempt::view), "running": s.workflow.running(), "progress": s.progress.map(progress_json), "band_timings": s.band_timings.iter().copied().map(progress_json).collect::<Vec<_>>(), "receive_trace": s.receive_trace, "outcome": s.workflow.outcome(), "mode": mode(app), "review_id":s.review.as_ref().map(|review| &review.id), "accepted_stages":s.review.as_ref().map(|review| &review.accepted)})
}

fn clear_state(app: &App) -> (u16, Value) {
    let mut state = app.state.lock().unwrap();
    if state.workflow.running() {
        return (409, json!({"error":"cannot clear Pi state while an attempt is running"}));
    }
    let attempt_id = state.workflow.attempt().map(|attempt| attempt.record.id.clone());
    if let Err(error) = reset_device(app) { return (503, json!({"error":error})); }
    *state = State::default();
    app.cancel_requested.store(false, Ordering::SeqCst);
    drop(state);
    log(app, json!({"event":"clear","attempt_id":attempt_id,"at":now()}));
    (200, json!({"cleared":true,"run_log_retained":true}))
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

fn receive_trace_json(trace: ReceiveTrace) -> Value {
    json!({
        "bytes_captured": trace.bytes_captured,
        "truncated": trace.truncated,
        "duration_ms": trace.duration_ms,
        "started_at_unix_ms": trace.started_at_unix_ms,
        "events": trace.events.into_iter().map(|event| json!({
            "at_ms": event.at_ms,
            "job_index": event.job_index,
            "phase": event.phase,
            "kind": event.kind,
            "byte": event.byte.map(|byte| format!("{byte:02X}")),
            "frame": event.frame,
            "frame_bytes": event.frame_bytes.map(|bytes| bytes.iter().map(|byte| format!("{byte:02X}")).collect::<Vec<_>>().join(" ")),
            "percent": event.percent,
            "quiet_ms": event.quiet_ms,
            "metadata": event.metadata,
        })).collect::<Vec<_>>(),
    })
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
    if !state.workflow.idle() {
        return (409, json!({"error": "finish or skip the planned attempt before changing mode"}));
    }
    if req.real && app.driver == "sim" {
        return (400, json!({"error": "this installation has no real driver configured"}));
    }
    if let Err(error) = reset_device(app) { return (503, json!({"error":error})); }
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
    let generation_started = Instant::now();
    let result = job::stages(&job, &app.materials, &app.info);
    let raster_generation_ms = generation_started.elapsed().as_millis() as u64;
    let stages = match result {
        Ok(stages) => stages.into_iter().map(Arc::new).collect(),
        Err(error) => return (400, json!({"error":error})),
    };
    let id = format!("review-{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos());
    let review = Review {id, job, stages, accepted:vec![], raster_generation_ms};
    let value = match review_json(app, &review) {
        Ok(value) => value,
        Err(error) => return (500, json!({"error":error})),
    };
    let mut state = app.state.lock().unwrap();
    if !state.workflow.idle() { return (409, json!({"error":"finish the active attempt before replacing the review"})); }
    if expected_review.as_ref().map(|expected| state.review.as_ref().map(|review| &review.id) != expected.as_ref()).unwrap_or(false) { return (409, json!({"error":"review changed; reload before editing"})); }
    state.review = Some(review);
    (200, value)
}

fn review_json(app: &App, review: &Review) -> Result<Value, String> {
    let placement = job::placement(&review.job, &app.materials, &app.info)?;
    let preview_started = Instant::now();
    let mut value = job_preview(app, &review.job, placement, review.stages.iter().map(Arc::as_ref))?;
    let preview_encoding_ms = preview_started.elapsed().as_millis() as u64;
    value["timing"] = json!({"raster_generation_ms":review.raster_generation_ms,"preview_encoding_ms":preview_encoding_ms});
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
    if !state.workflow.idle() { return (409, json!({"error":"finish the active attempt first"})); }
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
        (state.workflow.attempt().map(|attempt| attempt.record.clone()), state.workflow.attempt().map(|attempt| attempt.work.clone()))
    };
    let (Some(attempt), Some(Work::Job(job, stage, _))) = (attempt, work) else {
        return (404, json!({"error":"no card attempt to preview"}));
    };
    match job::placement(&job, &app.materials, &app.info) {
        Ok(placement) => {
            let mut preview = match job_preview(app, &job, placement, std::iter::once(stage.as_ref())) {
                Ok(preview) => preview,
                Err(error) => return (500, json!({"error":error})),
            };
            preview["attempt_id"] = json!(attempt.id);
            (200, preview)
        }
        Err(error) => (400, json!({"error":error})),
    }
}

fn rect_json(rect: laser::Rect) -> Value {
    json!({"x": r2(rect.x), "y": r2(rect.y), "w": r2(rect.w), "h": r2(rect.h)})
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

fn test_preview(app: &App, body: &str) -> (u16, Value) {
    let req: PlanReq = match serde_json::from_str(body) {
        Ok(req) => req,
        Err(error) => return (400, json!({"error": error.to_string()})),
    };
    let Some(step) = req.step else { return (400, json!({"error": "step required"})); };
    let Some(test) = app.test_catalog.iter().find(|test| test.step == step) else {
        return (400, json!({"error": "test is not in the startup catalog"}));
    };
    let result = prepare_test(app, test, req.params, req.material.as_deref()).and_then(|(params, goal)| {
        let raster = ladder::preview_raster(step, &params, &app.info)?;
        let svg = raster.as_ref().map(|raster| raster_svg(raster));
        let note = match step {
            Step::Frame => "Schematic of the laser-off BOUNDS frame; not a live projection or burn result.",
            Step::Registration => "Composite of both reviewed passes; pass B includes the long leading line.",
            Step::VerticalComparison => "Exact composite of the three separately homed A/B/C jobs. D is an independent follow-up test; inspect only after the supervised run stops.",
            Step::PowerSweep => "Exact composite pixels of the frame and nine separate line jobs; per-job powers and bed placement are in the goal. The frame's top edge replaces the first full-power line. Power 0 is not confirmed laser-off; every mark may be active.",
            Step::AudioPowerSweepLow => "Projection of one 20 mm box and nine fixed-power lines. Every mark is at the minimum nonzero power; nominal no-mark is not guaranteed.",
            Step::AudioPowerSweepFull => "Projection of one 20 mm box and nine fixed-power lines. Every mark is at full power; marking is expected.",
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
        if req.step.is_some() { return (400, json!({"error":"review_id cannot be combined with step"})); }
        let mut state = app.state.lock().unwrap();
        if !state.workflow.idle() { return (409, json!({"error":"finish the active attempt first"})); }
        let Some(review) = state.review.as_ref().filter(|review| review.id == id) else {
            return (409, json!({"error":"review is missing or stale; preview again"}));
        };
        let Some(stage) = review.stages.get(req.stage).cloned() else { return (400, json!({"error":"invalid review stage"})); };
        if (0..req.stage).any(|index| !review.accepted.contains(&index)) { return (409, json!({"error":"accept previous stage observations before continuing"})); }
        if stage.side == Some(job::Side::Back) && !req.placement_confirmed { return (409, json!({"error":"confirm the card is flipped and the back is placed correctly"})); }
        let prepared = match job::prepare(&stage, &app.info) { Ok(prepared) => Arc::new(prepared), Err(error) => return (400, json!({"error":error})) };
        let work = Work::Job(review.job.clone(), stage.clone(), prepared);
        let record = AttemptRecord { id:format!("{}-{}",SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos(),stage.name), goal:stage.goal.clone(), machine:app.info.model.into(), mode:mode(app)["mode"].as_str().unwrap().into(), planned:now(), target:Target::Card { review_id:id, stage:req.stage, stage_name:stage.name.clone(), template:review.job.template.clone() } };
        let planned = Attempt { record, work };
        let attempt = planned.view();
        state.review.as_mut().unwrap().accepted.retain(|index| *index < req.stage);
        if let Err(error) = state.workflow.plan(planned) { return (409, json!({"error":error})); }
        return (200, attempt);
    }
    let Some(step) = req.step else { return (400, json!({"error":"step required"})); };
    let Some(definition) = app.test_catalog.iter().find(|definition| definition.step == step) else {
        return (400, json!({"error":"test is not in the startup catalog"}));
    };
    if definition.requires_analyzer && !analyzer_is_available(app) {
        return (503, json!({"error":"audio analyzer is unavailable"}));
    }
    let (params, goal) = match prepare_test(app, definition, req.params, req.material.as_deref()) {
        Ok(prepared) => prepared,
        Err(e) => return (400, json!({"error": e})),
    };
    let prepared = match step.prepare(&params, &app.info) { Ok(prepared) => Arc::new(prepared), Err(error) => return (400, json!({"error":error})) };
    let work = Work::Step(step, params, prepared);
    let mut s = app.state.lock().unwrap();
    if definition.requires_real && app.simulated.load(Ordering::SeqCst) { return (409, json!({"error":"this test requires Real mode"})); }
    let record = AttemptRecord { id:format!("{}-{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos(), serde_json::to_value(step).unwrap().as_str().unwrap()), goal, machine:app.info.model.into(), mode:mode(app)["mode"].as_str().unwrap().into(), planned:now(), target:Target::Test { step, material:req.material } };
    let planned = Attempt { record, work };
    let attempt = planned.view();
    if let Err(error) = s.workflow.plan(planned) { return (409, json!({"error":error})); }
    (200, attempt)
}

const DEFAULT_SYNC_CUE_POST_LEAD_MS: u16 = 700;
const SYNC_CUE_DURATION_MS: u16 = 2000;

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct RunReq {
    attempt_id: Option<String>,
    #[serde(default)]
    sync_marker: bool,
    recording_id: Option<String>,
    cue_duration_ms: Option<u16>,
    post_cue_lead_ms: Option<u16>,
}

fn run(app: &Arc<App>, body: &str) -> (u16, Value) {
    let req: RunReq = if body.trim().is_empty() {
        RunReq::default()
    } else {
        match serde_json::from_str(body) {
            Ok(req) => req,
            Err(error) => return (400, json!({"error": error.to_string()})),
        }
    };
    let post_cue_lead_ms = req.post_cue_lead_ms.unwrap_or(DEFAULT_SYNC_CUE_POST_LEAD_MS);
    if req.sync_marker {
        let Some(recording_id) = req.recording_id.as_deref() else { return (400, json!({"error":"recording_id is required when sync_marker is true"})); };
        if recording_id.len() != 6 || !recording_id.bytes().all(|byte| byte.is_ascii_digit()) {
            return (400, json!({"error":"recording_id must be six decimal digits"}));
        }
        if req.cue_duration_ms != Some(SYNC_CUE_DURATION_MS) || post_cue_lead_ms > 2000 {
            return (400, json!({"error":"sync cue duration must be 2000 ms and post-cue lead must be 0..=2000 ms"}));
        }
    }
    let attempt = {
        let mut s = app.state.lock().unwrap();
        let Some(planned) = s.workflow.attempt() else {
            return (409, json!({"error": "plan first"}));
        };
        let a = &planned.record;
        let requires_sync_marker = a.step().and_then(|step| app.test_catalog.iter().find(|definition| definition.step == step)).map(|definition| definition.requires_sync_marker).unwrap_or(false);
        if requires_sync_marker && !req.sync_marker {
            return (409, json!({"error":"audio-recorded power sweep requires its recording-ID sync marker"}));
        }
        if req.sync_marker {
            let expected = attempt_recording_id(&a.id);
            if expected.as_deref() != req.recording_id.as_deref() {
                return (409, json!({"error":"recording_id does not match the planned attempt"}));
            }
        }
        let capture_trace = attempt_receive_trace_enabled(a.step(), req.sync_marker) || (app.audio_analyzer_url.is_some() && app.audio_analyzer_token.is_some());
        let started = match s.workflow.start(req.attempt_id.as_deref()) { Ok(attempt) => attempt, Err(error) => return (409, json!({"error":error})) };
        s.progress = None;
        s.band_timings.clear();
        s.receive_trace = None;
        s.capture_receive_trace = capture_trace;
        app.cancel_requested.store(false, Ordering::SeqCst);
        started
    };
    let attempt_id = attempt.record.id.clone();
    if req.sync_marker {
        let cue_duration_ms = req.cue_duration_ms.unwrap_or(SYNC_CUE_DURATION_MS);
        let recording_id = req.recording_id.clone().unwrap_or_default();
        log(app, json!({"event":"sync_marker","attempt_id":attempt_id,"recording_id":recording_id,"encoding":"three-tone preamble + six DTMF digits (last six attempt-id digits)","cue":"preamble_dtmf6","cue_duration_ms":cue_duration_ms,"post_cue_lead_ms":post_cue_lead_ms,"cue_to_request_ms":cue_duration_ms + post_cue_lead_ms,"at_ms":now_ms()}));
    }
    if app.executor.try_send(DeviceCommand::Run(attempt)).is_err() {
        let outcome = Outcome { ok:false, saw:"device executor is unavailable".into(), started:now(), ended:now(), elapsed_ms:0, band_timings:vec![], receive_trace:None };
        let _ = app.state.lock().unwrap().workflow.finish(&attempt_id, outcome);
        return (503, json!({"error":"device executor is unavailable"}));
    }
    (202, json!({"running": true, "attempt_id":attempt_id, "sync_marker":req.sync_marker, "recording_id":req.recording_id}))
}

fn attempt_recording_id(attempt_id: &str) -> Option<String> {
    let prefix = attempt_id.split('-').next()?;
    if prefix.len() < 6 || !prefix.bytes().all(|byte| byte.is_ascii_digit()) { return None; }
    Some(prefix[prefix.len() - 6..].to_owned())
}

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

fn receive_trace_enabled(step: Option<Step>) -> bool {
    matches!(step, Some(Step::PowerSweep | Step::AudioPowerSweepLow | Step::AudioPowerSweepFull))
}

fn attempt_receive_trace_enabled(step: Option<Step>, sync_marker: bool) -> bool {
    sync_marker || receive_trace_enabled(step)
}

fn reset_device(app: &App) -> Result<(), &'static str> {
    let (done, completed) = sync_channel(0);
    app.executor.try_send(DeviceCommand::Reset(done)).map_err(|_| "device executor is unavailable")?;
    completed.recv().map_err(|_| "device executor stopped before resetting")
}

fn execute(app: &App, work: &Work, slot: &mut Option<Box<dyn Machine>>) -> Result<String, String> {
    if app.cancel_requested.load(Ordering::SeqCst) { return Err("execution cancelled before device access".into()); }
    let talk = work.step() == Some(Step::Talk);
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
    let trace_enabled = app.state.lock().unwrap().capture_receive_trace || receive_trace_enabled(work.step());
    if trace_enabled { m.set_receive_trace(true); }
    let r = work.prepared().run(m.as_mut(), &mut progress, &|| app.cancel_requested.load(Ordering::SeqCst));
    if trace_enabled {
        let trace = m.take_receive_trace();
        let mut state = app.state.lock().unwrap();
        state.receive_trace = Some(receive_trace_json(trace));
        state.capture_receive_trace = false;
    }
    if r.is_err() {
        // Reopen next time; the link state is unknown after an error.
        *slot = None;
    }
    r
}

#[cfg(test)]
mod audio_trace_tests {
    use super::*;

    #[test]
    fn both_power_sweep_steps_enable_receive_tracing() {
        assert!(receive_trace_enabled(Some(Step::PowerSweep)));
        assert!(receive_trace_enabled(Some(Step::AudioPowerSweepLow)));
        assert!(receive_trace_enabled(Some(Step::AudioPowerSweepFull)));
        assert!(!receive_trace_enabled(Some(Step::Frame)));
        assert!(attempt_receive_trace_enabled(Some(Step::Frame), true));
        assert!(!attempt_receive_trace_enabled(Some(Step::Frame), false));
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct VerdictReq {
    id: Option<String>,
    status: VerdictStatus,
    #[serde(default)]
    comment: String,
}

fn verdict(app: &App, body: &str) -> (u16, Value) {
    let req: VerdictReq = match serde_json::from_str(body) {
        Ok(r) => r,
        Err(e) => return (400, json!({"error": e.to_string()})),
    };
    let status = req.status;
    if status != VerdictStatus::Ok && req.comment.trim().is_empty() {
        return (400, json!({"error": "deviation and skipped need a comment"}));
    }
    let mut s = app.state.lock().unwrap();
    if let Some(id) = &req.id {
        if s.workflow.attempt().map(|attempt| &attempt.record.id) != Some(id) {
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
            let entry = json!({"event": "verdict", "id": id, "material": ran["attempt"]["material"], "goal": ran["attempt"]["goal"], "status": status, "comment": req.comment, "at": now()});
            log(app, entry.clone());
            return (200, entry);
        }
    }
    let (attempt, accepted) = match s.workflow.judge(status, &req.comment) { Ok(result) => result, Err(error) => return (409, json!({"error":error})) };
    let a = attempt.record;
    if accepted {
        if let Target::Card { review_id, stage, .. } = &a.target {
            if let Some(review) = s.review.as_mut().filter(|review| review.id == *review_id) { review.accepted.push(*stage); }
        }
    }
    let entry = json!({"event": "verdict", "id": a.id, "material": a.material(), "goal": a.goal, "status": status, "comment": req.comment, "at": now()});
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
    let cancel_requested = state.workflow.running();
    drop(state);
    log(app, json!({"event": "stop", "sent": sent, "cancel_requested": cancel_requested, "at": now()}));
    (200, json!({"sent": sent, "cancel_requested": cancel_requested}))
}

fn log(app: &App, entry: Value) {
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
        .collect()
}

fn pending_verdicts(app: &App) -> Vec<Value> {
    let entries = read_log(app);
    entries.iter().filter(|entry| entry["event"] == "ran" && !entries.iter().any(|record| {
        (record["event"] == "verdict" && record["id"] == entry["attempt"]["id"])
            || (record["event"] == "clear" && record["attempt_id"] == entry["attempt"]["id"])
    })).cloned().collect()
}
