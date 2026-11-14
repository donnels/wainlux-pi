import base64
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/usr/local/bin")
from analyze_laser_audio import SAMPLE_RATE, align_run_log, classify_audio, find_cue_preamble, sync_timeline_events, wav_bytes, waveform_data, write_html


def tone(frequencies, duration_s, amplitude=0.075):
    count = round(SAMPLE_RATE * duration_s)
    return [int(sum(amplitude * 32767 * math.sin(2 * math.pi * frequency * sample / SAMPLE_RATE) for frequency in frequencies)) for sample in range(count)]


class AudioMarkerTests(unittest.TestCase):
    def test_decoded_sync_audio_has_timed_tone_digit_and_id_spans(self):
        marker = {
            "expected_id":"123456", "decoded_id":"123456", "matches_expected":True, "cue_start_s":1.0,
            "preamble_tones":[{"symbol":"TONE880","start_s":1.0,"end_s":1.12}],
            "digit_tones":[{"symbol":"1","start_s":1.5,"end_s":1.64},{"symbol":"2","start_s":1.7,"end_s":1.84}],
        }
        events = sync_timeline_events(marker)
        self.assertEqual([event["kind"] for event in events],["sync_cue","sync_tone","sync_digit","sync_digit"])
        self.assertEqual((events[0]["audio_time_s"],events[0]["end_s"]),(1.0,1.84))
        self.assertEqual(events[0]["label"],"ID 123456")
        self.assertIn("(match)",events[0]["metadata"])
        self.assertEqual(events[1]["label"],"880 Hz")
        self.assertEqual((events[2]["audio_time_s"],events[2]["end_s"]),(1.5,1.64))
        self.assertEqual(events[2]["label"],"1")

    def test_incomplete_sync_does_not_invent_decoded_id_or_digits(self):
        events = sync_timeline_events({"expected_id":"123456","cue_start_s":2.0,"preamble_tones":[{"symbol":"TONE660","start_s":2.0,"end_s":2.12}]})
        self.assertEqual(events[0]["label"],"ID not decoded")
        self.assertIn("expected ID 123456; decoded ID not decoded",events[0]["metadata"])
        self.assertFalse(any(event["kind"] == "sync_digit" for event in events))
        self.assertEqual(sync_timeline_events({}),[])

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

    def test_aligns_each_received_byte_and_decoded_frame(self):
        marker_ms = 1_000_000
        trace_start_ms = 1_000_100
        events = [
            {"event":"sync_marker","at_ms":marker_ms,"attempt_id":"1000000-frame","recording_id":"000000","cue_to_request_ms":2700},
            {"event":"ran","attempt":{"id":"1000000-frame"},"outcome":{"started":1001,"receive_trace":{"started_at_unix_ms":trace_start_ms,"events":[
                {"at_ms":10,"job_index":1,"phase":"completion_wait","kind":"rx_byte","byte":255,"quiet_ms":10},
                {"at_ms":20,"job_index":1,"phase":"completion_wait","kind":"status_frame","frame":"progress","frame_bytes":[255,255,0,100],"percent":100},
            ]}}},
        ]
        aligned = align_run_log(events,"1000000-frame","000000",2.0,2700)
        self.assertEqual([event["kind"] for event in aligned["events"]],["nominal_request","rx_byte","status_frame"])
        self.assertEqual([event["audio_time_s"] for event in aligned["events"][1:]],[4.81,4.82])
        self.assertEqual(aligned["events"][1]["byte"],255)
        self.assertEqual(aligned["events"][2]["frame_bytes"],[255,255,0,100])

    def test_waveform_spans_recording_and_overlays_log_markers(self):
        aligned = {"events":[
            {"kind":"nominal_request","audio_time_s":2.0},
            {"kind":"reported_complete","audio_time_s":9.5},
        ]}
        data = waveform_data([-32768,-1000,0,1000,32767] * 100)
        self.assertEqual(data["bin_ms"],2)
        self.assertGreater(len(data["minmax_base64"]),0)
        self.assertGreater(data["reference"],0)

    def test_hex_trace_and_transfer_metadata_match_pi_audit_format(self):
        events = [
            {"event":"sync_marker","at_ms":1000000,"attempt_id":"test","recording_id":"123456","cue_to_request_ms":2700},
            {"event":"ran","attempt":{"id":"test","stage_name":"front"},"outcome":{"started":1000,"ended":1004,"elapsed_ms":4321,"receive_trace":{"started_at_unix_ms":1000100,"events":[
                {"at_ms":10,"kind":"rx_byte","byte":"FF"},
                {"at_ms":20,"kind":"status_frame","frame_bytes":"FF FF 00 63","percent":99},
                {"at_ms":30,"kind":"transfer_start","metadata":"Band 1/2; power 1000 depth 10"},
            ]}}},
        ]
        aligned = align_run_log(events,"test","123456",2.0,2700)
        self.assertEqual(aligned["events"][1]["byte"],255)
        self.assertEqual(aligned["events"][2]["frame_bytes"],[255,255,0,99])
        self.assertIn("front / Band 1/2",aligned["events"][3]["metadata"])
        self.assertAlmostEqual(aligned["events"][-1]["audio_time_s"],9.021)

    def test_fine_envelope_retains_signed_low_amplitude_detail(self):
        import struct

        data = waveform_data([-3,7] * 8 + [-200,300] * 8)
        self.assertEqual(data["bin_ms"],2)
        self.assertEqual(struct.unpack("<hhhh",base64.b64decode(data["minmax_base64"])),(-3,7,-200,300))

    def test_sync_classification_stops_at_two_minutes(self):
        samples = [0] * (SAMPLE_RATE * 121)
        with patch("analyze_laser_audio.classify", return_value=None) as classify:
            self.assertEqual(classify_audio(samples), [])
        self.assertEqual(classify.call_count, 11997)

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
                    {"kind":"rx_byte","phase":"completion_wait","job_index":1,"byte":255,"quiet_ms":10,"audio_time_s":1.1},
                    {"kind":"status_frame","phase":"completion_wait","job_index":1,"frame_bytes":[255,255,0,100],"percent":100,"audio_time_s":1.2},
                    {"kind": "estimated_run_end", "audio_time_s": 2.0},
                ],
            }
            waveform = waveform_data([-1200,-400,600,1800] * 100)
            write_html(report_path, playback_wav, "sample.wav", "sample.m4a", report, aligned_log, waveform, {"analysis_compute_ms": 4})
            document = report_path.read_text(encoding="utf-8")
            self.assertIn(f'src="data:audio/wav;base64,{base64.b64encode(playback_wav).decode("ascii")}"', document)
            self.assertIn("Matched attempt attempt-1 to recording ID 123456", document)
            self.assertIn("Decoded ID: not decoded (not decoded)", document)
            self.assertIn("No receive trace was recorded", document)
            self.assertIn("Received packet / byte", document)
            self.assertIn("RX FF", document)
            self.assertIn("FF FF 00 64", document)
            self.assertIn('id="waveform"', document)
            self.assertIn('id="time-zoom"', document)
            self.assertNotIn('id="time-position"', document)
            self.assertIn('id="overview"', document)
            self.assertIn('id="event-detail"', document)
            self.assertIn('id="seek-selection"', document)
            self.assertIn('id="amplitude-reference"', document)
            self.assertIn('min="0.25"', document)
            self.assertIn('id="amplitude-zoom"', document)
            self.assertIn("Laser RX", document)
            self.assertIn("Host TX", document)
            self.assertIn("Jobs / bands", document)
            self.assertIn("Run / sync", document)
            self.assertIn("Jump to end", document)
            self.assertIn("selectEvent(candidate); return;", document)
            self.assertEqual(document.count("const player ="),1)
            self.assertIn("function drawWaveform", document)
            self.assertIn("amplitudeZoom", document)
            self.assertIn("Run-log alignment", document)
            self.assertIn("Analyzer processing time", document)
            self.assertNotIn("Audible activity candidates", document)
            self.assertIn("minmax_base64", document)


if __name__ == "__main__":
    unittest.main()