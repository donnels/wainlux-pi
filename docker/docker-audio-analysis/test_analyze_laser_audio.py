import base64
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/usr/local/bin")
from analyze_laser_audio import SAMPLE_RATE, align_run_log, classify_audio, find_cue_preamble, wav_bytes, write_html


def tone(frequencies, duration_s, amplitude=0.075):
    count = round(SAMPLE_RATE * duration_s)
    return [int(sum(amplitude * 32767 * math.sin(2 * math.pi * frequency * sample / SAMPLE_RATE) for frequency in frequencies)) for sample in range(count)]


class AudioMarkerTests(unittest.TestCase):
    def test_decodes_preamble_and_six_dtmf_digits(self):
        parts = [tone([660], 0.12, 0.18), [0] * 800, tone([880], 0.12, 0.18), [0] * 800, tone([660], 0.12, 0.18), [0] * 800, tone([880], 0.12, 0.18), [0] * 1120]
        digits = "321995"
        dtmf = {"0":(941,1336),"1":(697,1209),"2":(697,1336),"3":(697,1477),"4":(770,1209),"5":(770,1336),"6":(770,1477),"7":(852,1209),"8":(852,1336),"9":(852,1477)}
        for digit in digits:
            parts.extend((tone(dtmf[digit], 0.14), [0] * 640))
        segments = classify_audio([sample for part in parts for sample in part])
        preamble = find_cue_preamble(segments)
        decoded = "".join(segment["symbol"] for segment in segments if segment["symbol"].isdigit())
        self.assertEqual([segment["symbol"] for segment in preamble], ["TONE880", "TONE660", "TONE880"])
        self.assertEqual(decoded, digits)

    def test_aligns_run_duration_without_claiming_subsecond_log_precision(self):
        marker_ms = 1794322161889
        events = [
            {"event": "sync_marker", "at_ms": marker_ms, "attempt_id": "1794321995-frame", "recording_id": "321995", "cue_to_request_ms": 2700},
            {"event": "ran", "attempt": {"id": "1794321995-frame"}, "outcome": {"started": marker_ms // 1000, "ended": marker_ms // 1000 + 14}},
        ]
        aligned = align_run_log(events, "1794321995-frame", "321995", 15.8, 2700)
        self.assertEqual([event["audio_time_s"] for event in aligned["events"]], [18.5, 32.5])
        self.assertEqual([event["kind"] for event in aligned["events"]], ["nominal_request", "estimated_run_end"])

    def test_embeds_small_audio_for_file_url_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "sample.analysis.html"
            playback_wav = wav_bytes([0, 100, -100])
            report = {
                "audio": {"duration_s": 1.0, "codec": "aac", "sample_rate": 48000},
                "sync_marker": {},
                "interpretation_limit": "This report presents run logs and measured timings only.",
            }
            aligned_log = {
                "marker_found": True,
                "attempt_id": "attempt-1",
                "recording_id": "123456",
                "trace_found": False,
                "software_outcome": "frame acknowledged",
                "events": [
                    {"kind": "nominal_request", "audio_time_s": 1.0},
                    {"kind": "estimated_run_end", "audio_time_s": 2.0},
                ],
            }
            write_html(report_path, playback_wav, "sample.wav", "sample.m4a", report, aligned_log, {"analysis_compute_ms": 4})
            document = report_path.read_text(encoding="utf-8")
            self.assertIn(f'src="data:audio/wav;base64,{base64.b64encode(playback_wav).decode("ascii")}"', document)
            self.assertIn("Matched attempt attempt-1 to recording ID 123456", document)
            self.assertIn("No receive trace was recorded", document)
            self.assertIn("Run-log alignment", document)
            self.assertIn("Analyzer processing time", document)
            self.assertNotIn("Audible activity candidates", document)
            self.assertNotIn("waveform", document.lower())


if __name__ == "__main__":
    unittest.main()