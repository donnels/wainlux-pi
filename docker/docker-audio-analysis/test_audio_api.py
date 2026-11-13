import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/usr/local/bin")
from audio_api import AnalyzerHandler, MAX_AUDIO_BYTES, analyze_submission, parse_multipart, persist_submission, validate_submission


def multipart(fields, boundary="test-boundary"):
    chunks = []
    for name, value, filename, content_type in fields:
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode() + (f'; filename="{filename}"'.encode() if filename else b"") + b"\r\n",
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            value if isinstance(value, bytes) else value.encode(),
            b"\r\n",
        ))
    chunks.append(f"--{boundary}--\r\n".encode())
    return parse_multipart(f"multipart/form-data; boundary={boundary}", b"".join(chunks))


def submission(attempt_id="1794321995-audio_power_sweep_low", step="audio_power_sweep_low", recording_id="321995", mode="real"):
    attempt = {"id": attempt_id, "mode": mode}
    if step is not None:
        attempt["step"] = step
    events = [
        {"event": "sync_marker", "attempt_id": attempt_id, "recording_id": recording_id},
        {"event": "ran", "attempt": attempt, "outcome": {"ok": True}},
    ]
    return multipart([
        ("audio", b"synthetic recording", "voice.m4a", "audio/mp4"),
        ("attempt_id", attempt_id, None, "text/plain"),
        ("expected_id", recording_id, None, "text/plain"),
        ("run_log", json.dumps(events), None, "application/json"),
    ])


class AudioApiTests(unittest.TestCase):
    def test_accepts_matching_completed_real_attempt(self):
        audio, suffix, attempt_id, expected_id, events = validate_submission(submission())
        self.assertEqual(audio, b"synthetic recording")
        self.assertEqual(suffix, ".m4a")
        self.assertEqual(attempt_id, "1794321995-audio_power_sweep_low")
        self.assertEqual(expected_id, "321995")
        self.assertEqual(len(events), 2)

    def test_audio_and_filtered_log_persist_under_attempt_on_analyzer(self):
        parts = submission()
        with tempfile.TemporaryDirectory() as directory:
            audio_path, log_path, attempt_id, expected_id = persist_submission(parts, directory)
            self.assertEqual(audio_path.parent, Path(directory) / attempt_id)
            self.assertEqual(audio_path.read_bytes(), b"synthetic recording")
            saved_events = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved_events), 2)
            self.assertEqual(expected_id, "321995")

    def test_analysis_returns_persistence_process_and_report_timing(self):
        def fake_analyzer(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 900)
            report_path = Path(command[command.index("--html-report") + 1])
            report_path.write_text("<html>synthetic report</html>", encoding="utf-8")
            timing = {"probe_ms":2,"decode_ms":3,"sync_correlation_ms":4,"playback_encode_ms":5,"analysis_compute_ms":14}
            return subprocess.CompletedProcess(command, 0, json.dumps({"timing":timing}).encode(), b"")

        with tempfile.TemporaryDirectory() as directory, patch("audio_api.subprocess.run", side_effect=fake_analyzer):
            report, timing = analyze_submission(submission(), directory)
        self.assertIn(b"synthetic report", report)
        for key in ("persistence_ms", "analysis_process_ms", "report_read_ms", "analyzer_total_ms"):
            self.assertIsInstance(timing[key], int)
            self.assertGreaterEqual(timing[key], 0)
        self.assertEqual(timing["analysis"]["sync_correlation_ms"], 4)

    def test_upload_limit_has_room_for_long_wifi_card_recording(self):
        self.assertEqual(MAX_AUDIO_BYTES, 64 * 1024 * 1024)

    def test_rejects_mismatched_recording_id(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_submission(submission(recording_id="999999"))

    def test_accepts_matching_wifi_card_run(self):
        audio, suffix, attempt_id, expected_id, _ = validate_submission(
            submission(attempt_id="1794321995-wifi-content", step="wifi-content")
        )
        self.assertEqual(audio, b"synthetic recording")
        self.assertEqual(suffix, ".m4a")
        self.assertEqual(attempt_id, "1794321995-wifi-content")
        self.assertEqual(expected_id, "321995")

    def test_rejects_simulator_run(self):
        with self.assertRaisesRegex(ValueError, "completed Real run"):
            validate_submission(submission(mode="simulator"))

    def test_rejects_missing_completed_run(self):
        parts = submission()
        parts["run_log"] = (json.dumps([{"event":"sync_marker","attempt_id":"1794321995-audio_power_sweep_low","recording_id":"321995"}]).encode(), None, "application/json")
        with self.assertRaisesRegex(ValueError, "sync marker and completed run"):
            validate_submission(parts)

    def test_api_requires_configured_bearer_token(self):
        with patch.dict(os.environ, {"AUDIO_ANALYZER_TOKEN":"secret"}):
            handler = object.__new__(AnalyzerHandler)
            handler.path = "/api/analyze"
            handler.headers = {"Authorization":"Bearer wrong"}
            handler.send_error = lambda status, message: setattr(handler, "error", (status, message))
            handler.do_POST()
            self.assertEqual(handler.error[0], 401)


if __name__ == "__main__":
    unittest.main()
