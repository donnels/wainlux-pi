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
        test_catalog_path: concat!(env!("CARGO_MANIFEST_DIR"), "/../tests.toml").into(),
        audio_analyzer_url: None,
        audio_analyzer_token: None,
    })
    .unwrap();
    (addr, data.join("runs.jsonl").to_string_lossy().into())
}

fn start_with_analyzer(driver: &str, url: String, token: &str) -> (SocketAddr, String) {
    static N: AtomicUsize = AtomicUsize::new(0);
    let data = std::env::temp_dir().join(format!("laser-api-audio-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
    let _ = std::fs::remove_dir_all(&data);
    std::fs::create_dir_all(&data).unwrap();
    let addr = app::serve(app::Config {
        listen: "127.0.0.1:0".into(),
        data_dir: data.to_string_lossy().into(),
        driver: driver.into(),
        port_path: String::new(),
        materials_path: concat!(env!("CARGO_MANIFEST_DIR"), "/../materials.toml").into(),
        templates_path: concat!(env!("CARGO_MANIFEST_DIR"), "/../use-cases").into(),
        test_catalog_path: concat!(env!("CARGO_MANIFEST_DIR"), "/../tests.toml").into(),
        audio_analyzer_url: Some(url),
        audio_analyzer_token: Some(token.into()),
    }).unwrap();
    (addr, data.join("runs.jsonl").to_string_lossy().into())
}

#[test]
fn catalog_power_values_are_selected_for_machine_model() {
    for (driver, sweep, full) in [
        ("k6", json!([1000, 889, 778, 667, 556, 444, 333, 222, 111, 0]), json!([1000])),
        ("sim", json!([100, 89, 78, 67, 56, 44, 33, 22, 11, 0]), json!([100])),
    ] {
        let (addr, _) = start_with_driver(driver);
        let (code, catalog) = call(addr, "GET", "/api/steps", None);
        assert_eq!(code, 200);
        let definitions = catalog["steps"].as_array().unwrap();
        let values = |step: &str| {
            definitions.iter().find(|test| test["step"] == step).unwrap()["power_values"].clone()
        };
        assert_eq!(values("power_sweep"), sweep);
        assert_eq!(values("audio_power_sweep_low"), json!([1]));
        assert_eq!(values("audio_power_sweep_full"), full);
    }
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
        "items": [{"id":"wifi","content": {"kind": "wifi", "ssid": "Guest", "password": "s3cret"}}]
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
    let (code, v) = call(addr, "POST", "/api/verdict", Some(json!({"status": "ok", "comment": "contract test"})));
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
    assert!(!document.contains("/api/cards/import:"));
    assert!(document.contains("/api/state:") && document.contains("operationId: clearIdleState"));
    assert!(document.contains("/api/stop:"));
    assert!(document.contains("/api/audio-analyses:") && document.contains("uploadAudioForAnalysis"));
    assert!(document.contains("/api/audio-analyses/{attempt_id}/report:") && document.contains("getAudioAnalysisReport"));
    assert!(document.contains("/api/audio-analyses/availability:") && document.contains("getAudioAnalyzerAvailability"));
    assert!(document.contains("maxLength: 67108864"));
    assert!(document.contains("audio/x-m4a:"));
}

#[test]
fn standalone_web_app_manifest_is_served() {
    let (addr, _) = start();
    let response = ureq::get(&format!("http://{addr}/manifest.webmanifest")).call().unwrap();
    assert_eq!(response.header("Content-Type"), Some("application/manifest+json; charset=utf-8"));
    let manifest = response.into_string().unwrap();
    assert!(manifest.contains("\"display\": \"standalone\""));
    assert!(manifest.contains("\"start_url\": \"/\""));
}

#[test]
fn clear_state_resets_idle_review_and_plan_but_retains_audit_log() {
    let (addr, log) = start();
    let (code, _) = call(addr, "POST", "/api/jobs/review", Some(json!({"job":wifi_job()})));
    assert_eq!(code, 200);
    call(addr, "POST", "/api/plan", Some(json!({"step":"talk"})));
    let (code, verdict) = call(addr, "POST", "/api/verdict", Some(json!({"status":"skipped","comment":"reset test"})));
    assert_eq!(code, 200, "{verdict}");
    let (_, planned) = call(addr, "POST", "/api/plan", Some(json!({"step":"talk"})));

    let (code, cleared) = call(addr, "DELETE", "/api/state", None);
    assert_eq!(code, 200, "{cleared}");
    assert_eq!(cleared["cleared"], true);
    assert_eq!(cleared["run_log_retained"], true);
    let (_, state) = call(addr, "GET", "/api/state", None);
    assert_eq!(state["attempt"], Value::Null);
    assert_eq!(state["review_id"], Value::Null);
    assert_eq!(state["outcome"], Value::Null);
    assert_eq!(state["running"], false);
    assert_eq!(call(addr, "GET", "/api/jobs/review", None).0, 404);
    let (_, entries) = call(addr, "GET", "/api/log", None);
    assert_eq!(entries.as_array().unwrap().len(), 2);
    assert_eq!(entries[0]["event"], "verdict");
    assert_eq!(entries[1]["event"], "clear");
    assert_eq!(entries[1]["attempt_id"], planned["id"]);
    assert_eq!(call(addr, "GET", "/api/pending-verdicts", None).1, json!([]));
    assert!(std::path::Path::new(&log).exists());
}

#[test]
fn clear_state_refuses_while_a_simulated_run_is_active() {
    let (addr, _) = start();
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"step":"frame","params":{"x_mm":0.0,"y_mm":0.0,"size_mm":10.0}}))).0, 200);
    assert_eq!(call(addr, "POST", "/api/run", None).0, 202);
    let (code, problem) = call(addr, "DELETE", "/api/state", None);
    assert_eq!(code, 409);
    assert!(problem["error"].as_str().unwrap().contains("while an attempt is running"));
    assert_eq!(call(addr, "GET", "/api/state", None).1["running"], true);
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

    let (code, corners) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"corners","params":{"x_mm":5.0,"y_mm":5.0}})));
    assert_eq!(code, 200, "{corners}");
    assert!(corners["goal"].as_str().unwrap().contains("perimeter"));

    for (size, width, run) in [(10.0, 400, 200), (25.0, 510, 500)] {
        let (code, sweep) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"power_sweep","params":{"x_mm":20.0,"size_mm":size}})));
        assert_eq!(code, 200, "{sweep}");
        let svg = sweep["svg"].as_str().unwrap();
        assert!(svg.contains(&format!("viewBox=\"0 0 {width} 400\"")));
        assert!(svg.contains(&format!("M10 40h{run}v1h-{run}z")));
    }
}

