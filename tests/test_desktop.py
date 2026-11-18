from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from openpyxl import Workbook

from benchmark.desktop import ProcessController, read_snapshot, save_report_copy, sheet_names
from benchmark.storage import Store

FAKE = Path(__file__).parent.resolve() / "fake_codex.py"


class DesktopTests(unittest.TestCase):
    def test_worker_exit_is_reported_even_if_descendant_retains_output_pipe(self):
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)']);print('done',flush=True)"
        with patch("benchmark.desktop.worker_command", return_value=[sys.executable, "-c", code]):
            start = time.monotonic()
            self.controller.start("fixture", [])
            events = self.wait_events()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(events[-1]["code"], 0)
        self.assertFalse(self.controller.busy)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.run = self.root / "research"
        self.source = self.root / "source.xlsx"
        wb = Workbook()
        wb.active.title = "Компании"
        wb.active.append(["Название компании", "ИНН", "Сайт"])
        wb.active.append(["Первая компания", "0123456789", "https://example.test"])
        wb.active.append(["Вторая компания", "9876543210", "https://example.test"])
        wb.save(self.source)
        wb.close()
        FAKE.chmod(0o755)
        env = os.environ.copy()
        env["BENCHMARK_TEST_MODE"] = "good"
        self.controller = ProcessController(env=env)
        self.addCleanup(self.cleanup_process)

    def cleanup_process(self):
        if self.controller.busy:
            self.controller.cancel()
            self.wait_events()

    def wait_events(self, timeout=10):
        events = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                event = self.controller.events.get(timeout=0.05)
            except queue.Empty:
                continue
            events.append(event)
            if event["type"] == "finished":
                return events
        self.fail("Worker did not finish within deadline")

    def initialize(self):
        self.controller.start("init", ["init", str(self.source), "--run-dir", str(self.run)])
        events = self.wait_events()
        self.assertEqual(events[-1]["code"], 0, events[-1]["output"])

    def test_process_import_then_snapshot_is_read_only(self):
        self.initialize()
        db = self.run / "state.sqlite3"
        before = db.read_bytes()
        first = read_snapshot(self.run)
        second = read_snapshot(self.run)
        self.assertEqual(first.total, 2)
        self.assertEqual(first.saved, 0)
        self.assertEqual(first.states, {"pending": 2})
        self.assertEqual(first.companies[0]["inn"], "0123456789")
        self.assertEqual(first.source["path"], str(self.source))
        self.assertEqual(first.states, second.states)
        self.assertEqual(before, db.read_bytes())

    def test_bad_directory_does_not_create_database(self):
        with self.assertRaisesRegex(ValueError, "не найден"):
            read_snapshot(self.root)
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_corrupt_database_reports_error(self):
        self.run.mkdir()
        (self.run / "state.sqlite3").write_text("not a database")
        with self.assertRaisesRegex(ValueError, "Не удалось"):
            read_snapshot(self.run)

    def test_sheet_picker_and_invalid_workbook(self):
        self.assertEqual(sheet_names(self.source), ["Компании"])
        invalid = self.root / "invalid.xlsx"
        invalid.write_text("not a workbook")
        with self.assertRaisesRegex(ValueError, "исправной"):
            sheet_names(invalid)

    def test_background_run_exports_and_resumes(self):
        self.initialize()
        args = ["run", "--run-dir", str(self.run), "--max-companies", "1", "--codex", str(FAKE)]
        self.controller.start("run", args)
        events = self.wait_events()
        self.assertEqual(events[-1]["code"], 0, events[-1]["output"])
        self.assertTrue(any("Первая компания" in e.get("text", "") for e in events))
        self.assertEqual(read_snapshot(self.run).saved, 1)
        self.controller.start("run", args)
        events = self.wait_events()
        self.assertEqual(events[-1]["code"], 0)
        snapshot = read_snapshot(self.run)
        self.assertEqual(snapshot.saved, 2)
        self.assertEqual([c["attempts"] for c in snapshot.companies], [1, 1])

    def test_report_save_and_source_protection(self):
        self.initialize()
        self.controller.start("export", ["export", "--run-dir", str(self.run)])
        self.assertEqual(self.wait_events()[-1]["code"], 0)
        destination = self.root / "copied-report.xlsx"
        self.assertEqual(save_report_copy(self.run, destination), destination)
        self.assertEqual(destination.read_bytes(), (self.run / "reports" / "report.xlsx").read_bytes())
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "исходный Excel"):
            save_report_copy(self.run, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_cli_auth_check_has_no_research_side_effects(self):
        self.controller.start("doctor", ["doctor", "--codex", str(FAKE)])
        final = self.wait_events()[-1]
        self.assertEqual(final["code"], 0)
        self.assertEqual(json.loads(final["output"])["login"], "ChatGPT")
        self.assertFalse(self.run.exists())

    def test_subscription_limit_keeps_saved_card(self):
        self.initialize()
        args = ["run", "--run-dir", str(self.run), "--max-companies", "1", "--codex", str(FAKE)]
        self.controller.start("run", args)
        self.assertEqual(self.wait_events()[-1]["code"], 0)
        self.controller.env["BENCHMARK_TEST_MODE"] = "limit"
        self.controller.start("run", args)
        self.assertEqual(self.wait_events()[-1]["code"], 75)
        snapshot = read_snapshot(self.run)
        self.assertEqual(snapshot.saved, 1)
        self.assertEqual(snapshot.states["pending"], 1)
        self.assertEqual(snapshot.last_stop["reason"], "subscription_limit")

    def test_double_start_is_refused(self):
        self.initialize()
        self.controller.env["BENCHMARK_TEST_MODE"] = "timeout"
        self.controller.start("run", ["run", "--run-dir", str(self.run), "--timeout", "1", "--max-companies", "1", "--codex", str(FAKE)])
        with self.assertRaisesRegex(ValueError, "текущей операции"):
            self.controller.start("doctor", ["doctor"])
        self.wait_events()

    @unittest.skipIf(os.name == "nt", "Windows uses process-tree termination; requires native Windows QA")
    def test_cancel_returns_current_to_queue_and_preserves_progress(self):
        self.initialize()
        self.controller.env["BENCHMARK_TEST_MODE"] = "timeout"
        self.controller.start("run", ["run", "--run-dir", str(self.run), "--codex", str(FAKE)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if read_snapshot(self.run).states.get("running"):
                break
            time.sleep(0.02)
        self.assertTrue(read_snapshot(self.run).states.get("running"))
        self.controller.cancel()
        final = self.wait_events()[-1]
        self.assertTrue(final["cancelled"])
        self.assertEqual(final["code"], 130, final["output"])
        self.assertEqual(read_snapshot(self.run).states, {"pending": 2})


if __name__ == "__main__":
    unittest.main()
