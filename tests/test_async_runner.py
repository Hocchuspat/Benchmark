"""Concurrent research uses only fake Codex; SQLite commits stay coordinated."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark import codex
from benchmark.runner import run_queue
from benchmark.storage import Store

FAKE = Path(__file__).parent.resolve() / "fake_codex.py"


class ConcurrentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.company_count = 4
        self.run = self.create_run("run")
        env = patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "good"})
        env.start(); self.addCleanup(env.stop)

    def create_run(self, name, count=4):
        folder = self.root / name
        store = Store(folder)
        try:
            store.initialize([{"company_id": f"company-{i}", "name": f"Компания {i}", "inn": f"{1234567890 + i}",
                               "sheet": "Компании", "row": i + 2} for i in range(count)], {})
        finally:
            store.close()
        return folder

    def snapshot(self, folder=None):
        path = ((folder or self.run) / "state.sqlite3").resolve()
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM companies ORDER BY position")]

    def launch_background(self, **kwargs):
        outcome = {}
        def target():
            try:
                outcome["code"] = run_queue(self.run, binary=str(FAKE), emit=lambda _: None, **kwargs)
            except BaseException as exc:
                outcome["error"] = exc
        thread = threading.Thread(target=target)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        return thread, outcome

    def test_two_companies_overlap_and_fast_result_commits_before_slow_one(self):
        barrier = threading.Barrier(2)
        release = threading.Event()
        active, peak = 0, 0
        lock = threading.Lock()
        original = codex.execute
        def investigate(company, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            try:
                barrier.wait(timeout=4)
                result = original(company, *args, **kwargs)
                if company["company_id"] == "company-0":
                    release.wait(timeout=5)
                else:
                    (self.run / "stop.request").write_text("stop")
                return result
            finally:
                with lock:
                    active -= 1
        with patch("benchmark.codex.execute", side_effect=investigate):
            thread, outcome = self.launch_background()
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    rows = self.snapshot()
                    if rows[1]["card_json"]:
                        break
                    time.sleep(.02)
                self.assertIsNotNone(rows[1]["card_json"], outcome)
                self.assertEqual(rows[0]["state"], "running")
                self.assertIsNone(rows[0]["card_json"])
                self.assertEqual([row["attempts"] for row in rows[2:]], [0, 0])
            finally:
                release.set(); thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["code"], 0)
        self.assertEqual(peak, 2)
        rows = self.snapshot()
        self.assertEqual([row["state"] for row in rows], ["completed", "completed", "pending", "pending"])
        self.assertEqual([row["attempts"] for row in rows], [1, 1, 0, 0])

    def test_limit_pauses_new_work_but_preserves_other_inflight_result_and_resume(self):
        barrier = threading.Barrier(2)
        release = threading.Event()
        original = codex.execute
        def investigate(company, *args, **kwargs):
            barrier.wait(timeout=4)
            if company["company_id"] == "company-0":
                return {"error_kind": "subscription_limit", "error": "usage limit", "usage": {}, "web_calls": 0}
            release.wait(timeout=4)
            return original(company, *args, **kwargs)
        def emit(message):
            if "subscription_limit" in message:
                release.set()
        with patch("benchmark.codex.execute", side_effect=investigate):
            self.assertEqual(run_queue(self.run, binary=str(FAKE), emit=emit), 75)
        rows = self.snapshot()
        self.assertEqual([row["state"] for row in rows], ["pending", "completed", "pending", "pending"])
        self.assertEqual([row["attempts"] for row in rows], [1, 1, 0, 0])
        self.assertEqual(run_queue(self.run, binary=str(FAKE), emit=lambda _: None), 0)
        self.assertEqual([row["attempts"] for row in self.snapshot()], [2, 1, 1, 1])

    def test_batch_limit_counts_launched_jobs_and_token_budget_forces_one_worker(self):
        self.assertEqual(run_queue(self.run, binary=str(FAKE), max_companies=3, emit=lambda _: None), 0)
        self.assertEqual([row["attempts"] for row in self.snapshot()], [1, 1, 1, 0])
        folder = self.create_run("budget")
        self.assertEqual(run_queue(folder, binary=str(FAKE), token_budget=150, emit=lambda _: None), 0)
        self.assertEqual([row["attempts"] for row in self.snapshot(folder)], [1, 0, 0, 0])
        store = Store(folder)
        try:
            self.assertEqual(store.meta("last_stop")["workers"], 1)
        finally:
            store.close()

    def test_worker_exception_does_not_discard_other_company_results(self):
        original = codex.execute
        def investigate(company, *args, **kwargs):
            if company["company_id"] == "company-0":
                raise RuntimeError("simulated worker failure")
            return original(company, *args, **kwargs)
        with patch("benchmark.codex.execute", side_effect=investigate):
            run_queue(self.run, binary=str(FAKE), emit=lambda _: None)
        self.assertEqual([row["state"] for row in self.snapshot()], ["failed", "completed", "completed", "completed"])

    def test_consecutive_failures_stop_scheduling_and_drain_started_jobs(self):
        folder = self.create_run("failures", count=8)
        failure = {"error_kind": "execution_error", "error": "failed", "usage": {}, "web_calls": 0}
        with patch("benchmark.codex.execute", return_value=failure):
            self.assertEqual(run_queue(folder, binary=str(FAKE), emit=lambda _: None), 1)
        rows = self.snapshot(folder)
        attempted = sum(row["attempts"] for row in rows)
        self.assertGreaterEqual(attempted, 3)
        self.assertLessEqual(attempted, 4)
        self.assertTrue(all(row["state"] != "running" for row in rows))

    def test_ready_files_for_multiple_interrupted_jobs_recover_without_new_queries(self):
        folder = self.create_run("recovery", count=2)
        store = Store(folder)
        try:
            for row in store.rows():
                attempt = store.start(row["id"])
                company = json.loads(row["input_json"])
                codex.execute(company, folder / "attempts" / row["id"] / f"{attempt:03d}", str(FAKE))
        finally:
            store.close()
        with patch("benchmark.codex.execute", side_effect=AssertionError("Do not repeat completed research")):
            self.assertEqual(run_queue(folder, binary=str(FAKE), emit=lambda _: None), 0)
        self.assertEqual([row["attempts"] for row in self.snapshot(folder)], [1, 1])
        self.assertTrue(all(row["card_json"] for row in self.snapshot(folder)))

    def test_invalid_worker_count_is_rejected_before_authentication(self):
        with patch("benchmark.codex.check_login", side_effect=AssertionError("No authentication")):
            for count in [0, 5]:
                with self.subTest(count=count), self.assertRaises(ValueError):
                    run_queue(self.run, workers=count)

    def test_cli_defaults_process_whole_queue(self):
        from benchmark.cli import main, parser
        arguments = parser().parse_args(["run", "--run-dir", str(self.run)])
        self.assertEqual(arguments.max_companies, 0)
        self.assertEqual(arguments.workers, 2)
        self.assertEqual(main(["run", "--run-dir", str(self.run), "--codex", str(FAKE)]), 0)
        self.assertTrue(all(row["card_json"] for row in self.snapshot()))

    def test_interrupt_cancels_both_active_jobs_and_leaves_queue_resumable(self):
        import benchmark.runner as runner
        barrier = threading.Barrier(2)
        started = threading.Event()
        observed_cancellation = []
        def investigate(company, *args, **kwargs):
            barrier.wait(timeout=4); started.set()
            event = kwargs["cancel_event"]
            if event.wait(timeout=4):
                observed_cancellation.append(company["company_id"])
            return {"error_kind": "interrupted", "error": "Остановлено", "usage": {}, "web_calls": 0}
        original_wait = runner.wait
        interrupted = False
        def interrupt_once(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                self.assertTrue(started.wait(timeout=4))
                raise KeyboardInterrupt
            return original_wait(*args, **kwargs)
        with patch("benchmark.codex.execute", side_effect=investigate), patch("benchmark.runner.wait", side_effect=interrupt_once):
            self.assertEqual(run_queue(self.run, binary=str(FAKE), emit=lambda _: None), 130)
        self.assertEqual(sorted(observed_cancellation), ["company-0", "company-1"])
        self.assertTrue(all(row["state"] == "pending" for row in self.snapshot()))
        self.assertEqual([row["attempts"] for row in self.snapshot()], [1, 1, 0, 0])
        self.assertEqual(run_queue(self.run, binary=str(FAKE), emit=lambda _: None), 0)
        self.assertEqual([row["attempts"] for row in self.snapshot()], [2, 2, 1, 1])

    def test_cancellation_stops_codex_and_returns_retryable_result(self):
        event = threading.Event()
        timer = threading.Timer(.3, event.set)
        timer.start()
        try:
            started = time.monotonic()
            with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "timeout"}):
                result = codex.execute({"company_id": "cancel", "name": "Компания"}, self.root / "cancel", str(FAKE), timeout=10, cancel_event=event)
            self.assertEqual(result["error_kind"], "interrupted")
            self.assertLess(time.monotonic() - started, 3)
        finally:
            timer.cancel()

    def test_stop_quickly_cancels_parallel_jobs_keeps_saved_card_and_resumes(self):
        self.assertEqual(run_queue(self.run, binary=str(FAKE), max_companies=1, emit=lambda _: None), 0)
        saved = self.snapshot()[0]["card_json"]
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "timeout"}):
            thread, outcome = self.launch_background()
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if sum(row["state"] == "running" for row in self.snapshot()) == 2:
                        break
                    time.sleep(.02)
                self.assertEqual(sum(row["state"] == "running" for row in self.snapshot()), 2)
                started = time.monotonic()
                (self.run / "stop.request").write_text("stop")
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive(), outcome)
                self.assertLess(time.monotonic() - started, 4)
                self.assertEqual(outcome, {"code": 0})
            finally:
                (self.run / "stop.request").write_text("stop")
                thread.join(timeout=5)
        rows = self.snapshot()
        self.assertEqual(rows[0]["card_json"], saved)
        self.assertEqual([r["state"] for r in rows], ["completed", "pending", "pending", "pending"])
        store = Store(self.run)
        try:
            self.assertEqual(store.meta("last_stop")["reason"], "user_stop")
        finally:
            store.close()
        self.assertEqual(run_queue(self.run, binary=str(FAKE), emit=lambda _: None), 0)
        self.assertEqual([r["attempts"] for r in self.snapshot()], [1, 2, 2, 1])