#[test]
fn oversized_test_geometry_is_refused_before_allocation() {
    let (addr, _) = start();
    for step in ["square", "qr", "blank_rows", "frame", "power_sweep"] {
        for size in [1.0e30, -1.0, 0.0, 0.00001] {
            for route in ["/api/tests/preview", "/api/plan"] {
                let (code, response) = call(addr, "POST", route, Some(json!({"step":step,"params":{"size_mm":size}})));
                assert_eq!(code, 400, "{step} {size} {route}: {response}");
            }
        }
    }
    assert!(call(addr, "GET", "/api/state", None).1["attempt"].is_null());
}

#[test]
fn audio_power_profiles_are_separate_real_mode_steps_with_identical_patterns() {
    let analyzer = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let analyzer_addr = analyzer.server_addr().to_ip().unwrap();
    let analyzer_thread = std::thread::spawn(move || {
        let request = analyzer.recv().unwrap();
        assert_eq!(request.url(), "/health");
        assert_eq!(request.headers().iter().find(|header| header.field.to_string().eq_ignore_ascii_case("Authorization")).unwrap().value.as_str(), "Bearer plan-test-token");
        request.respond(tiny_http::Response::from_string("{\"ready\":1}")).unwrap();
    });
    let (addr, _) = start_with_analyzer("k6", format!("http://{analyzer_addr}/api/analyze"), "plan-test-token");
    let params = json!({"x_mm":20.0,"y_mm":0.0,"size_mm":19.0});
    let (code, regular) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"power_sweep","params":params})));
    assert_eq!(code, 200, "{regular}");
    let (code, low) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"audio_power_sweep_low","params":params})));
    assert_eq!(code, 200, "{low}");
    let (code, full) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"audio_power_sweep_full","params":params})));
    assert_eq!(code, 200, "{full}");
    assert_eq!(low["svg"], full["svg"]);
    assert!(low["goal"].as_str().unwrap().contains("power 1/1000"));
    assert!(full["goal"].as_str().unwrap().contains("power 1000/1000"));
    assert_ne!(low["goal"], full["goal"]);
    let (code, catalog) = call(addr, "GET", "/api/steps", None);
    assert_eq!(code, 200);
    let definitions = catalog["steps"].as_array().unwrap();
    let low_index = definitions.iter().position(|test| test["step"] == "audio_power_sweep_low").unwrap();
    let full_index = definitions.iter().position(|test| test["step"] == "audio_power_sweep_full").unwrap();
    assert!(low_index < full_index);
    let low_definition = &definitions[low_index];
    assert_eq!(low_definition["title"], "Audio compare: minimum power (nominal no-mark)");
    assert_eq!(low_definition["controls"], json!(["x_mm"]));
    assert_eq!(low_definition["settings"], json!(["power", "depth"]));
    assert_eq!(low_definition["requires_analyzer"], true);
    assert_eq!(low_definition["requires_sync_marker"], true);
    assert_eq!(low_definition["requires_recording_ack"], true);
    assert_eq!(low_definition["fixed_size_mm"], 19.0);
    assert_eq!(call(addr, "POST", "/api/mode", Some(json!({"real":true}))).0, 200);
    let (code, planned) = call(addr, "POST", "/api/plan", Some(json!({"step":"audio_power_sweep_low","params":{"x_mm":20.0,"y_mm":30.0,"size_mm":77.0,"values":{"power":999}}})));
    assert_eq!(code, 200, "{planned}");
    assert_eq!(planned["params"]["x_mm"], 20.0);
    assert_eq!(planned["params"]["y_mm"], 0.0);
    assert_eq!(planned["params"]["size_mm"], 19.0);
    assert_eq!(planned["params"]["values"]["power"], 1);
    let (code, refused) = call(addr, "POST", "/api/run", None);
    assert_eq!(code, 409);
    assert!(refused["error"].as_str().unwrap().contains("requires its recording-ID sync marker"));
    analyzer_thread.join().unwrap();
}

