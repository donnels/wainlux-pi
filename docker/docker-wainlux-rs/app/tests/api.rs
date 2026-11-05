//! API contract tests. A real server on a free port, simulated machine, shipped materials.
//! What a client such as neocv relies on is pinned here.

use serde_json::{json, Value};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

fn start() -> (SocketAddr, String) {
    start_with_driver("sim")
}

fn start_with_driver(driver: &str) -> (SocketAddr, String) {
    start_with_template(driver, None)
}

fn start_with_template(driver: &str, source: Option<&str>) -> (SocketAddr, String) {
    static N: AtomicUsize = AtomicUsize::new(0);
    let data = std::env::temp_dir().join(format!("laser-api-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
    let _ = std::fs::remove_dir_all(&data);
    let templates_path = match source {
        None => concat!(env!("CARGO_MANIFEST_DIR"), "/../use-cases").to_string(),
        Some(source) => {
            let directory = data.join("templates"); std::fs::create_dir_all(&directory).unwrap();
            std::fs::write(directory.join("case.toml"), source).unwrap();
            directory.to_string_lossy().into()
        }
    };
    let addr = app::serve(app::Config {
        listen: "127.0.0.1:0".into(),
        data_dir: data.to_string_lossy().into(),
        driver: driver.into(),
        port_path: String::new(),
        materials_path: concat!(env!("CARGO_MANIFEST_DIR"), "/../materials.toml").into(),
        templates_path,
    })
    .unwrap();
    (addr, data.join("runs.jsonl").to_string_lossy().into())
}

fn call(addr: SocketAddr, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
    let body = body.map(|b| b.to_string()).unwrap_or_default();
    let response = match ureq::request(method, &format!("http://{addr}{path}")).send_string(&body) {
        Ok(response) | Err(ureq::Error::Status(_, response)) => response,
        Err(error) => panic!("HTTP request: {error}"),
    };
    let code = response.status();
    let body = response.into_string().unwrap();
    (code, serde_json::from_str(&body).unwrap_or(Value::Null))
}

fn wifi_job() -> Value {
    json!({
        "template":"wifi-login",
        "piece": "bamboo-ntag215-light",
        "backing": "backing-first",
        "items": [{"content": {"kind": "wifi", "ssid": "Guest", "password": "s3cret"}}]
    })
}

/// plan -> run -> wait -> verdict. Returns (attempt, outcome, verdict).
fn attempt(addr: SocketAddr, plan: Value) -> (Value, Value, Value) {
    let (code, a) = call(addr, "POST", "/api/plan", Some(plan));
    assert_eq!(code, 200, "{a}");
    assert_eq!(call(addr, "POST", "/api/run", None).0, 202);
    let end = Instant::now() + Duration::from_secs(10);
    let outcome = loop {
        let (_, s) = call(addr, "GET", "/api/state", None);
        if !s["outcome"].is_null() {
            break s["outcome"].clone();
        }
        assert!(Instant::now() < end, "no outcome: {s}");
        std::thread::sleep(Duration::from_millis(20));
    };
    let (code, v) = call(addr, "POST", "/api/verdict", Some(json!({"ok": true, "comment": "contract test"})));
    assert_eq!(code, 200);
    assert_eq!(v["status"], "ok");
    (a, outcome, v)
}

#[test]
fn machine_and_materials_describe_themselves() {
    let (addr, _) = start();
    let (code, m) = call(addr, "GET", "/api/machine", None);
    assert_eq!(code, 200);
    assert_eq!(m["model"], "Simulator");
    assert!(m["reach_mm"].is_array() && m["settings"].is_array());
    let (_, list) = call(addr, "GET", "/api/materials", None);
    let ids: Vec<&str> = list.as_array().unwrap().iter().map(|e| e["material"]["id"].as_str().unwrap()).collect();
    assert!(ids.contains(&"bamboo-ntag215-light") && ids.contains(&"backing-first"));
}

#[test]
fn openapi_document_is_served_by_the_rust_api() {
    let (addr, _) = start();
    let response = ureq::get(&format!("http://{addr}/openapi.yaml")).call().unwrap();
    assert_eq!(response.header("Content-Type"), Some("application/yaml; charset=utf-8"));
    let document = response.into_string().unwrap();
    assert!(document.starts_with("openapi: 3.1.0"));
    assert!(document.contains("/api/jobs/preview:"));
    assert!(document.contains("/api/stop:"));
}

#[test]
fn test_preview_returns_expected_pattern_or_no_pattern_note() {
    let (addr, _) = start();
    let (code, frame) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"frame","params":{"x_mm":2.0,"y_mm":3.0,"size_mm":10.0}})));
    assert_eq!(code, 200, "{frame}");
    assert!(frame["goal"].as_str().unwrap().contains("10 mm square"));
    let frame_svg = frame["svg"].as_str().unwrap();
    assert!(frame_svg.contains("viewBox=\"0 0 200 200\""));
    assert!(frame["note"].as_str().unwrap().contains("laser-off BOUNDS"));

    let (code, talk) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"talk"})));
    assert_eq!(code, 200, "{talk}");
    assert!(talk["svg"].is_null());
    assert!(talk["goal"].as_str().unwrap().contains("answers"));
}


