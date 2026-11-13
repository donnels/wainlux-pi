use serde_json::Value;
use std::path::Path;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct Receipt {
    pub review_id: String,
    pub raster_generation_ms: u64,
    pub preview_encoding_ms: u64,
    pub client_round_trip_ms: u64,
}

fn read_text(path: &Path) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|_| "Cannot read an input file as UTF-8 text.".into())
}

pub fn run(server: &str, path: &Path, text_files: &[String]) -> Result<Receipt, String> {
    let mut request: Value = serde_json::from_str(&read_text(path)?)
        .map_err(|_| "Input must be a JSON review request.".to_owned())?;
    let items = request.pointer_mut("/job/items").and_then(Value::as_array_mut)
        .ok_or("Input must contain job.items.")?;
    for replacement in text_files {
        let (element, filename) = replacement.split_once('=').ok_or("Use --text-file ELEMENT=FILE.")?;
        let item = items.iter_mut().find(|item| item["id"] == element)
            .ok_or("Text-file element is not present in the job.")?;
        if !matches!(item["content"]["kind"].as_str(), Some("qr" | "text")) {
            return Err("Text-file element must be a QR or text item.".into());
        }
        let text = read_text(Path::new(filename))?;
        if text.trim().is_empty() || text.contains('\0') {
            return Err("Text-file payload must be nonempty UTF-8 without NUL characters.".into());
        }
        item["content"]["text"] = Value::String(text);
    }
    send(server, request)
}

fn send(server: &str, request: Value) -> Result<Receipt, String> {
    let started = Instant::now();
    let response = ureq::AgentBuilder::new().redirects(0).timeout(Duration::from_secs(30)).build()
        .post(&format!("{}/api/jobs/review", server.trim_end_matches('/')))
        .send_json(request)
        .map_err(|error| match error {
            ureq::Error::Status(status, _) => format!("Review refused (HTTP {status}); inspect the operator UI. Response content withheld."),
            _ => "Cannot submit review; check the explicit server URL and connection.".into(),
        })?;
    if response.status() != 200 {
        return Err(format!("Unexpected review response (HTTP {}). No redirect was followed.", response.status()));
    }
    let response: Value = response.into_json().map_err(|_| "Invalid review response.".to_owned())?;
    let review_id = response["review_id"].as_str().map(str::to_owned).ok_or("Review response has no ID.".to_owned())?;
    Ok(Receipt {
        review_id,
        raster_generation_ms: response["timing"]["raster_generation_ms"].as_u64().unwrap_or(0),
        preview_encoding_ms: response["timing"]["preview_encoding_ms"].as_u64().unwrap_or(0),
        client_round_trip_ms: started.elapsed().as_millis() as u64,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn shipped_examples_and_text_files_create_reviews_without_planning() {
        let client = Path::new(env!("CARGO_MANIFEST_DIR"));
        let service = client.join("../docker/docker-wainlux-rs");
        let data = std::env::temp_dir().join(format!("wainlux-client-tests-{}", std::process::id()));
        let addr = app::serve(app::Config {
            listen: "127.0.0.1:0".into(),
            data_dir: data.to_string_lossy().into(),
            driver: "k6".into(),
            port_path: String::new(),
            materials_path: service.join("materials.toml").to_string_lossy().into(),
            templates_path: service.join("use-cases").to_string_lossy().into(),
            test_catalog_path: service.join("tests.toml").to_string_lossy().into(),
            audio_analyzer_url: None,
            audio_analyzer_token: None,
        }).unwrap();
        let url = format!("http://{addr}");
        for (example, element, filename, field, sides) in [
            ("wifi", "", "", "", vec!["front"]),
            ("github", "recovery-qr", "synthetic-codes.txt", "recovery-payload", vec!["front", "back"]),
            ("vcard", "contact-qr", "contact.vcf", "contact", vec!["front", "back"]),
        ] {
            let file = client.join("inputs").join(filename);
            let replacements = if element.is_empty() { vec![] } else { vec![format!("{element}={}", file.display())] };
            let receipt = run(&url, &client.join(format!("inputs/{example}.json")), &replacements).unwrap();
            let review: Value = ureq::get(&format!("{url}/api/jobs/review")).call().unwrap().into_json().unwrap();
            assert_eq!(review["review_id"], receipt.review_id);
            assert_eq!(review["timing"]["raster_generation_ms"].as_u64(), Some(receipt.raster_generation_ms));
            assert!(review["timing"]["preview_encoding_ms"].as_u64().is_some());
            let stages = review["stages"].as_array().unwrap();
            assert_eq!(stages.iter().map(|stage| stage["side"].as_str().unwrap()).collect::<Vec<_>>(), sides);
            if !field.is_empty() {
                assert_eq!(review["fields"][field]["value"], read_text(&file).unwrap());
            }
            let state: Value = ureq::get(&format!("{url}/api/state")).call().unwrap().into_json().unwrap();
            assert_eq!(state["running"], false);
            assert!(state["attempt"].is_null());
        }
        std::fs::remove_dir_all(data).unwrap();
    }

    #[test]
    fn submits_only_review_and_does_not_echo_response_errors_or_follow_redirects() {
        for status in [200, 409, 302] {
            let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
            let url = format!("http://{}", server.server_addr());
            let worker = std::thread::spawn(move || {
                let mut request = server.recv_timeout(Duration::from_secs(5)).unwrap().unwrap();
                assert_eq!(request.method(), &tiny_http::Method::Post);
                assert_eq!(request.url(), "/api/jobs/review");
                let mut body = String::new();
                request.as_reader().read_to_string(&mut body).unwrap();
                assert_eq!(serde_json::from_str::<Value>(&body).unwrap()["job"]["payload"], "SYNTHETIC-SECRET");
                request.respond(tiny_http::Response::from_string(
                    r#"{"review_id":"review-test","error":"SYNTHETIC-SECRET"}"#
                ).with_status_code(status).with_header(
                    tiny_http::Header::from_bytes("Location", "/api/run").unwrap()
                )).unwrap();
            });
            let result = send(&url, json!({"job":{"payload":"SYNTHETIC-SECRET"}}));
            if status == 200 {
                assert_eq!(result.unwrap().review_id, "review-test");
            } else {
                let error = result.unwrap_err();
                assert!(error.contains(&status.to_string()));
                assert!(!error.contains("SYNTHETIC-SECRET"));
            }
            worker.join().unwrap();
        }
    }
}