#[test]
fn audio_upload_forwards_only_matching_run_events_and_serves_report_same_origin() {
    let analyzer = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let analyzer_addr = analyzer.server_addr().to_ip().unwrap();
    let (captured_tx, captured_rx) = std::sync::mpsc::channel();
    let analyzer_thread = std::thread::spawn(move || {
        let mut request = analyzer.recv().unwrap();
        assert_eq!(request.headers().iter().find(|header| header.field.to_string().eq_ignore_ascii_case("Authorization")).unwrap().value.as_str(), "Bearer test-analyzer-token");
        let mut body = Vec::new();
        std::io::Read::read_to_end(&mut request.as_reader(), &mut body).unwrap();
        captured_tx.send(body).unwrap();
        let content_type = tiny_http::Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
        let timing = tiny_http::Header::from_bytes("X-Audio-Timing", r#"{"persistence_ms":3,"analysis_process_ms":40,"analysis":{"signal_analysis_ms":12}}"#).unwrap();
        request.respond(tiny_http::Response::from_string("<!doctype html><title>mock report</title>").with_header(content_type).with_header(timing)).unwrap();
        let report_request = analyzer.recv().unwrap();
        assert_eq!(report_request.method(), &tiny_http::Method::Get);
        assert!(report_request.url().ends_with("/api/reports/1794321995-wifi-content/report"));
        assert_eq!(report_request.headers().iter().find(|header| header.field.to_string().eq_ignore_ascii_case("Authorization")).unwrap().value.as_str(), "Bearer test-analyzer-token");
        let content_type = tiny_http::Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
        report_request.respond(tiny_http::Response::from_string("<!doctype html><title>mock report</title>").with_header(content_type)).unwrap();
    });
    let (addr, log_path) = start_with_analyzer("sim", format!("http://{analyzer_addr}/api/analyze"), "test-analyzer-token");
    let attempt_id = "1794321995-wifi-content";
    let recording_id = "321995";
    let events = [
        json!({"event":"sync_marker","attempt_id":attempt_id,"recording_id":recording_id,"at_ms":1794322161889i64}),
        json!({"event":"ran","attempt":{"id":attempt_id,"mode":"real","target":{"Card":{"stage_name":"content"}}},"outcome":{"ok":true,"saw":"complete"}}),
        json!({"event":"verdict","id":"unrelated-attempt","status":"ok","comment":"must not be forwarded"}),
    ];
    std::fs::write(&log_path, events.iter().map(Value::to_string).collect::<Vec<_>>().join("\n") + "\n").unwrap();

    let upload_page = ureq::get(&format!("http://{addr}/audio-upload/{attempt_id}")).call().unwrap();
    assert_eq!(upload_page.header("Content-Type"), Some("text/html; charset=utf-8"));
    let upload_html = upload_page.into_string().unwrap();
    assert!(upload_html.contains("type=\"file\""));
    assert!(upload_html.contains("X-Attempt-ID"));
    assert!(upload_html.contains(attempt_id));
    let qr = ureq::get(&format!("http://{addr}/api/audio-analyses/{attempt_id}/upload-qr.svg")).call().unwrap();
    assert_eq!(qr.header("Content-Type"), Some("image/svg+xml; charset=utf-8"));
    assert!(qr.into_string().unwrap().contains("<svg"));
    assert!(matches!(ureq::get(&format!("http://{addr}/audio-upload/unmatched-attempt")).call(), Err(ureq::Error::Status(404, _))));

    let invalid = ureq::post(&format!("http://{addr}/api/audio-analyses"))
        .set("Content-Type", "audio/mp4")
        .set("X-Attempt-ID", "1794321995-other")
        .send_bytes(b"not accepted");
    assert!(matches!(invalid, Err(ureq::Error::Status(409, _))));

    let upload = ureq::post(&format!("http://{addr}/api/audio-analyses"))
        .set("Content-Type", "audio/x-m4a")
        .set("X-Attempt-ID", attempt_id)
        .send_bytes(b"synthetic recording")
        .unwrap();
    let response: Value = serde_json::from_reader(upload.into_reader()).unwrap();
    let report_path = response["report_url"].as_str().unwrap();
    assert_eq!(report_path, format!("/api/audio-analyses/{attempt_id}/report"));
    assert_eq!(response["timing"]["persistence_ms"], 3);
    assert_eq!(response["timing"]["analysis_process_ms"], 40);
    assert_eq!(response["timing"]["analysis"]["signal_analysis_ms"], 12);
    assert!(response["timing"]["pi_receive_ms"].as_u64().is_some());
    assert!(response["timing"]["pi_prepare_ms"].as_u64().is_some());
    assert!(response["timing"]["analyzer_http_ms"].as_u64().is_some());
    assert!(response["timing"]["pi_total_ms"].as_u64().is_some());
    let forwarded = String::from_utf8(captured_rx.recv_timeout(Duration::from_secs(2)).unwrap()).unwrap();
    assert!(forwarded.contains("name=\"audio\"; filename=\"recording.m4a\""));
    assert!(forwarded.contains(attempt_id) && forwarded.contains(recording_id));
    assert!(!forwarded.contains("unrelated-attempt") && !forwarded.contains("must not be forwarded"));
    let (_, entries) = call(addr, "GET", "/api/log", None);
    let analysis = entries.as_array().unwrap().iter().find(|entry| entry["event"] == "audio_analysis").unwrap();
    assert_eq!(analysis["attempt_id"], attempt_id);
    assert_eq!(analysis["timing"]["pi_total_ms"], response["timing"]["pi_total_ms"]);
    assert_eq!(analysis["report_url"], report_path);
    assert!(!std::path::Path::new(&log_path).parent().unwrap().join("audio-analyses").exists(), "Pi must not retain audio or analysis reports");

    let report = ureq::get(&format!("http://{addr}{report_path}")).call().unwrap();
    assert_eq!(report.header("Content-Type"), Some("text/html; charset=utf-8"));
    assert!(report.header("Content-Security-Policy").unwrap().contains("default-src 'none'"));
    assert!(report.into_string().unwrap().contains("mock report"));
    analyzer_thread.join().unwrap();
}

#[test]
fn audio_analyzer_availability_is_optional_and_authenticated() {
    let (addr, _) = start();
    let (_, unavailable) = call(addr, "GET", "/api/audio-analyses/availability", None);
    assert_eq!(unavailable["available"], false);

    let analyzer = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let analyzer_addr = analyzer.server_addr().to_ip().unwrap();
    let analyzer_thread = std::thread::spawn(move || {
        let request = analyzer.recv().unwrap();
        assert_eq!(request.url(), "/health");
        assert_eq!(request.headers().iter().find(|header| header.field.to_string().eq_ignore_ascii_case("Authorization")).unwrap().value.as_str(), "Bearer availability-token");
        let content_type = tiny_http::Header::from_bytes("Content-Type", "application/json").unwrap();
        request.respond(tiny_http::Response::from_string("{\"ready\":1}").with_header(content_type)).unwrap();
    });
    let (addr, _) = start_with_analyzer("sim", format!("http://{analyzer_addr}/api/analyze"), "availability-token");
    let (_, available) = call(addr, "GET", "/api/audio-analyses/availability", None);
    assert_eq!(available["available"], true);
    analyzer_thread.join().unwrap();
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
    let (code, review) = call(addr, "POST", "/api/jobs/review", Some(json!({"job":wifi_job()})));
    assert_eq!(code, 200, "{review}");
    for stage in 0..2 {
        let (a, outcome, v) = attempt(addr, json!({"review_id":review["review_id"],"stage":stage}));
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
    assert_eq!(loaded["stages"], review["stages"]);
    assert_eq!(loaded["timing"]["raster_generation_ms"], review["timing"]["raster_generation_ms"]);
    assert!(review["timing"]["preview_encoding_ms"].as_u64().is_some());
    assert!(loaded["timing"]["preview_encoding_ms"].as_u64().is_some());
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
    let job = wifi_job();
    let (code, submitted) = call(addr, "POST", "/api/jobs/review", Some(json!({"job":job})));
    assert_eq!(code, 200, "{submitted}");
    let (_, review) = call(addr, "GET", "/api/jobs/review", None);
    attempt(addr, json!({"review_id":review["review_id"],"stage":0}));
    let (code, attempt) = call(addr, "POST", "/api/plan", Some(json!({"review_id":review["review_id"],"stage":1})));
    assert_eq!(code, 200);
    let response = ureq::get(&format!("http://{addr}/api/attempt/preview")).call().unwrap();
    assert_eq!(response.header("Cache-Control"), Some("no-store"));
    let active: Value = serde_json::from_str(&response.into_string().unwrap()).unwrap();
    assert_eq!(active["attempt_id"], attempt["id"]);
    assert_eq!(active["display"], template["display"]);
    assert_eq!(active["stages"].as_array().unwrap().len(), 1);
    assert_eq!(active["stages"][0]["goal"], review["stages"][1]["goal"]);
    assert_eq!(active["stages"][0]["burns"], review["stages"][1]["burns"]);
    assert!(active.get("job").is_none());
    let mut invalid_job = wifi_job();
    invalid_job["template"] = json!("does-not-exist");
    assert_eq!(call(addr, "POST", "/api/jobs/preview", Some(json!({"job":invalid_job}))).0, 400);
}

#[test]
fn ladder_step_with_material_still_works() {
    let (addr, _) = start();
    let (_, materials) = call(addr, "GET", "/api/materials", None);
    let material = materials.as_array().unwrap().iter().find(|entry| entry["material"]["id"] == "backing-first").unwrap();
    assert!(material["material"]["note"].as_str().unwrap().contains("Baseline: a QR scanned at power 500, depth 10."));
    let (preview_code, preview) = call(addr, "POST", "/api/tests/preview", Some(json!({"step":"square","material":"backing-first","params":{"size_mm":5.0}})));
    assert_eq!(preview_code, 200, "{preview}");
    assert!(preview["goal"].as_str().unwrap().contains("Power % 50"), "simulator uses its own defaults: {preview}");
    let (a, outcome, verdict) = attempt(addr, json!({"step":"square","material":"backing-first","params":{"size_mm":5.0}}));
    assert_eq!(outcome["ok"], true);
    assert_eq!(a["material"], "backing-first");
    assert!(a["goal"].as_str().unwrap().contains("Power % 50"));
    assert_eq!(verdict["material"], "backing-first");
    let (_, log) = call(addr, "GET", "/api/log", None);
    let run = log.as_array().unwrap().iter().find(|entry| entry["event"] == "ran").unwrap();
    assert_eq!(run["attempt"]["material"], "backing-first");
}

#[test]
fn qr_step_does_not_log_payload() {
    let (addr, log_path) = start();
    let payload = "SYNTHETIC-PRIVATE-QR-PAYLOAD";
    let (planned, outcome, _) = attempt(addr, json!({"step":"qr","params":{"text":payload,"size_mm":19.0}}));
    assert_eq!(outcome["ok"], true);
    assert!(!planned["goal"].as_str().unwrap().contains(payload));
    let (_, log) = call(addr, "GET", "/api/log", None);
    assert!(!log.to_string().contains(payload));
    assert!(!std::fs::read_to_string(log_path).unwrap().contains(payload));
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
fn sync_marker_is_logged_against_the_run_attempt() {
    let (addr, _) = start();
    let (plan_code, planned) = call(addr, "POST", "/api/plan", Some(json!({"step":"talk"})));
    assert_eq!(plan_code, 200, "{planned}");
    let attempt_prefix = planned["id"].as_str().unwrap().split('-').next().unwrap();
    let recording_id = attempt_prefix[attempt_prefix.len() - 6..].to_string();
    let (malformed_code, _) = call(addr, "POST", "/api/run", Some(json!({"sync_marker":true,"recording_id":"12A456","cue_duration_ms":2000,"post_cue_lead_ms":700})));
    assert_eq!(malformed_code, 400);
    let (mismatch_code, _) = call(addr, "POST", "/api/run", Some(json!({"sync_marker":true,"recording_id":"000000","cue_duration_ms":2000,"post_cue_lead_ms":700})));
    assert_eq!(mismatch_code, 409);
    let (run_code, started) = call(addr, "POST", "/api/run", Some(json!({"sync_marker":true,"recording_id":recording_id,"cue_duration_ms":2000,"post_cue_lead_ms":700})));
    assert_eq!(run_code, 202, "{started}");
    assert_eq!(started["attempt_id"], planned["id"]);
    assert_eq!(started["sync_marker"], true);
    assert_eq!(started["recording_id"], recording_id);

    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let (_, state) = call(addr, "GET", "/api/state", None);
        if !state["outcome"].is_null() { break; }
        assert!(Instant::now() < deadline, "no outcome: {state}");
        std::thread::yield_now();
    }
    let (_, log) = call(addr, "GET", "/api/log", None);
    let events = log.as_array().unwrap();
    let marker_index = events.iter().position(|event| event["event"] == "sync_marker").unwrap();
    let marker = &events[marker_index];
    assert_eq!(marker["attempt_id"], planned["id"]);
    assert_eq!(marker["recording_id"], recording_id);
    assert_eq!(marker["cue"], "preamble_dtmf6");
    assert_eq!(marker["cue_duration_ms"], 2000);
    assert_eq!(marker["post_cue_lead_ms"], 700);
    assert_eq!(marker["cue_to_request_ms"], 2700);
    assert!(marker["at_ms"].as_u64().is_some());
    assert!(events.iter().position(|event| event["event"] == "ran").unwrap() > marker_index);
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
fn workflow_preserves_plans_and_rejects_stale_starts() {
    let (addr, _) = start();
    assert_eq!(call(addr, "GET", "/api/state", None).1["phase"], "idle");
    let (code, planned) = call(addr, "POST", "/api/plan", Some(json!({"step":"talk"})));
    assert_eq!(code, 200);
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"step":"home"}))).0, 409);
    assert_eq!(call(addr, "POST", "/api/run", Some(json!({"attempt_id":"stale"}))).0, 409);
    let state = call(addr, "GET", "/api/state", None).1;
    assert_eq!(state["phase"], "planned");
    assert_eq!(state["attempt"]["id"], planned["id"]);
    assert_eq!(call(addr, "POST", "/api/run", Some(json!({"attempt_id":planned["id"]}))).0, 202);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let state = call(addr, "GET", "/api/state", None).1;
        if state["phase"] == "awaiting_verdict" { break; }
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"step":"talk"}))).0, 409);
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"status":"ok"}))).0, 200);
    assert_eq!(call(addr, "GET", "/api/state", None).1["phase"], "idle");

    let (_, review) = call(addr, "POST", "/api/jobs/review", Some(json!({"job":wifi_job()})));
    let (code, card) = call(addr, "POST", "/api/plan", Some(json!({"review_id":review["review_id"],"stage":0})));
    assert_eq!(code, 200, "{card}");
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"step":"talk"}))).0, 409);
    assert_eq!(call(addr, "GET", "/api/state", None).1["attempt"]["id"], card["id"]);
}