#[test]
fn swagger_ui_is_bundled_and_does_not_enable_mutating_try_it_out() {
    let (addr, _) = start();
    let page = ureq::get(&format!("http://{addr}/docs")).call().unwrap();
    assert_eq!(page.header("Content-Type"), Some("text/html; charset=utf-8"));
    let html = page.into_string().unwrap();
    assert!(html.contains("/openapi.yaml"));
    assert!(html.contains("supportedSubmitMethods: [\"get\"]"));
    assert!(!html.contains("unpkg.com") && !html.contains("jsdelivr.net"));

    let script = ureq::get(&format!("http://{addr}/docs/swagger-ui-bundle.js")).call().unwrap();
    assert_eq!(script.header("Content-Type"), Some("text/javascript; charset=utf-8"));
    assert!(!script.into_string().unwrap().is_empty());

    for (path, content_type) in [
        ("/docs/swagger-ui.css", "text/css; charset=utf-8"),
        ("/docs/favicon-16x16.png", "image/png"),
        ("/docs/favicon-32x32.png", "image/png"),
    ] {
        let response = ureq::get(&format!("http://{addr}{path}")).call().unwrap();
        assert_eq!(response.header("Content-Type"), Some(content_type));
        let mut reader = response.into_reader();
        let mut bytes = Vec::new();
        std::io::Read::read_to_end(&mut reader, &mut bytes).unwrap();
        assert!(!bytes.is_empty(), "{path} must be bundled");
    }
}

