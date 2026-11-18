import unittest

from mcp_server import (
    audio_analysis_for_attempt,
    find_job,
    job_entries,
    trace_window,
    updates_since,
)


class McpReadOnlyTests(unittest.TestCase):
    def test_updates_returns_only_the_requested_cursor_page(self):
        events = [{"event": "mode"}, {"event": "ran"}, {"event": "verdict"}]
        first = updates_since(events, 0, 2)
        self.assertEqual(first, {"events": events[:2], "next_cursor": 2, "has_more": True})
        second = updates_since(events, first["next_cursor"], 2)
        self.assertEqual(second, {"events": events[2:], "next_cursor": 3, "has_more": False})

    def test_audio_summary_selects_only_the_requested_attempt(self):
        events = [
            {"event": "audio_analysis", "attempt_id": "attempt-a", "timing": {"pi_total_ms": 4}},
            {"event": "audio_analysis", "attempt_id": "attempt-b", "timing": {"pi_total_ms": 9}, "report_url": "/report"},
        ]
        self.assertEqual(audio_analysis_for_attempt(events, "attempt-b"), events[1])
        self.assertIsNone(audio_analysis_for_attempt(events, "attempt-c"))

    def test_updates_rejects_invalid_paging(self):
        with self.assertRaises(ValueError):
            updates_since([], -1, 10)
        with self.assertRaises(ValueError):
            updates_since([], 0, 501)

    def test_job_toc_summarizes_ran_events_without_copying_trace(self):
        events = [
            {
                "event": "ran",
                "attempt": {"id": "job-1", "step": "square", "goal": "test"},
                "outcome": {
                    "ok": True,
                    "saw": "assumed complete after 90 seconds",
                    "started": 10,
                    "ended": 20,
                    "receive_trace": {
                        "bytes_captured": 1024,
                        "truncated": True,
                        "events": [{"kind": "status_frame", "percent": 99}, {"kind": "rx_byte", "byte": "FF"}],
                    },
                },
            },
            {"event": "verdict", "id": "job-1", "ok": True, "comment": "stopped"},
        ]
        result = job_entries(events)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["attempt_id"], "job-1")
        self.assertEqual(result[0]["completion"], "silence_assumed")
        self.assertEqual(result[0]["last_progress"], 99)
        self.assertTrue(result[0]["trace_truncated"])
        self.assertNotIn("events", result[0])
        self.assertEqual(result[0]["verdict"]["comment"], "stopped")

    def test_trace_window_returns_bounded_head_and_tail(self):
        events = [{"event": "ran", "attempt": {"id": "job-1"}, "outcome": {
            "receive_trace": {"bytes_captured": 4, "truncated": False, "duration_ms": 50,
                              "events": [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}]}
        }}]
        self.assertEqual([item["n"] for item in trace_window(events, "job-1", 2, "head")["events"]], [1, 2])
        self.assertEqual([item["n"] for item in trace_window(events, "job-1", 2, "tail")["events"]], [3, 4])
        with self.assertRaises(ValueError):
            trace_window(events, "job-1", 101, "tail")
        with self.assertRaises(ValueError):
            find_job(events, "missing")


if __name__ == "__main__":
    unittest.main()
