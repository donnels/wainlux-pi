#!/usr/bin/env python3
"""HTTP upload API for recordings forwarded by the Pi service."""

import argparse
import hmac
import json
import os
import subprocess
import sys
import time
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 6 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_AUDIO_BYTES + MAX_LOG_BYTES + 64 * 1024
ANALYSIS_TIMEOUT_SECONDS = 900
STORE_LOCK = threading.Lock()
ANALYSIS_LOCK = threading.Lock()
REPORT_RESERVATION_BYTES = 56 * 1024 * 1024
TIMELINE_RESERVATION_BYTES = 16 * 1024 * 1024


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
        raise ValueError("run_log exceeds the 6 MiB limit")
    return audio, suffix, attempt_id, expected_id, run_log


def persist_submission(parts, data_dir):
    audio, suffix, attempt_id, expected_id, run_log = validate_submission(parts)
    attempt_dir = Path(data_dir) / attempt_id
    audio_path = attempt_dir / f"audio{suffix}"
    log_path = attempt_dir / "run-log.json"
    trace, summary = load_trace(attempt_dir)
    ran = next(event for event in run_log if event.get("event") == "ran" and (event.get("attempt") or {}).get("id") == attempt_id)
    ran.setdefault("outcome", {}).setdefault("receive_trace", {}).update(summary)
    ran["outcome"]["receive_trace"]["events"] = trace
    log_text = json.dumps(run_log)
    with STORE_LOCK:
        reserve_files(Path(data_dir), attempt_id, {audio_path.name:len(audio),log_path.name:len(log_text.encode()),".reservation":REPORT_RESERVATION_BYTES})
        attempt_dir.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(audio)
        log_path.write_text(log_text, encoding="utf-8")
        (attempt_dir / ".reservation").write_text(str(REPORT_RESERVATION_BYTES), encoding="ascii")
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
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=ANALYSIS_TIMEOUT_SECONDS, check=False)
        if report_path.is_file() and report_path.stat().st_size > REPORT_RESERVATION_BYTES:
            report_path.unlink()
            raise RuntimeError("audio report exceeded its reserved size")
    finally:
        (audio_path.parent / ".reservation").unlink(missing_ok=True)
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


