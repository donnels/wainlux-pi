import unittest

from mcp_server import audio_analysis_for_attempt, updates_since


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


if __name__ == "__main__":
    unittest.main()