#[test]
fn real_audio_analysis_example_is_read_only_and_self_contained() {
    let (addr, log_path) = start();
    let response = ureq::get(&format!("http://{addr}/examples/audio-analysis")).call().unwrap();
    assert_eq!(response.header("Content-Type"), Some("text/html; charset=utf-8"));
    assert!(response.header("Cache-Control").unwrap().contains("max-age=3600"));
    let html = response.into_string().unwrap();
    assert!(html.contains("Decoded ID: 321995 (match)"));
    assert!(html.contains("data:audio/wav;base64,"));
    assert!(html.contains("First activity candidate begins +0.55 s"));
    assert!(!std::path::Path::new(&log_path).exists(), "serving an example must not create a laser run log");
}

#[test]
fn bad_requests_are_refused_with_a_reason() {
    let (addr, _) = start();
    let cases = [
        json!({"step": "talk", "job": wifi_job()}),
        json!({}),
        json!({"step": "talk", "colour": "red"}),
    ];
    for c in cases {
        let (code, r) = call(addr, "POST", "/api/plan", Some(c.clone()));
        assert_eq!(code, 400, "{c} -> {r}");
        assert!(r["error"].is_string(), "{r}");
    }
    assert_eq!(call(addr, "POST", "/api/run", None).0, 409, "nothing planned");
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"status":"ok"}))).0, 409, "nothing ran");
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
    assert!(entry.get("ok").is_none());
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
fn removed_card_import_route_is_not_registered() {
    let (addr, _) = start();
    assert_eq!(call(addr, "POST", "/api/cards/import", Some(json!({}))).0, 404);
    assert_eq!(call(addr, "GET", "/api/cards/import", None).0, 404);
}
#[test]
fn removed_client_request_shapes_are_rejected() {
    let (addr, _) = start();
    assert_eq!(call(addr, "POST", "/api/plan", Some(json!({"job":wifi_job()}))).0, 400);
    assert_eq!(call(addr, "POST", "/api/verdict", Some(json!({"ok":true}))).0, 400);
}