def analyze_run_log(payload, data_dir):
    from analyze_laser_audio import write_run_log_report

    started = time.perf_counter_ns()
    if not isinstance(payload, dict) or not {"attempt_id", "run_log"}.issubset(payload) or set(payload) - {"attempt_id", "run_log", "trace_jsonl", "trace_summary"}:
        raise ValueError("required fields: attempt_id, run_log")
    attempt_id = payload["attempt_id"]
    if not isinstance(attempt_id, str) or not attempt_id or any(character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_" for character in attempt_id):
        raise ValueError("invalid attempt ID")
    events = payload["run_log"]
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("run_log must be an array")
    runs = [event for event in events if event.get("event") == "ran" and isinstance(event.get("attempt"), dict) and event["attempt"].get("id") == attempt_id]
    if len(runs) != 1 or not isinstance(runs[0].get("outcome"), dict):
        raise ValueError("requires a completed run")
    encoded = json.dumps([runs[0]])
    if len(encoded.encode()) > MAX_LOG_BYTES:
        raise ValueError("run_log exceeds the 6 MiB limit")
    trace = payload.get("trace_jsonl", "")
    if not isinstance(trace, str) or len(trace.encode()) > MAX_LOG_BYTES:
        raise ValueError("trace exceeds the 6 MiB request limit")
    trace_summary = payload.get("trace_summary", {})
    if not isinstance(trace_summary, dict):
        raise ValueError("trace_summary must be an object")
    trace_events = []
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("trace contains invalid JSON") from error
        if not isinstance(event, dict):
            raise ValueError("trace events must be objects")
        trace_events.append(event)
    run_for_report = json.loads(json.dumps(runs[0]))
    report_trace = run_for_report.setdefault("outcome", {}).setdefault("receive_trace", {})
    report_trace.update(trace_summary)
    if trace or not report_trace.get("events"):
        report_trace["events"] = trace_events
    encoded = json.dumps([run_for_report])
    if len(encoded.encode()) > MAX_LOG_BYTES:
        raise ValueError("run bundle exceeds the 6 MiB limit")
    directory = Path(data_dir) / attempt_id
    report_path = directory / "report.analysis.html"
    has_audio = any(directory.glob("audio.*"))
    files = {"run-log.json":len(encoded.encode()),"trace-summary.json":len(json.dumps(trace_summary).encode())}
    if trace: files["trace.jsonl"] = len(trace.encode())
    reserve_bytes = 0 if report_path.is_file() and has_audio else TIMELINE_RESERVATION_BYTES
    files[".reservation"] = reserve_bytes
    with STORE_LOCK:
        reserve_files(Path(data_dir), attempt_id, files)
        directory.mkdir(parents=True, exist_ok=True)
        if not (report_path.is_file() and has_audio):
            (directory / "run-log.json").write_text(encoded, encoding="utf-8")
        if trace:
            (directory / "trace.jsonl").write_text(trace, encoding="utf-8")
        (directory / "trace-summary.json").write_text(json.dumps(trace_summary), encoding="utf-8")
        (directory / ".reservation").write_text(str(reserve_bytes), encoding="ascii")
    try:
        if reserve_bytes:
            write_run_log_report(report_path, [run_for_report], attempt_id)
            if report_path.stat().st_size > reserve_bytes:
                report_path.unlink(missing_ok=True)
                raise OSError("timeline report exceeded its reserved size")
    finally:
        (directory / ".reservation").unlink(missing_ok=True)
    return report_path.read_bytes(), {"analyzer_total_ms":(time.perf_counter_ns() - started) // 1_000_000}


def _directory_bytes(path):
    return sum(_managed_file_size(item) for item in path.rglob("*") if item.is_file())


def _managed_file_size(path):
    if path.name == ".reservation":
        try:
            return max(0, int(path.read_text(encoding="ascii")))
        except (OSError, ValueError):
            return 0
    return path.stat().st_size


def load_trace(attempt_dir):
    events = []
    try:
        events = [json.loads(line) for line in (attempt_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError):
        events = []
    try:
        summary = json.loads((attempt_dir / "trace-summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    return events, summary


def reserve_files(data_dir, attempt_id, incoming_files):
    data_dir.mkdir(parents=True, exist_ok=True)
    rolling_limit = int(os.environ.get("ANALYSIS_ROLLING_MAX_BYTES", "1073741824"))
    pinned_limit = int(os.environ.get("ANALYSIS_PINNED_MAX_BYTES", "3221225472"))
    total_limit = int(os.environ.get("ANALYSIS_DATA_MAX_BYTES", "4294967296"))
    target = data_dir / attempt_id
    existing = _directory_bytes(target) if target.exists() else 0
    pinned = (target / ".pinned").exists()
    target_replaced = sum(_managed_file_size(target / name) for name in incoming_files if (target / name).is_file()) if target.exists() else 0
    incoming_bytes = max(0,sum(incoming_files.values()) - target_replaced)
    pinned_bytes = sum(_directory_bytes(item) for item in data_dir.iterdir() if item.is_dir() and (item / ".pinned").exists())
    if pinned and pinned_bytes + incoming_bytes > pinned_limit:
        raise OSError("pinned analyzer capacity exceeded")
    if not pinned:
        unpinned = sorted((item for item in data_dir.iterdir() if item.is_dir() and not (item / ".pinned").exists() and item.name != attempt_id), key=lambda item: item.stat().st_mtime)
        rolling = sum(_directory_bytes(item) for item in data_dir.iterdir() if item.is_dir() and not (item / ".pinned").exists()) + incoming_bytes
        while rolling > rolling_limit and unpinned:
            old = unpinned.pop(0)
            size = _directory_bytes(old)
            import shutil
            shutil.rmtree(old)
            rolling -= size
        if rolling > rolling_limit:
            raise OSError("unpinned analyzer capacity exceeded")
    total = sum(_directory_bytes(item) for item in data_dir.iterdir() if item.is_dir()) + incoming_bytes
    if total > total_limit:
        raise OSError("analyzer managed-artifact capacity exceeded")


class AnalyzerHandler(BaseHTTPRequestHandler):
    server_version = "WainluxAudioAnalyzer/1"

    def authorized(self):
        expected = os.environ.get("AUDIO_ANALYZER_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def do_POST(self):
        if self.path.startswith("/api/delete/"):
            if not self.authorized():
                self.send_error(401, "invalid bearer token")
                return
            attempt_id = self.path.removeprefix("/api/delete/")
            if not attempt_id or any(character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_" for character in attempt_id):
                self.send_error(400, "invalid attempt ID")
                return
            directory = Path(os.environ.get("AUDIO_DATA_DIR", "/data")) / attempt_id
            with ANALYSIS_LOCK, STORE_LOCK:
                if not directory.is_dir():
                    self.send_error(404, "attempt not found")
                    return
                if (directory / ".reservation").exists():
                    self.send_error(409, "attempt analysis is in progress")
                    return
                summary = {}
                try:
                    summary = json.loads((directory / "trace-summary.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
                import shutil
                shutil.rmtree(directory)
            response = json.dumps({"attempt_id":attempt_id,"deleted":True,"raster_payload_included":bool(summary.get("raster_payload_included"))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if self.path.startswith("/api/pin/"):
            if not self.authorized():
                self.send_error(401, "invalid bearer token")
                return
            attempt_id = self.path.removeprefix("/api/pin/")
            if not attempt_id or any(character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_" for character in attempt_id):
                self.send_error(400, "invalid attempt ID")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {"pinned"} or not isinstance(payload["pinned"], bool):
                    raise ValueError("pinned must be a boolean")
                directory = Path(os.environ.get("AUDIO_DATA_DIR", "/data")) / attempt_id
                if not directory.is_dir():
                    self.send_error(404, "attempt not found")
                    return
                with STORE_LOCK:
                    marker = directory / ".pinned"
                    if payload["pinned"]:
                        current = sum(_directory_bytes(item) for item in directory.parent.iterdir() if item.is_dir() and (item / ".pinned").exists())
                        if not marker.exists() and current + _directory_bytes(directory) > int(os.environ.get("ANALYSIS_PINNED_MAX_BYTES", "3221225472")):
                            self.send_error(507, "pinned artifact capacity exceeded")
                            return
                        marker.touch(exist_ok=True)
                    else:
                        marker.unlink(missing_ok=True)
                response = json.dumps({"attempt_id":attempt_id,"pinned":payload["pinned"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except (ValueError, OSError) as error:
                self.send_error(400, str(error))
            return
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
        log_only = content_type.split(";", 1)[0].strip().lower() == "application/json"
        if not log_only and not content_type.lower().startswith("multipart/form-data;"):
            self.send_error(415, "expected multipart/form-data or application/json")
            return
        if log_only and length > MAX_REQUEST_BYTES:
            self.send_error(413, "run bundle exceeds the request limit")
            return
        request_started = time.perf_counter_ns()
        try:
            body = self.rfile.read(length)
            request_receive_ms = (time.perf_counter_ns() - request_started) // 1_000_000
            phase_started = time.perf_counter_ns()
            if log_only:
                payload = json.loads(body)
                multipart_parse_ms = 0
                with ANALYSIS_LOCK:
                    report, timing = analyze_run_log(payload, Path(os.environ.get("AUDIO_DATA_DIR", "/data")))
            else:
                parts = parse_multipart(content_type, body)
                multipart_parse_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
                with ANALYSIS_LOCK:
                    report, timing = analyze_submission(parts, Path(os.environ.get("AUDIO_DATA_DIR", "/data")))
        except (ValueError, UnicodeError) as error:
            self.send_error(400, str(error))
            return
        except subprocess.TimeoutExpired:
            self.send_error(504, "audio analysis timed out; recording and run log were retained for retry")
            return
        except OSError as error:
            self.send_error(507, str(error))
            return
        except RuntimeError:
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
        if self.path == "/api/retention":
            if not self.authorized():
                self.send_error(401, "invalid bearer token")
                return
            data_dir = Path(os.environ.get("AUDIO_DATA_DIR", "/data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            attempts = sorted((item for item in data_dir.iterdir() if item.is_dir()), key=lambda item:item.stat().st_mtime)
            managed = sum(_directory_bytes(item) for item in attempts)
            pinned = sum(_directory_bytes(item) for item in attempts if (item / ".pinned").exists())
            unpinned = managed - pinned
            payload = json.dumps({"managed_bytes":managed,"pinned_bytes":pinned,"rolling_bytes":unpinned,"total_limit_bytes":int(os.environ.get("ANALYSIS_DATA_MAX_BYTES","4294967296")),"rolling_limit_bytes":int(os.environ.get("ANALYSIS_ROLLING_MAX_BYTES","1073741824")),"pinned_limit_bytes":int(os.environ.get("ANALYSIS_PINNED_MAX_BYTES","3221225472")),"attempt_count":len(attempts),"oldest_attempt":attempts[0].name if attempts else None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        retention_prefix = "/api/retention/"
        if self.path.startswith(retention_prefix):
            if not self.authorized():
                self.send_error(401, "invalid bearer token")
                return
            attempt_id = self.path[len(retention_prefix):]
            allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
            directory = Path(os.environ.get("AUDIO_DATA_DIR", "/data")) / attempt_id
            if not attempt_id or any(character not in allowed for character in attempt_id) or not directory.is_dir():
                self.send_error(404, "attempt not found")
                return
            files = sorted(item.name for item in directory.iterdir() if item.is_file() and item.name != ".pinned")
            summary = {}
            try:
                summary = json.loads((directory / "trace-summary.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            payload = json.dumps({"attempt_id":attempt_id,"pinned":(directory / ".pinned").exists(),"artifacts":files,"bytes":_directory_bytes(directory),"raster_payload_included":bool(summary.get("raster_payload_included"))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
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
    for reservation in Path(os.environ.get("AUDIO_DATA_DIR", "/data")).glob("*/.reservation"):
        reservation.unlink(missing_ok=True)
    server = ThreadingHTTPServer((args.listen, args.port), AnalyzerHandler)
    print(f"Audio analyzer listening on {args.listen}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