#[test]
fn preview_lists_stages_without_running() {
    let (addr, log) = start();
    let (code, p) = call(addr, "POST", "/api/jobs/preview", Some(json!({"job": wifi_job()})));
    assert_eq!(code, 200, "{p}");
    let names: Vec<&str> = p["stages"].as_array().unwrap().iter().map(|s| s["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["guide", "item 0"]);
    assert!(p["stages"][1]["goal"].as_str().unwrap().contains("Phone joins the network"));
    assert!(!std::path::Path::new(&log).exists(), "preview must not log");
}

#[test]
fn wifi_card_runs_stage_by_stage_and_logs_no_secrets() {
    let (addr, log) = start();
    for stage in 0..2 {
        let (a, outcome, v) = attempt(addr, json!({"job": wifi_job(), "stage": stage}));
        assert_eq!(outcome["ok"], true, "{outcome}");
        assert_eq!(v["goal"], a["goal"]);
    }
    let text = std::fs::read_to_string(&log).unwrap();
    assert_eq!(text.lines().count(), 4, "ran + verdict per stage");
    assert!(!text.contains("s3cret"));
    assert!(!text.contains("Guest"));
    let (_, entries) = call(addr, "GET", "/api/log", None);
    assert_eq!(entries.as_array().unwrap().len(), 4);
}

#[test]
fn vsr_logo_is_served_locally_as_png() {
    let (addr, _) = start();
    let response = ureq::get(&format!("http://{addr}/assets/vsr-logo.png")).call().unwrap();
    assert_eq!(response.header("Content-Type"), Some("image/png"));
    let mut bytes = Vec::new();
    std::io::Read::read_to_end(&mut response.into_reader(), &mut bytes).unwrap();
    assert!(bytes.starts_with(b"\x89PNG\r\n\x1a\n"));
}

fn submit_typed_example(addr: SocketAddr, template: &str, fields: Value) -> Value {
    let (code, review) = call(addr, "POST", "/api/jobs/form", Some(json!({"template":template,"review_id":null,"fields":fields,"piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false})));
    assert_eq!(code, 200, "{}", review["error"]);
    review
}

#[test]
fn vcard_and_public_tiger_are_submitted_via_api_as_separate_sides() {
    let (addr, log) = start_with_driver("k6");
    let review = submit_typed_example(addr, "vcard", json!({"contact":"BEGIN:VCARD\nVERSION:3.0\nFN:Jane Example\nURL:https://example.com/cv\nEND:VCARD","include-label":true,"label":"Jane\nExample","include-logo":true}));
    assert_eq!(review["stages"].as_array().unwrap().iter().map(|stage| stage["name"].as_str().unwrap()).collect::<Vec<_>>(), ["front","back"]);
    assert_eq!(review["stages"][0]["side"], "front");
    assert_eq!(review["stages"][1]["side"], "back");
    assert_ne!(review["stages"][0]["burns"][0]["png"], review["stages"][1]["burns"][0]["png"]);
    assert_eq!(call(addr, "GET", "/api/assets", None).1[0]["id"], "tiger");
    let logo = ureq::get(&format!("http://{addr}/api/assets/tiger.png")).call().unwrap();
    assert_eq!(logo.header("Content-Type"), Some("image/png"));
    assert!(!std::path::Path::new(&log).exists());
    assert_eq!(call(addr, "GET", "/api/state", None).1["running"], false);
}

#[test]
fn recovery_card_api_requires_front_acceptance_and_explicit_flip() {
    let (addr, _) = start_with_driver("k6");
    let codes = "SYNTHETIC DEMO ONLY - NOT VALID CODES\nDEMO-ALPHA-001\nDEMO-BRAVO-002\nDEMO-CHARLIE-003\nDEMO-DELTA-004\nDEMO-ECHO-005\nDEMO-FOXTROT-006\nDEMO-GOLF-007\nDEMO-HOTEL-008";
    let (_, templates) = call(addr,"GET","/api/templates",None);
    let github_template = templates.as_array().unwrap().iter().find(|template| template["id"]=="github-recovery").unwrap();
    assert_eq!(github_template["fields"].as_array().unwrap().iter().find(|field|field["id"]=="include-use-checklist").unwrap()["default"],false);
    let (review_code, review) = call(addr,"POST","/api/jobs/review",Some(json!({"job":{"template":"github-recovery","piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false,"items":[{"id":"recovery-qr","side":"front","content":{"kind":"qr","text":codes}},{"id":"use-checklist","side":"front","content":{"kind":"checkboxes","count":16,"columns":2,"box_mm":3.5,"gap_mm":0.8,"line_mm":0.35},"x_mm":67.725,"y_mm":10.06},{"id":"login-qr","side":"back","content":{"kind":"qr","text":"https://github.com/login"}}]}})));
    assert_eq!(review_code,200,"{}",review["error"]);
    let mut id = review["review_id"].clone();
    assert_eq!(review["stages"][0]["name"], "front"); assert_eq!(review["stages"][1]["name"], "back");
    assert!(!review["template"]["fields"].as_array().unwrap().iter().any(|field| field["id"] == "codes" || field["id"] == "print-codes"));
    assert!(!review["template"]["elements"].as_array().unwrap().iter().any(|element| element["kind"] == "text" && element["bindings"]["content.text"] == "recovery-payload"));
    assert!(review["template"]["elements"].as_array().unwrap().iter().any(|element| element["id"] == "front-context"));
    assert!(review["template"]["fields"].as_array().unwrap().iter().any(|field| field["id"] == "recovery-payload" && field["editable"] == true && field["revealable"] == true && field["masked"] == true));
    assert_eq!(review["fields"]["recovery-payload"]["value"], codes);
    assert_eq!(review["fields"]["include-url-text"]["value"], false);
    assert_eq!(review["fields"]["include-use-checklist"]["value"], true, "submitted checklist element enables the opt-in grid");
    assert_eq!(review["fields"]["front-context"]["value"], "GitHub MFA recovery codes");
    assert_eq!(review["fields"]["back-context"]["value"], "GitHub login to use these codes");
    assert_eq!(review["fields"]["front-context-y"]["value"], 1.75);
    assert_eq!(review["fields"]["back-context-y"]["value"], 1.75);
    assert_eq!(review["job"]["items"].as_array().unwrap().len(), 5, "API-provided QR/checklist items retain both template-default context captions");
    assert!(review["stages"][0]["burns"][0]["width_px"].as_u64().unwrap() > 0);
    let (code, edited) = call(addr, "POST", "/api/jobs/form", Some(json!({"template":"github-recovery","review_id":id,"fields":{"recovery-payload":"SYNTHETIC EDITED DEMO ONLY\nDEMO-ZULU-999"},"piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false})));
    assert_eq!(code,200,"{}",edited["error"]);
    assert_eq!(edited["fields"]["recovery-payload"]["value"],"SYNTHETIC EDITED DEMO ONLY\nDEMO-ZULU-999");
    assert_ne!(edited["stages"][0]["burns"][0]["png"],review["stages"][0]["burns"][0]["png"]);
    id=edited["review_id"].clone();
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"review_id":id,"stage":1,"placement_confirmed":true}))).0, 409);
    let (_, outcome, _) = attempt(addr, json!({"review_id":id,"stage":0}));
    assert_eq!(outcome["ok"], true);
    assert_eq!(call(addr, "GET", "/api/jobs/review", None).1["accepted_stages"], json!([0]));
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"review_id":id,"stage":1}))).0, 409);
    assert_eq!(call(addr, "POST", "/api/mode", Some(json!({"real":true}))).0, 200);
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"review_id":id,"stage":1,"placement_confirmed":true}))).0, 409, "simulation verdict cannot unlock a real back burn");
    call(addr, "POST", "/api/mode", Some(json!({"real":false})));
    attempt(addr, json!({"review_id":id,"stage":0}));
    let (_, outcome, _) = attempt(addr, json!({"review_id":id,"stage":1,"placement_confirmed":true}));
    assert_eq!(outcome["ok"], true);
    let (code, optional) = call(addr, "POST", "/api/jobs/form", Some(json!({"template":"github-recovery","review_id":id,"fields":{"include-use-checklist":false},"piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false})));
    assert_eq!(code,200); assert_eq!(optional["fields"]["include-use-checklist"]["value"],false);
    assert!(!optional["job"]["items"].as_array().unwrap().iter().any(|item|item["id"]=="use-checklist"));
    let (code, resized) = call(addr, "POST", "/api/jobs/form", Some(json!({"template":"github-recovery","review_id":optional["review_id"],"fields":{"include-use-checklist":true,"check-count":8},"piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false})));
    assert_eq!(code,200); assert_eq!(resized["fields"]["check-count"]["value"],8);
    assert!(resized["job"]["items"].as_array().unwrap().iter().any(|item|item["id"]=="use-checklist"));
    assert_ne!(resized["stages"][0]["burns"][0]["png"],optional["stages"][0]["burns"][0]["png"],"changing the box count changes only the private raster output");
    let (empty_code, _) = call(addr,"POST","/api/jobs/review",Some(json!({"job":{"template":"github-recovery","piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false,"items":[{"id":"recovery-qr","side":"front","content":{"kind":"qr","text":" "}},{"id":"login-qr","side":"back","content":{"kind":"qr","text":"https://github.com/login"}}]}})));
    assert_eq!(empty_code,400,"empty recovery data is not a valid review");
}

#[test]
fn editing_github_login_url_changes_only_the_back_qr_raster() {
    let (addr, log) = start_with_driver("sim");
    let payload = "SYNTHETIC DEMO ONLY - NOT VALID CODES\nD0001-C0001";
    let (code, first) = call(addr,"POST","/api/jobs/review",Some(json!({"job":{"template":"github-recovery","piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false,"items":[{"id":"recovery-qr","side":"front","content":{"kind":"qr","text":payload}},{"id":"login-qr","side":"back","content":{"kind":"qr","text":"https://github.com/login"}}]}})));
    assert_eq!(code,200);
    let form = json!({"template":"github-recovery","review_id":first["review_id"],"fields":{"login-url":"https://github.com/new-login"},"piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":false});
    let (code, second) = call(addr,"POST","/api/jobs/form",Some(form));
    assert_eq!(code,200,"{}",second["error"]);
    assert_eq!(second["fields"]["login-url"]["value"],"https://github.com/new-login");
    assert_eq!(first["stages"][0]["burns"][0]["png"],second["stages"][0]["burns"][0]["png"],"front recovery QR remains unchanged");
    assert_ne!(first["stages"][1]["burns"][0]["png"],second["stages"][1]["burns"][0]["png"],"back login QR is regenerated from the edited URL");
    assert!(!first.to_string().contains(payload) && !second.to_string().contains(payload));
    assert!(!std::path::Path::new(&log).exists());
}

#[test]
fn operator_projection_hides_locked_source_and_rejects_edits() {
    let source = include_str!("../../use-cases/wifi-login.toml").lines().map(|line| {
        if line.contains("id=\"password\"") || line.contains("id=\"title\"") {
            line.replace("editable=true", "editable=false").replace("revealable=true", "revealable=false")
        } else { line.to_string() }
    }).collect::<Vec<_>>().join("\n");
    let (addr, log) = start_with_template("sim", Some(&source));
    let mut job = wifi_job();
    job["items"].as_array_mut().unwrap().push(json!({"id":"title","content":{"kind":"text","text":"Private title","font_size_mm":2.5}}));
    let (code, review) = call(addr, "POST", "/api/jobs/review", Some(json!({"job":job})));
    assert_eq!(code, 200, "{}", review["error"]);
    assert!(review["fields"]["password"]["value"].is_null());
    assert!(review["fields"]["title"]["value"].is_null());
    assert_eq!(review["fields"]["title"]["provided"], true);
    assert!(review["job"]["items"][0].get("content").is_none());
    assert!(!review.to_string().contains("s3cret"));
    assert!(!review.to_string().contains("Private title"));
    let mut form = json!({"template":"wifi-login","review_id":review["review_id"],"piece":"bamboo-ntag215-light","backing":"backing-first","placement":"left","guide":true,"fields":{"title":"Changed"}});
    assert_eq!(call(addr, "POST", "/api/jobs/form", Some(form.clone())).0, 403);
    form["fields"] = json!({"not-declared":"Changed"});
    assert_eq!(call(addr, "POST", "/api/jobs/form", Some(form.clone())).0, 403);
    form["fields"] = json!({"qr-x":16.0});
    let (code, moved) = call(addr, "POST", "/api/jobs/form", Some(form));
    assert_eq!(code, 200, "{}", moved["error"]);
    assert_eq!(moved["fields"]["qr-x"]["value"], 16.0);
    assert!(moved["fields"]["password"]["value"].is_null());
    assert!(moved["fields"]["title"]["value"].is_null());
    assert_eq!(moved["stages"][1]["burns"][0]["height_px"], review["stages"][1]["burns"][0]["height_px"]);
    assert!(!std::path::Path::new(&log).exists());
    assert_eq!(call(addr, "GET", "/api/state", None).1["running"], false);
}

#[test]
fn submitted_review_returns_one_bit_png_and_never_starts_the_laser() {
    use base64::Engine;
    let (addr, log) = start_with_driver("k6");
    let (code, review) = call(addr, "POST", "/api/jobs/review", Some(json!({"job":wifi_job()})));
    assert_eq!(code, 200);
    let id = review["review_id"].as_str().unwrap();
    let (_, state) = call(addr, "GET", "/api/state", None);
    assert_eq!(state["review_id"], id);
    assert_eq!(state["running"], false);
    assert!(state["attempt"].is_null());
    assert!(!std::path::Path::new(&log).exists());
    let response = ureq::get(&format!("http://{addr}/api/jobs/review")).call().unwrap();
    assert_eq!(response.header("Cache-Control"), Some("no-store"));
    let loaded: Value = serde_json::from_str(&response.into_string().unwrap()).unwrap();
    assert_eq!(loaded, review);
    let burn = &review["stages"][0]["burns"][0];
    let bytes = base64::engine::general_purpose::STANDARD.decode(burn["png"].as_str().unwrap().strip_prefix("data:image/png;base64,").unwrap()).unwrap();
    let mut reader = png::Decoder::new(std::io::Cursor::new(bytes)).read_info().unwrap();
    assert_eq!(reader.info().bit_depth, png::BitDepth::One);
    assert_eq!(reader.info().color_type, png::ColorType::Grayscale);
    assert_eq!(reader.info().width as u64, burn["width_px"].as_u64().unwrap());
    assert_eq!(reader.info().height as u64, burn["height_px"].as_u64().unwrap());
    let mut pixels = vec![0; reader.output_buffer_size()];
    reader.next_frame(&mut pixels).unwrap();
    assert_eq!(pixels[0] & 128, 0, "connected guide top-left is a black burn pixel");
    let stride = (reader.info().width as usize + 7) / 8;
    let middle = reader.info().height as usize / 2;
    assert_eq!(pixels[middle * stride] & 128, 0, "left leg is black");
    assert_eq!(pixels[middle * stride + 10], 255, "interior is unburned white");
    let (code, _) = call(addr, "POST", "/api/plan", Some(json!({"review_id":"stale","stage":0})));
    assert_eq!(code, 409);
    let (code, planned) = call(addr, "POST", "/api/plan", Some(json!({"review_id":id,"stage":0})));
    assert_eq!(code, 200);
    assert_eq!(planned["review_id"], id);
    let (_, active) = call(addr, "GET", "/api/attempt/preview", None);
    assert_eq!(active["stages"][0], review["stages"][0]);
    assert_eq!(call(addr, "POST", "/api/jobs/review", Some(json!({"job":wifi_job()}))).0, 409);
    assert_eq!(call(addr, "GET", "/api/state", None).1["running"], false);
}

#[test]
fn template_visibility_and_exact_active_preview_survive_a_new_client() {
    let (addr, _) = start();
    let (code, templates) = call(addr, "GET", "/api/templates", None);
    assert_eq!(code, 200);
    let template = templates.as_array().unwrap().iter().find(|template| template["id"] == "wifi-login").unwrap();
    assert_eq!(template["display"]["card_content_visible"], false);
    assert_eq!(template["display"]["markings_visible"], true);
    assert!(template["display"]["masked_fields"].as_array().unwrap().contains(&json!("ssid")));
    assert_eq!(call(addr, "GET", "/api/attempt/preview", None).0, 404);
    let mut job = wifi_job();
    job["template"] = json!("wifi-login");
    let (_, preview) = call(addr, "POST", "/api/jobs/preview", Some(json!({"job":job})));
    let (code, attempt) = call(addr, "POST", "/api/plan", Some(json!({"job":job,"stage":1})));
    assert_eq!(code, 200);
    let response = ureq::get(&format!("http://{addr}/api/attempt/preview")).call().unwrap();
    assert_eq!(response.header("Cache-Control"), Some("no-store"));
    let active: Value = serde_json::from_str(&response.into_string().unwrap()).unwrap();
    assert_eq!(active["attempt_id"], attempt["id"]);
    assert_eq!(active["display"], template["display"]);
    assert_eq!(active["stages"].as_array().unwrap().len(), 1);
    assert_eq!(active["stages"][0], preview["stages"][1]);
    assert!(active.get("job").is_none());
    job["template"] = json!("does-not-exist");
    assert_eq!(call(addr, "POST", "/api/jobs/preview", Some(json!({"job":job}))).0, 400);
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"job":job}))).0, 400);
}

#[test]
fn ladder_step_with_material_still_works() {
    let (addr, _) = start();
    let (a, outcome, _) = attempt(addr, json!({"step": "talk"}));
    assert_eq!(outcome["ok"], true);
    assert!(a["goal"].as_str().unwrap().contains("Simulator"));
}

#[test]
fn api_reports_structured_non_banded_progress() {
    let (addr, _) = start();
    assert_eq!(call(addr,"POST","/api/plan",Some(json!({"step":"square"}))).0,200);
    assert_eq!(call(addr,"POST","/api/run",None).0,202);
    let deadline=Instant::now()+Duration::from_secs(5);
    let state=loop { let (_,state)=call(addr,"GET","/api/state",None); if !state["outcome"].is_null(){break state;} assert!(Instant::now()<deadline); std::thread::yield_now(); };
    assert_eq!(state["outcome"]["ok"],true);
    assert_eq!(state["progress"]["phase"],"reported_complete");
    assert_eq!(state["progress"]["percent"],100);
    assert!(state["progress"]["band"].is_null());
}

#[test]
fn idle_stop_does_not_cancel_the_next_explicit_attempt() {
    let (addr, _) = start();
    let (code, stopped) = call(addr, "POST", "/api/stop", None);
    assert_eq!(code, 200);
    assert_eq!(stopped["sent"], false);
    assert_eq!(stopped["cancel_requested"], false);
    let (_, outcome, _) = attempt(addr, json!({"step":"talk"}));
    assert_eq!(outcome["ok"], true);
}

#[test]
fn bad_requests_are_refused_with_a_reason() {
    let (addr, _) = start();
    let cases = [
        json!({"job": {"piece": "nope", "items": []}}),
        json!({"job": wifi_job(), "stage": 5}),
        json!({"step": "talk", "job": wifi_job()}),
        json!({}),
        json!({"job": wifi_job(), "colour": "red"}),
    ];
    for c in cases {
        let (code, r) = call(addr, "POST", "/api/plan", Some(c.clone()));
        assert_eq!(code, 400, "{c} -> {r}");
        assert!(r["error"].is_string(), "{r}");
    }
    assert_eq!(call(addr, "POST", "/api/run", None).0, 409, "nothing planned");
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"ok": true}))).0, 409, "nothing ran");
}

#[test]
fn skipped_is_only_for_an_unperformed_test() {
    let (addr, _) = start();
    let (_, planned) = call(addr, "POST", "/api/plan", Some(json!({"step": "talk"})));
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"status": "skipped"}))).0, 400);
    let (code, entry) = call(addr, "POST", "/api/verdict", Some(json!({"status": "skipped", "comment": "device unavailable"})));
    assert_eq!(code, 200);
    assert_eq!(entry["status"], "skipped");
    assert_eq!(entry["goal"], planned["goal"]);
    assert!(entry["ok"].is_null());
    let (_, entries) = call(addr, "GET", "/api/log", None);
    assert_eq!(entries.as_array().unwrap().len(), 1);
    assert_eq!(entries[0]["event"], "verdict");
}

#[test]
fn executed_test_gets_a_verdict_against_its_original_goal() {
    let (addr, _) = start();
    let (_, planned) = call(addr, "POST", "/api/plan", Some(json!({"step": "talk"})));
    call(addr, "POST", "/api/run", None);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let (_, state) = call(addr, "GET", "/api/state", None);
        if !state["outcome"].is_null() { break; }
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"status": "skipped", "comment": "cannot observe"}))).0, 409);
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"status": "deviation"}))).0, 400);
    let (code, entry) = call(addr, "POST", "/api/verdict", Some(json!({"status": "deviation", "comment": "unexpected identity"})));
    assert_eq!(code, 200);
    assert_eq!(entry["status"], "deviation");
    assert_eq!(entry["goal"], planned["goal"]);
}

