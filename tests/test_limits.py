"""Quota checks use fake Codex and never run a model turn."""
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.limits import quota_windows, read_limits

FAKE = str(Path(__file__).parent.resolve() / "fake_codex.py")


class LimitsTests(unittest.TestCase):
    def test_account_quota_is_read_without_model_calls(self):
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "good", "OPENAI_API_KEY": "must-not-be-forwarded"}):
            result = read_limits(FAKE)
        self.assertEqual([w["remaining"] for w in result["windows"]], [76, 96])
        self.assertEqual([w["name"] for w in result["windows"]], ["5 ч", "неделя"])
        self.assertEqual(set(result), {"windows", "fetched_at"})

    def test_missing_quota_and_timeout_are_optional(self):
        for mode in ["quota_unavailable", "quota_timeout"]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"BENCHMARK_TEST_MODE": mode}):
                start = time.monotonic()
                self.assertIsNone(read_limits(FAKE, timeout=.3))
                self.assertLess(time.monotonic() - start, 2)

    def test_cancelled_poll_does_not_start_codex(self):
        event = threading.Event(); event.set()
        with patch("benchmark.limits.subprocess.Popen") as start:
            self.assertIsNone(read_limits(FAKE, cancel_event=event))
        start.assert_not_called()

    def test_invalid_or_missing_windows_do_not_invent_available_capacity(self):
        for snapshot in [None, {}, {"primary": {"usedPercent": -1, "windowDurationMins": 300}},
                         {"primary": {"usedPercent": float("nan"), "windowDurationMins": 300}},
                         {"primary": {"usedPercent": 15}}]:
            self.assertEqual(quota_windows({"rateLimits": snapshot}), [])
        result = quota_windows({"rateLimitsByLimitId": {"codex": {"secondary": {"usedPercent": 100, "windowDurationMins": 10080}}}})
        self.assertEqual(result, [{"name": "неделя", "remaining": 0, "resets_at": None}])
