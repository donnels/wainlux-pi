#!/usr/bin/env python3
"""HTTP upload API for recordings forwarded by the Pi service."""

import argparse
import hmac
import json
import os
import subprocess
import sys
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 512 * 1024
MAX_REQUEST_BYTES = MAX_AUDIO_BYTES + MAX_LOG_BYTES + 64 * 1024
ANALYSIS_TIMEOUT_SECONDS = 900


def parse_multipart(content_type, body):
    message = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ValueError("expected multipart/form-data")
    parts = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name in parts:
            raise ValueError(f"duplicate {name} field")
        parts[name] = (part.get_payload(decode=True) or b"", part.get_filename(), part.get_content_type())
    return parts


def validate_submission(parts):
    required = {"audio", "attempt_id", "expected_id", "run_log"}
    if set(parts) != required:
        raise ValueError("required fields: audio, attempt_id, expected_id, run_log")
    audio, filename, _ = parts["audio"]
    suffix = Path(filename or "recording.m4a").suffix.lower()
    if suffix not in (".m4a", ".wav") or not audio:
        raise ValueError("audio must be a non-empty .m4a or .wav file")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("audio exceeds the 64 MiB upload limit")
    attempt_id = parts["attempt_id"][0].decode("utf-8").strip()
    expected_id = parts["expected_id"][0].decode("ascii").strip()
    allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
    if not attempt_id or any(character not in allowed for character in attempt_id):
        raise ValueError("invalid attempt ID")
    if len(expected_id) != 6 or not expected_id.isdigit() or not attempt_id.split("-", 1)[0].endswith(expected_id):
        raise ValueError("expected ID does not match the attempt ID")
    try:
        run_log = json.loads(parts["run_log"][0])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("run_log must be JSON") from error
    if not isinstance(run_log, list):
        raise ValueError("run_log must be an array of matching events")
    marker = next((event for event in run_log if event.get("event") == "sync_marker" and event.get("attempt_id") == attempt_id and event.get("recording_id") == expected_id), None)
    outcome = next((event for event in run_log if event.get("event") == "ran" and (event.get("attempt") or {}).get("id") == attempt_id), None)
    if marker is None or outcome is None:
        raise ValueError("run_log must include the matching sync marker and completed run")
    if (outcome.get("attempt") or {}).get("mode") != "real":
        raise ValueError("attempt must be a completed Real run")
    if len(parts["run_log"][0]) > MAX_LOG_BYTES:
        raise ValueError("run_log exceeds the 512 KiB limit")
    return audio, suffix, attempt_id, expected_id, run_log


def persist_submission(parts, data_dir):
    audio, suffix, attempt_id, expected_id, run_log = validate_submission(parts)
    attempt_dir = Path(data_dir) / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    audio_path = attempt_dir / f"audio{suffix}"
    log_path = attempt_dir / "run-log.json"
    audio_path.write_bytes(audio)
    log_path.write_text(json.dumps(run_log), encoding="utf-8")
    return audio_path, log_path, attempt_id, expected_id


def analyze_submission(parts, data_dir):
    total_started = time.perf_counter_ns()
    phase_started = time.perf_counter_ns()
    audio_path, log_path, attempt_id, expected_id = persist_submission(parts, data_dir)
    persistence_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    report_path = audio_path.parent / "report.analysis.html"
    command = [
        sys.executable, str(Path(__file__).with_name("analyze_laser_audio.py")), str(audio_path),
        "--run-log", str(log_path), "--attempt-id", attempt_id,
        "--expected-id", expected_id, "--html-report", str(report_path),
    ]
    phase_started = time.perf_counter_ns()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=ANALYSIS_TIMEOUT_SECONDS, check=False)
    analysis_process_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    if result.returncode != 0 or not report_path.is_file():
        raise RuntimeError("audio analysis failed; source recording and run log were retained")
    phase_started = time.perf_counter_ns()
    report = report_path.read_bytes()
    report_read_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    try:
        analysis_timing = json.loads(result.stdout).get("timing", {})
    except (UnicodeDecodeError, json.JSONDecodeError):
        analysis_timing = {}
    timing = {
        "persistence_ms": persistence_ms,
        "analysis_process_ms": analysis_process_ms,
        "report_read_ms": report_read_ms,
        "analyzer_total_ms": (time.perf_counter_ns() - total_started) // 1_000_000,
        "analysis": analysis_timing,
    }
    return report, timing


class AnalyzerHandler(BaseHTTPRequestHandler):
    server_version = "WainluxAudioAnalyzer/1"

    def authorized(self):
        expected = os.environ.get("AUDIO_ANALYZER_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def do_POST(self):
        if self.path != "/api/analyze":
            self.send_error(404)
            return
        if not os.environ.get("AUDIO_ANALYZER_TOKEN", ""):
            self.send_error(503, "analyzer token is not configured")
            return
        if not self.authorized():
            self.send_error(401, "invalid bearer token")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_error(413, "request exceeds upload limit")
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            self.send_error(415, "expected multipart/form-data")
            return
        request_started = time.perf_counter_ns()
        try:
            body = self.rfile.read(length)
            request_receive_ms = (time.perf_counter_ns() - request_started) // 1_000_000
            phase_started = time.perf_counter_ns()
            parts = parse_multipart(content_type, body)
            multipart_parse_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
            report, timing = analyze_submission(parts, Path(os.environ.get("AUDIO_DATA_DIR", "/data")))
        except (ValueError, UnicodeError) as error:
            self.send_error(400, str(error))
            return
        except subprocess.TimeoutExpired:
            self.send_error(504, "audio analysis timed out; recording and run log were retained for retry")
            return
        except (OSError, RuntimeError):
            self.send_error(502, "audio analysis failed")
            return
        timing.update({
            "request_receive_ms": request_receive_ms,
            "multipart_parse_ms": multipart_parse_ms,
            "request_total_ms": (time.perf_counter_ns() - request_started) // 1_000_000,
        })
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(report)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Audio-Timing", json.dumps(timing, separators=(",", ":")))
        self.end_headers()
        self.wfile.write(report)

    def do_GET(self):
        if self.path == "/health":
            if not self.authorized():
                self.send_error(401, "invalid bearer token")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"ready":1}')
            return
        prefix, suffix = "/api/reports/", "/report"
        if not self.path.startswith(prefix) or not self.path.endswith(suffix):
            self.send_error(404)
            return
        if not self.authorized():
            self.send_error(401, "invalid bearer token")
            return
        attempt_id = self.path[len(prefix):-len(suffix)]
        allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
        if not attempt_id or any(character not in allowed for character in attempt_id):
            self.send_error(404)
            return
        report_path = Path(os.environ.get("AUDIO_DATA_DIR", "/data")) / attempt_id / "report.analysis.html"
        try:
            report = report_path.read_bytes()
        except OSError:
            self.send_error(404, "report not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(report)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(report)

    def log_message(self, format, *args):
        print(f"{self.client_address[0]} {format % args}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.listen, args.port), AnalyzerHandler)
    print(f"Audio analyzer listening on {args.listen}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