#[test]
fn logged_attempt_can_be_judged_later_without_replacing_current_plan() {
    let (addr, log_path) = start();
    let previous = json!({"event":"ran","attempt":{"id":"prior-abc","goal":"Original A/B/C hypothesis"},"outcome":{"ok":true,"saw":"all jobs returned"}});
    std::fs::write(&log_path, format!("{previous}\n")).unwrap();
    let (_, current) = call(addr, "POST", "/api/plan", Some(json!({"step":"talk"})));
    let (_, pending) = call(addr, "GET", "/api/pending-verdicts", None);
    assert_eq!(pending.as_array().unwrap().len(), 1);
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"id":"prior-abc","status":"skipped","comment":"not inspected yet"}))).0, 409);
    let (code, entry) = call(addr, "POST", "/api/verdict", Some(json!({"id":"prior-abc","status":"deviation","comment":"Measured later"})));
    assert_eq!(code, 200);
    assert_eq!(entry["goal"], "Original A/B/C hypothesis");
    let (_, state) = call(addr, "GET", "/api/state", None);
    assert_eq!(state["attempt"]["id"], current["id"]);
    let (_, pending) = call(addr, "GET", "/api/pending-verdicts", None);
    assert!(pending.as_array().unwrap().is_empty());
}

#[test]
fn mode_switch_is_idle_only_and_does_not_run_the_laser() {
    let (addr, _) = start_with_driver("k6");
    assert_eq!(call(addr, "GET", "/api/mode", None).1["mode"], "simulator");
    assert_eq!(call(addr, "POST", "/api/mode", Some(json!({"real":true}))).0, 200);
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"step":"talk"}))).0, 200);
    assert_eq!(call(addr, "POST", "/api/mode", Some(json!({"real":false}))).0, 409);
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"status":"skipped","comment":"No motion wanted"}))).0, 200);
    assert_eq!(call(addr, "POST", "/api/mode", Some(json!({"real":false}))).0, 200);
    assert_eq!(call(addr, "GET", "/api/machine", None).1["reach_mm"], json!([80.0,75.0]));
}

