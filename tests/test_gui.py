"""Opt-in native window tests: BENCHMARK_GUI_TESTS=1 python -m unittest ..."""
from __future__ import annotations

import os
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from benchmark.storage import Store


@unittest.skipUnless(os.environ.get("BENCHMARK_GUI_TESTS") == "1", "Enable native window tests explicitly")
class WindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from benchmark.gui import BenchmarkWindow
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name).resolve()
        store = Store(self.run)
        store.initialize([{"company_id": "company-a", "name": "Первая компания", "inn": "0123456789", "sheet": "Компании", "row": 2},
                          {"company_id": "company-b", "name": "Вторая компания", "inn": "9876543210", "sheet": "Компании", "row": 3}], {"path": "/test.xlsx"})
        store.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = BenchmarkWindow(self.root, run_dir=self.run, check_auth=False)
        self.addCleanup(self.app.destroy)
        self.root.update_idletasks()

    def test_opening_window_does_not_launch_research(self):
        self.assertEqual(len(self.app.table.get_children()), 2)
        self.assertIsNone(self.app.action)
        self.assertEqual(self.app.metrics["Сохранено"].get(), "0")
        self.assertTrue(self.app.start_button.instate(["disabled"]))

    def test_search_and_company_details(self):
        self.app.search.set("9876")
        self.root.update_idletasks()
        self.assertEqual(self.app.table.get_children(), ("company-b",))
        self.app.table.selection_set("company-b")
        self.app.show_company()
        self.assertIn("Вторая компания", self.app.details.get("1.0", "end"))

    def test_start_uses_existing_run_and_batch(self):
        self.app.batch.set("20")
        self.app.auth_ready = True
        with patch.object(self.app.controller, "start") as start:
            self.app.start_research()
        args = start.call_args.args
        self.assertEqual(args[0], "run")
        self.assertIn(str(self.run), args[1])
        self.assertEqual(args[1][args[1].index("--max-companies") + 1], "20")
        self.assertTrue(self.app.new_button.instate(["disabled"]))

    def test_stop_and_close_wait_for_current_company(self):
        self.app.action = "run"
        self.app.close()
        self.assertTrue(self.app.close_when_finished)
        self.assertTrue(self.app.stop_requested)
        self.assertTrue((self.run / "stop.request").exists())
        self.assertFalse(self.app.closed)

    def test_invalid_batch_never_starts_research(self):
        self.app.batch.set("-1")
        with patch("benchmark.gui.messagebox.showerror") as error, patch.object(self.app.controller, "start") as start:
            self.app.start_research()
        error.assert_called_once()
        start.assert_not_called()

    def test_failed_launch_remains_visible_after_refresh(self):
        self.app.action = "run"
        self.app.finished({"action": "run", "code": 1, "output": "Ошибка запуска", "cancelled": False})
        self.app.refresh()
        self.assertIn("Не удалось завершить запуск", self.app.status_text.get())
        self.assertEqual(self.app.notebook.index(self.app.notebook.select()), 1)

    def test_new_run_dialog_has_room_for_controls(self):
        from benchmark.gui import NewRunDialog
        self.root.deiconify()
        self.root.update()
        dialog = NewRunDialog(self.app)
        self.root.update()
        self.assertGreaterEqual(dialog.window.winfo_height(), dialog.window.winfo_reqheight())
        dialog.window.destroy()

    def test_table_rows_are_visible_at_minimum_window_size(self):
        self.root.geometry("1000x760")
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.app.table.bbox("company-a"), "Первая строка таблицы скрыта")
        self.assertTrue(self.app.table.bbox("company-b"), "Вторая строка таблицы скрыта")
        _, y, _, height = self.app.table.bbox("company-b")
        self.assertLessEqual(y + height, self.app.table.winfo_height(), "Вторая строка таблицы обрезана")
        self.assertGreaterEqual(self.app.table.winfo_height(), 100)
        self.assertGreaterEqual(self.app.details.winfo_height(), 70)

    def test_window_auth_run_and_continue_with_fake_cli(self):
        self.app.binary = str(Path(__file__).parent.resolve() / "fake_codex.py")
        self.app.controller.env["BENCHMARK_TEST_MODE"] = "good"
        self.app.check_codex()
        def wait_idle():
            deadline = time.monotonic() + 8
            while self.app.action is not None and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertIsNone(self.app.action)
        wait_idle()
        self.assertTrue(self.app.auth_ready)
        self.app.batch.set("1")
        self.app.start_research()
        wait_idle()
        self.assertEqual(self.app.metrics["Сохранено"].get(), "1")
        self.app.start_research()
        wait_idle()
        self.assertEqual(self.app.metrics["Сохранено"].get(), "2")


if __name__ == "__main__":
    unittest.main()
