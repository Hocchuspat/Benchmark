"""Qt tests use an offscreen window and the fake CLI; never spend the subscription."""
from __future__ import annotations

import copy
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.storage import Store

QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(QT_AVAILABLE, "Install the gui extra for Qt tests")
class QtWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def setUp(self):
        from PySide6.QtCore import QSettings
        from benchmark.qt_gui import BenchmarkWindow
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.run_dir = self.root / "research"
        store = Store(self.run_dir)
        store.initialize([{"company_id": "company-a", "name": "Первая компания", "inn": "0123456789", "sheet": "Компании", "row": 2},
                          {"company_id": "company-b", "name": "Вторая компания", "inn": "9876543210", "sheet": "Компании", "row": 3}],
                         {"path": str(self.root / "source.xlsx")})
        store.close()
        self.settings = QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat)
        self.window = BenchmarkWindow(run_dir=self.run_dir, settings=self.settings, check_auth=False)
        self.window.show(); self.app.processEvents()
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        if self.window.controller.busy:
            self.window.controller.cancel(); self.wait_idle()
        if not self.window.closed:
            self.window.close(); self.app.processEvents()

    def wait_idle(self):
        deadline = time.monotonic() + 8
        while self.window.action is not None and time.monotonic() < deadline:
            self.app.processEvents(); self.window.tick(); time.sleep(0.01)
        self.assertIsNone(self.window.action, "Background action did not finish")

    def connect_fake(self):
        self.window.binary = str(Path(__file__).parent.resolve() / "fake_codex.py")
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "good"
        self.window.check_codex(); self.wait_idle()
        self.assertTrue(self.window.auth_ready)

    def complete_one(self):
        self.connect_fake(); self.window.batch.setCurrentText("1"); self.window.start_research(); self.wait_idle()

    def test_opening_is_read_only_and_does_not_start_research(self):
        before = (self.run_dir / "state.sqlite3").read_bytes()
        self.window.refresh(); self.window.refresh()
        self.assertEqual(self.window.proxy.rowCount(), 2)
        self.assertEqual(self.window.metrics["Сохранено"].value.text(), "0")
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIsNone(self.window.action)
        self.assertEqual(before, (self.run_dir / "state.sqlite3").read_bytes())

    def test_search_and_selection_show_the_right_company(self):
        self.window.search.setText("9876"); self.app.processEvents()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0))
        self.assertEqual(self.window.selected_id, "company-b")
        self.assertEqual(self.window.company_title.text(), "Вторая компания")
        self.window.search.clear()
        self.assertEqual(self.window.proxy.rowCount(), 2)

    def test_filters_distinguish_saved_pending_and_review(self):
        self.complete_one()
        self.window.show_filter({"pending"})
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.assertEqual(self.window.proxy.index(0, 0).data(), "Вторая компания")
        self.window.show_filter({"needs_review"})
        self.assertEqual(self.window.proxy.rowCount(), 0)
        self.window.show_filter({"completed"})
        self.assertEqual(self.window.proxy.rowCount(), 1)

    def test_run_then_continue_never_repeats_completed(self):
        self.complete_one()
        self.assertEqual(self.window.metrics["Сохранено"].value.text(), "1")
        self.window.start_research(); self.wait_idle()
        self.assertEqual(self.window.metrics["Сохранено"].value.text(), "2")
        self.assertEqual([c["attempts"] for c in self.window.snapshot.companies], [1, 1])

    def test_retry_button_researches_only_selected_company(self):
        self.complete_one()
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        self.assertTrue(self.window.retry_button.isEnabled())
        self.window.retry_button.click(); self.wait_idle()
        self.assertEqual([c["attempts"] for c in self.window.snapshot.companies], [2, 0])
        self.assertEqual(self.window.snapshot.states, {"completed": 1, "pending": 1})

    def test_retry_button_recovers_failed_company_when_no_pending_remain(self):
        self.connect_fake()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "invalid"
        self.window.batch.setCurrentText("0"); self.window.start_research(); self.wait_idle()
        self.assertEqual(self.window.snapshot.states, {"failed": 2})
        self.window.table.setCurrentIndex(self.window.proxy.index(1, 0)); self.app.processEvents()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.retry_button.isEnabled())
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "good"
        self.window.retry_button.click(); self.wait_idle()
        self.assertEqual([c["attempts"] for c in self.window.snapshot.companies], [1, 2])
        self.assertEqual(self.window.snapshot.states, {"failed": 1, "completed": 1})

    def test_selected_company_survives_snapshot_refresh(self):
        self.window.table.setCurrentIndex(self.window.proxy.index(1, 0))
        self.complete_one()
        self.assertEqual(self.window.selected_id, "company-b")
        self.assertEqual(self.window.company_title.text(), "Вторая компания")

    def test_theme_density_columns_and_layout_are_restored(self):
        from PySide6.QtCore import Qt
        from benchmark.qt_gui import BenchmarkWindow
        self.window.apply_theme("dark")
        self.window.density_action.setChecked(True)
        self.window.columns_menu.actions()[3].setChecked(False)
        self.window.columns_menu.actions()[4].setChecked(True)
        self.window.table.horizontalHeader().moveSection(0, 2)
        self.window.table.setColumnWidth(2, 175)
        self.window.toggle_sidebar(); self.window.batch.setCurrentText("37")
        self.window.save_preferences()
        other = BenchmarkWindow(run_dir=self.run_dir, settings=self.settings, check_auth=False)
        try:
            self.assertEqual(other.theme, "dark")
            self.assertTrue(other.compact_rows)
            self.assertTrue(other.table.isColumnHidden(3))
            self.assertEqual(other.splitter.orientation(), Qt.Orientation.Horizontal)
            self.assertTrue(other.sidebar_collapsed)
            self.assertEqual(other.batch.currentText(), "37")
            self.assertEqual(other.batch_size(), 37)
            self.assertEqual(other.table.horizontalHeader().visualIndex(0), 2)
            self.assertEqual(other.table.columnWidth(2), 175)
        finally:
            other.close()

    def test_column_visibility_and_order_can_be_reset(self):
        self.window.columns_menu.actions()[4].setChecked(True)
        self.window.table.horizontalHeader().moveSection(0, 2)
        self.window.toggle_sidebar()
        self.window.reset_layout()
        self.assertFalse(self.window.table.isColumnHidden(0))
        self.assertTrue(self.window.table.isColumnHidden(4))
        self.assertEqual(self.window.table.horizontalHeader().visualIndex(0), 0)
        self.assertTrue(self.window.inspector.isVisible())
        self.assertFalse(self.window.sidebar_collapsed)

    def test_sources_are_clickable_and_untrusted_html_is_escaped(self):
        from benchmark.ui_models import company_documents
        self.complete_one()
        company = copy.deepcopy(self.window.snapshot.companies[0])
        company["card"]["facts"]["primary_activity"]["value"] = '<script>alert("x")</script>'
        info, sources, _ = company_documents(company)
        self.assertNotIn("<script>", info)
        self.assertIn("&lt;script&gt;", info)
        self.assertIn('href="https://example.test/contacts"', sources)

    def test_only_web_links_are_opened(self):
        from PySide6.QtCore import QUrl
        with patch("benchmark.qt_gui.QDesktopServices.openUrl") as opener:
            self.window.open_source(QUrl("file:///private/secret"))
            self.window.open_source(QUrl("javascript:alert(1)"))
            opener.assert_not_called()
            self.window.open_source(QUrl("https://example.test"))
            opener.assert_called_once()

    def test_close_during_research_requests_graceful_stop(self):
        self.window.action = "run"
        self.window.close(); self.app.processEvents()
        self.assertTrue(self.window.stop_requested)
        self.assertTrue(self.window.close_when_finished)
        self.assertTrue((self.run_dir / "stop.request").exists())
        self.assertFalse(self.window.closed)
        self.window.action = None

    def test_processing_settings_are_forwarded_to_cli(self):
        self.window.auth_ready = True; self.window.batch.setCurrentText("20")
        self.window.timeout = 180; self.window.token_budget = 20000; self.window.model_name = "test-model"; self.window.retry_failed = True
        with patch.object(self.window.controller, "start") as start:
            self.window.start_research()
        args = start.call_args.args[1]
        self.assertEqual(args[args.index("--max-companies") + 1], "20")
        self.assertEqual(args[args.index("--timeout") + 1], "180")
        self.assertEqual(args[args.index("--token-budget") + 1], "20000")
        self.assertIn("--retry-failed", args)
        self.assertEqual(args[args.index("--model") + 1], "test-model")
        self.window.action = None

    def test_count_dropdown_and_custom_input_are_forwarded_to_cli(self):
        self.window.auth_ready = True
        self.assertTrue(self.window.batch.isEditable())
        for text, count in [("50", "50"), ("37", "37"), ("Вся очередь", "0"), ("0", "0")]:
            with self.subTest(text=text), patch.object(self.window.controller, "start") as start:
                self.window.batch.setCurrentText(text)
                self.window.start_research()
                args = start.call_args.args[1]
                self.assertEqual(args[args.index("--max-companies") + 1], count)
                self.window.action = None

    def test_invalid_count_does_not_start_processing_or_replace_saved_value(self):
        self.window.auth_ready = True
        self.window.batch.setCurrentText("37"); self.window.save_preferences()
        for text in ["", "abc", "-1", "100001"]:
            with self.subTest(text=text), patch.object(self.window.controller, "start") as start:
                self.window.batch.setCurrentText(text)
                self.window.start_research()
                start.assert_not_called()
                self.assertTrue(self.window.status.property("error"))
                self.window.save_preferences()
                self.assertEqual(self.settings.value("batch", type=int), 37)

    def test_failed_launch_is_visible(self):
        self.window.action = "run"
        self.window.finished({"action": "run", "code": 1, "cancelled": False, "output": "Ошибка"})
        self.assertIn("Ошибка запуска", self.window.status.text())
        self.assertTrue(self.window.table.isVisible())
        self.assertTrue(self.window.status.property("error"))

    def test_sorting_preserves_company_identity(self):
        from PySide6.QtCore import Qt
        self.window.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0))
        self.assertEqual(self.window.selected_id, "company-b")
        self.assertEqual(self.window.company_title.text(), "Вторая компания")

    def test_new_excel_creates_an_independent_queue(self):
        from openpyxl import Workbook
        from PySide6.QtWidgets import QDialog
        from benchmark.qt_gui import NewResearchDialog
        source = self.root / "new.xlsx"; destination = self.root / "new-research"
        book = Workbook(); book.active.title = "Компании"
        book.active.append(["Название компании", "ИНН", "Сайт"])
        book.active.append(["Импортированная компания", "0123456789", "example.test"])
        book.save(source); book.close()
        dialog = NewResearchDialog(self.window)
        dialog.source.setText(str(source)); dialog.destination.setText(str(destination)); dialog.validate()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        with patch("benchmark.qt_gui.NewResearchDialog", return_value=dialog), patch.object(dialog, "exec", return_value=QDialog.DialogCode.Accepted):
            self.window.new_research()
        self.wait_idle()
        self.assertEqual(self.window.snapshot.total, 1)
        self.assertEqual(self.window.run_dir, destination)
        self.assertEqual(self.window.snapshot.saved, 0)
        from benchmark.desktop import read_snapshot
        self.assertEqual(read_snapshot(self.run_dir).total, 2)

    def test_export_saves_a_copy_through_existing_backend(self):
        self.complete_one()
        destination = self.root / "saved.xlsx"
        with patch("benchmark.qt_gui.QFileDialog.getSaveFileName", return_value=(str(destination), "Excel (*.xlsx)")):
            self.window.save_report()
        self.wait_idle()
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), (self.run_dir / "reports" / "report.xlsx").read_bytes())

    def test_subscription_limit_pauses_without_losing_saved_card(self):
        self.complete_one()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "limit"
        self.window.start_research(); self.wait_idle()
        self.assertEqual(self.window.snapshot.saved, 1)
        self.assertEqual(self.window.snapshot.states["pending"], 1)
        self.assertIn("Лимит подписки", self.window.auth_label.text())

    def test_authentication_failure_enables_real_login_again(self):
        self.complete_one()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "auth_error"
        self.window.start_research(); self.wait_idle()
        self.assertFalse(self.window.auth_ready)
        self.assertEqual(self.window.login_button.title.text(), "Войти через ChatGPT")
        self.assertEqual(self.window.snapshot.saved, 1)
        self.assertFalse(self.window.start_button.isEnabled())

    def test_saved_metric_filters_without_mislabeling_the_results(self):
        self.complete_one()
        self.window.metrics["Сохранено"].click()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.assertEqual(self.window.table_title.text(), "Сохранённые карточки")

    def test_minimum_window_keeps_both_panels_visible(self):
        self.window.resize(1060, 700); self.app.processEvents()
        self.assertGreaterEqual(self.window.table.viewport().height(), 140)
        self.assertGreaterEqual(self.window.details.viewport().height(), 140)
        self.assertTrue(self.window.start_button.isVisible())
        self.assertTrue(self.window.inspector.isVisible())

    def test_old_hidden_vertical_layout_is_restored_as_visible_right_card(self):
        from PySide6.QtCore import Qt
        from benchmark.qt_gui import BenchmarkWindow
        self.window.splitter.setOrientation(Qt.Orientation.Vertical)
        self.window.inspector.hide()
        self.settings.setValue("splitter", self.window.splitter.saveState())
        self.settings.setValue("orientation", "below")
        self.settings.setValue("show_card", False)
        self.window.splitter.setOrientation(Qt.Orientation.Horizontal)
        self.window.inspector.show()
        other = BenchmarkWindow(run_dir=self.run_dir, settings=self.settings, check_auth=False)
        try:
            other.show(); self.app.processEvents()
            self.assertEqual(other.splitter.orientation(), Qt.Orientation.Horizontal)
            self.assertTrue(other.inspector.isVisible())
            self.assertGreater(other.inspector.x(), other.table.x())
            self.assertTrue(all(size > 0 for size in other.splitter.sizes()))
            other.save_preferences()
            self.assertFalse(self.settings.contains("orientation"))
            self.assertFalse(self.settings.contains("show_card"))
        finally:
            other.close()

    def test_failed_auth_check_is_visible_and_clears_after_reconnecting(self):
        self.window.finished({"action": "doctor", "code": 1, "output": "Ошибка: Нужен вход через ChatGPT.", "cancelled": False})
        self.assertIn("Нужен вход через ChatGPT", self.window.status.text())
        self.assertFalse(self.window.auth_ready)
        self.connect_fake()
        self.assertIsNone(self.window.notice)
        self.assertFalse(self.window.status.property("error"))

    def test_login_link_survives_without_a_log_screen(self):
        from PySide6.QtCore import QUrl
        self.window.action = "login"
        self.window.login_progress("https://example.test/oauth")
        self.assertFalse(self.window.browser_login.isVisible())
        self.window.login_progress("Open https://auth.openai.com/oauth/authorize?state=test in your browser")
        self.assertTrue(self.window.browser_login.isVisible())
        with patch("benchmark.qt_gui.QDesktopServices.openUrl") as opener:
            self.window.browser_login.click()
        opener.assert_called_once_with(QUrl("https://auth.openai.com/oauth/authorize?state=test"))
        self.window.finished({"action": "login", "code": 1, "output": "Вход отменён", "cancelled": False})
        self.assertFalse(self.window.browser_login.isVisible())
        self.assertEqual(self.window.login_url, "")
        self.assertIn("Вход не завершён", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