#[test]
fn imported_card_guide_then_qr_and_multiline_text_share_layout() {
    let (addr, log_path) = start_with_driver("k6");
    let source = "[card]\nmaterial='bamboo-ntag215-light'\nbacking='backing-first'\nplacement='left'\n[wifi]\nssid='Synthetic guest'\npassword='synthetic-private-value'\nsecurity='WPA2'\n[content]\nqr_kind='wifi'\nurl_or_text=''\ntext=\"\"\"Gärtringen\nHome WLAN\"\"\"\nfont_size_mm=2.5\n";
    let (code, imported) = call(addr, "POST", "/api/cards/import", Some(json!({"toml":source,"template":"wifi-login"})));
    assert_eq!(code, 200, "{}", imported["error"]);
    let job = imported["job"].clone();
    assert_eq!(job["items"].as_array().unwrap().len(), 2);
    assert_eq!(job["items"][1]["content"]["kind"], "text");
    let (code, preview) = call(addr, "POST", "/api/jobs/preview", Some(json!({"job":job})));
    assert_eq!(code, 200, "{}", preview["error"]);
    assert_eq!(preview["stages"][0]["name"], "guide");
    assert_eq!(preview["stages"][1]["name"], "content");
    assert_eq!(preview["stages"][1]["burns"].as_array().unwrap().len(), 1);
    assert_eq!(preview["layout"]["reach_mm"], json!([80.0,75.0]));
    assert!(!std::path::Path::new(&log_path).exists());
    let mut guide = job.clone();
    guide["items"] = json!([]);
    attempt(addr, json!({"job":guide,"stage":0}));
    let mut content = job;
    content["guide"] = json!(false);
    attempt(addr, json!({"job":content,"stage":0}));
    let log = std::fs::read_to_string(log_path).unwrap();
    assert!(!log.contains("synthetic-private-value") && !log.contains("Gärtringen"));
}
