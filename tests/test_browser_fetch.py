from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from benchmark.browser_fetch import browser_command, render_pages


class BrowserProcessTests(unittest.TestCase):
    def test_engine_is_not_required_for_empty_work(self):
        with patch("benchmark.browser_fetch.subprocess.Popen", side_effect=AssertionError("No browser needed")):
            self.assertEqual(render_pages([]), {"pages": [], "attempts": []})
            self.assertEqual(render_pages(["https://example.org/"], timeout=1), {"pages": [], "attempts": []})

    def test_missing_engine_is_a_recoverable_attempt(self):
        with patch("benchmark.browser_fetch.importlib.util.find_spec", return_value=None):
            result = render_pages(["https://example.org/"])
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["attempts"][0]["outcome"], "engine_unavailable")

    def test_rendered_data_is_received_from_isolated_process(self):
        payload = {"pages": [{"url": "https://example.org/", "text": "Компания"}], "attempts": []}
        proc = Mock(returncode=0)
        proc.communicate.return_value = (json.dumps(payload), "")
        with patch("benchmark.browser_fetch.importlib.util.find_spec", return_value=object()), patch("benchmark.browser_fetch.subprocess.Popen", return_value=proc):
            result = render_pages(["https://example.org/"], timeout=10)
        self.assertEqual(result, payload)
        request = json.loads(proc.communicate.call_args.args[0])
        self.assertEqual(request["urls"], ["https://example.org/"])
        self.assertIn("deadline", request)

    def test_child_crash_or_bad_json_does_not_raise_into_company_queue(self):
        for code, output in [(1, ""), (0, "invalid"), (0, "[]"), (0, '{"pages":null,"attempts":[]}')]:
            with self.subTest(code=code, output=output):
                proc = Mock(returncode=code)
                proc.communicate.return_value = (output, "engine failed")
                with patch("benchmark.browser_fetch.importlib.util.find_spec", return_value=object()), patch("benchmark.browser_fetch.subprocess.Popen", return_value=proc):
                    result = render_pages(["https://example.org/"])
                self.assertEqual(result["pages"], [])
                self.assertEqual(result["attempts"][0]["outcome"], "engine_error")

    def test_timeout_stops_child_and_returns_failure_without_losing_parent(self):
        proc = Mock(pid=123)
        proc.communicate.return_value = ("", "")
        with patch("benchmark.browser_fetch.importlib.util.find_spec", return_value=object()), patch("benchmark.browser_fetch.subprocess.Popen", return_value=proc), patch("benchmark.browser_fetch.stop_child_process") as stop, patch("benchmark.browser_fetch.time.monotonic", side_effect=[0, 0, 6]):
            result = render_pages(["https://example.org/"], timeout=5)
        self.assertEqual(result["attempts"][0]["outcome"], "timeout")
        stop.assert_called_once_with(proc)

    def test_cancellation_interrupts_polling_browser_without_waiting_for_deadline(self):
        event = threading.Event()
        proc = Mock(pid=123)
        def communicate(*args, **kwargs):
            if not event.is_set():
                event.set()
                raise subprocess.TimeoutExpired("browser", .2)
            return "", ""
        proc.communicate.side_effect = communicate
        with patch("benchmark.browser_fetch.importlib.util.find_spec", return_value=object()), patch("benchmark.browser_fetch.subprocess.Popen", return_value=proc), patch("benchmark.browser_fetch.stop_child_process") as stop:
            result = render_pages(["https://example.org/"], cancel_event=event)
        self.assertEqual(result["attempts"][0]["outcome"], "cancelled")
        stop.assert_called_once_with(proc)

    def test_already_cancelled_browser_is_never_started(self):
        event = threading.Event(); event.set()
        with patch("benchmark.browser_fetch.subprocess.Popen") as launch:
            result = render_pages(["https://example.org/"], cancel_event=event)
        launch.assert_not_called()
        self.assertEqual(result["attempts"][0]["outcome"], "cancelled")

    def test_cancellation_reaps_real_slow_browser_bridge_process(self):
        event = threading.Event()
        timer = threading.Timer(.3, event.set)
        launched = []
        native_popen = subprocess.Popen
        def launch(*args, **kwargs):
            proc = native_popen(*args, **kwargs)
            launched.append(proc)
            return proc
        command = [sys.executable, "-c", "import sys,time;sys.stdin.read();time.sleep(15)"]
        timer.start()
        try:
            started = time.monotonic()
            with patch("benchmark.browser_fetch.importlib.util.find_spec", return_value=object()), patch("benchmark.browser_fetch.browser_command", return_value=command), patch("benchmark.browser_fetch.subprocess.Popen", side_effect=launch):
                result = render_pages(["https://example.org/"], cancel_event=event)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(result["attempts"][0]["outcome"], "cancelled")
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].poll())
        finally:
            timer.cancel()
            for proc in launched:
                if proc.poll() is None:
                    proc.kill(); proc.wait()

    def test_packaged_worker_does_not_use_system_python(self):
        executable = Path("installed") / "Benchmark-worker"
        with patch("benchmark.browser_fetch.frozen", return_value=True), patch("benchmark.browser_fetch.worker_executable", return_value=executable):
            self.assertEqual(browser_command(), [str(executable), "--benchmark-browser-worker"])
        from benchmark.app import main
        with patch("benchmark.browser_fetch.main", return_value=0) as browser:
            self.assertEqual(main(["--benchmark-browser-worker"]), 0)
        browser.assert_called_once()
