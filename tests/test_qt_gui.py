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
        self.connect_fake(); self.window.batch.setText("1"); self.window.start_research(); self.wait_idle()

    def test_opening_is_read_only_and_does_not_start_research(self):
        before = (self.run_dir / "state.sqlite3").read_bytes()
        self.window.refresh(); self.window.refresh()
        self.assertEqual(self.window.proxy.rowCount(), 2)
        self.assertEqual(self.window.metrics["Обработано"].value.text(), "0")
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIsNone(self.window.action)
        self.assertEqual(before, (self.run_dir / "state.sqlite3").read_bytes())

    def test_default_start_shows_upload_page_even_with_a_saved_last_run(self):
        from benchmark.qt_gui import BenchmarkWindow
        self.settings.setValue("last_run", str(self.run_dir))
        with patch("benchmark.qt_gui.read_snapshot", side_effect=AssertionError("No automatic opening")):
            other = BenchmarkWindow(settings=self.settings, check_auth=False)
        try:
            other.show(); self.app.processEvents()
            self.assertIsNone(other.run_dir)
            self.assertIsNone(other.snapshot)
            self.assertEqual(other.pages.currentWidget(), other.welcome)
            self.assertTrue(other.upload_button.isVisible())
            self.assertEqual(other.upload_button.text(), "Загрузить Excel")
            self.assertFalse(other.table.isVisible())
            self.assertFalse(other.inspector.isVisible())
            self.assertFalse(hasattr(other, "refresh_button"))
            from PySide6.QtWidgets import QDialog
            with patch("benchmark.qt_gui.NewResearchDialog") as dialog:
                dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
                other.upload_button.click()
                dialog.assert_called_once_with(other)
        finally:
            other.close()

    def test_explicit_open_switches_upload_page_to_research(self):
        from benchmark.qt_gui import BenchmarkWindow
        other = BenchmarkWindow(settings=self.settings, check_auth=False)
        try:
            other.show(); self.app.processEvents()
            self.assertEqual(other.pages.currentWidget(), other.welcome)
            self.assertTrue(other.load_run(self.run_dir))
            self.app.processEvents()
            self.assertEqual(other.pages.currentWidget(), other.research_page)
            self.assertTrue(other.table.isVisible())
            self.assertFalse(hasattr(other, "refresh_button"))
        finally:
            other.close()

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
        self.window.show_filter({"unprocessed"})
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.assertEqual(self.window.proxy.index(0, 0).data(), "Вторая компания")
        self.window.show_filter({"processed"})
        self.assertEqual(self.window.proxy.rowCount(), 1)

    def test_run_then_continue_never_repeats_completed(self):
        self.complete_one()
        self.assertEqual(self.window.metrics["Обработано"].value.text(), "1")
        self.window.start_research(); self.wait_idle()
        self.assertEqual(self.window.metrics["Обработано"].value.text(), "2")
        self.assertEqual([c["attempts"] for c in self.window.snapshot.companies], [1, 1])

    def test_site_field_copy_action_copies_selected_company_url(self):
        self.complete_one()
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        site = self.window.snapshot.companies[0]["site"]
        self.assertEqual(self.window.company_site.text(), site)
        self.assertTrue(self.window.copy_site_action.isEnabled())
        self.app.clipboard().setText("previous")
        self.window.copy_site_action.trigger()
        self.assertEqual(self.app.clipboard().text(), site)
        self.window.table.setCurrentIndex(self.window.proxy.index(1, 0)); self.app.processEvents()
        self.assertEqual(self.window.company_site.text(), "")
        self.assertFalse(self.window.copy_site_action.isEnabled())
        self.window.copy_site_action.trigger()
        self.assertEqual(self.app.clipboard().text(), site)

    def test_site_cell_copy_uses_correct_company_after_sorting(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        companies = copy.deepcopy(self.window.snapshot.companies)
        companies[0]["site"] = "http://first.example.org/"
        companies[1]["site"] = "https://second.example.org/contacts"
        self.window.model.replace(companies)
        self.window.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        index = self.window.proxy.index(0, 3)
        self.window.table.scrollTo(index); self.app.processEvents()
        self.assertEqual(index.data(Qt.ItemDataRole.UserRole), companies[1]["site"])
        rect = self.window.table.visualRect(index)
        delegate = self.window.table.itemDelegateForColumn(3)
        self.app.clipboard().setText("previous")
        QTest.mouseClick(self.window.table.viewport(), Qt.MouseButton.LeftButton, pos=delegate.copy_rect(rect).center())
        self.assertEqual(self.app.clipboard().text(), companies[1]["site"])

    def test_copy_feedback_animates_resets_and_tracks_site_when_selection_changes(self):
        from PySide6.QtTest import QTest
        self.complete_one()
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        site = self.window.company_site.text()
        self.window.copy_site_action.trigger()
        self.assertEqual(self.window.copy_site_action.toolTip(), "Скопировано")
        QTest.qWait(100)
        self.assertGreater(self.window.copy_feedback(site), 0)
        self.assertLess(self.window.copy_feedback(site), 1)
        self.assertIsNone(self.window.copy_feedback("https://another.example.org/"))
        self.window.copy_site_action.trigger()
        self.assertEqual(self.app.clipboard().text(), site)
        self.window.table.setCurrentIndex(self.window.proxy.index(1, 0)); self.app.processEvents()
        self.assertEqual(self.window.copy_site_action.toolTip(), "Копировать сайт")
        self.assertFalse(self.window.copy_site_action.isEnabled())
        QTest.qWait(1500)
        self.assertIsNone(self.window.copy_feedback(site))
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        self.assertEqual(self.window.copy_site_action.toolTip(), "Копировать сайт")

    def test_processing_clock_accumulates_runs_excludes_pauses_and_resets_on_open(self):
        self.connect_fake()
        self.window.timer.stop()
        with patch("benchmark.qt_gui.monotonic", return_value=100) as clock, patch.object(self.window.controller, "start"):
            self.window.start_research()
            clock.return_value = 171
            self.window.tick()
            self.assertEqual(self.window.processing_time.text(), "Время обработки · 00:01:11")
            self.window.finished({"action": "run", "code": 0, "output": "", "cancelled": False})
            clock.return_value = 2000
            self.window.tick()
            self.assertEqual(self.window.processing_time.text(), "Время обработки · 00:01:11")
            self.window.start_research()
            clock.return_value = 2009
            self.window.finished({"action": "run", "code": 0, "output": "", "cancelled": False})
            self.assertEqual(self.window.processing_time.text(), "Время обработки · 00:01:20")
            self.window.check_codex()
            clock.return_value = 3000
            self.window.finished({"action": "doctor", "code": 0, "output": "", "cancelled": False})
            self.assertEqual(self.window.processing_time.text(), "Время обработки · 00:01:20")
            self.window.load_run(self.run_dir)
            self.assertEqual(self.window.processing_time.text(), "Время обработки · 00:00:00")

    def test_stop_enables_continue_quickly_even_before_any_result_is_saved(self):
        self.connect_fake()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "timeout"
        self.window.start_research()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.processEvents(); self.window.tick()
            if self.window.snapshot.states.get("running") == 2:
                break
            time.sleep(.01)
        self.assertEqual(self.window.snapshot.states.get("running"), 2)
        started = time.monotonic()
        self.window.stop_button.click(); self.wait_idle()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.window.snapshot.saved, 0)
        self.assertEqual(self.window.snapshot.states, {"pending": 2})
        self.assertEqual(self.window.snapshot.last_stop["reason"], "user_stop")
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertEqual(self.window.start_button.text(), "Продолжить")
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "good"
        self.window.start_button.click(); self.wait_idle()
        self.assertEqual(self.window.snapshot.saved, 2)
        self.assertEqual([c["attempts"] for c in self.window.snapshot.companies], [2, 2])

    def test_report_ready_count_is_shown_in_saved_metric(self):
        self.complete_one()
        self.assertEqual(self.window.metrics["Обработано"].value.text(), "1")
        row = self.window.snapshot.companies[0]
        self.assertEqual(row["identity"], "confirmed_by_inn")
        self.assertEqual(self.window.model.index(0, 2).data(), "Обработан")
        self.assertEqual(self.window.model.columnCount(), 6)

    def test_partial_result_has_processed_status_without_a_notes_tab(self):
        self.connect_fake()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "missing_activity"
        self.window.batch.setText("1"); self.window.start_research(); self.wait_idle()
        self.assertEqual(self.window.snapshot.companies[0]["state"], "needs_review")
        self.assertEqual(self.window.model.index(0, 2).data(), "Обработан")
        self.assertEqual(self.window.model.index(1, 2).data(), "Не обработан")
        self.assertEqual(self.window.metrics["Обработано"].value.text(), "1")
        self.assertEqual(self.window.metrics["Не обработано"].value.text(), "1")
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        self.assertEqual(self.window.detail_tabs.count(), 2)
        self.assertIn("Нет данных.", self.window.details.toPlainText())
        self.assertNotIn("Описание деятельности не найдено", self.window.details.toPlainText())
        self.assertFalse(hasattr(self.window, "notes"))

    def test_failed_retry_with_saved_result_remains_processed(self):
        self.complete_one()
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "invalid"
        self.window.retry_button.click(); self.wait_idle()
        self.assertEqual(self.window.snapshot.companies[0]["state"], "failed")
        self.assertEqual(self.window.model.index(0, 2).data(), "Обработан")
        self.window.show_filter({"processed"})
        self.assertEqual(self.window.proxy.rowCount(), 1)

    def test_header_login_and_compact_rows_replace_sidebar_view_and_refresh(self):
        self.assertFalse(hasattr(self.window, "sidebar"))
        self.assertFalse(hasattr(self.window, "view_button"))
        self.assertTrue(self.window.login_button.isVisible())
        self.assertFalse(hasattr(self.window, "refresh_button"))
        self.assertEqual(self.window.table.verticalHeader().defaultSectionSize(), 36)
        self.assertEqual(self.window.login_button.parentWidget(), self.window.workspace)
        self.assertEqual([b.text() for b, _ in self.window.chips], ["Все", "Обрабатывается", "Обработан", "Не обработан"])

    def test_active_parallel_companies_show_processing_until_saved(self):
        self.connect_fake()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "slow"
        self.window.start_research()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.processEvents(); self.window.tick()
            if self.window.snapshot.states.get("running") == 2:
                break
            time.sleep(.01)
        self.assertEqual(self.window.snapshot.states.get("running"), 2)
        self.assertEqual([self.window.model.index(i, 2).data() for i in range(2)], ["Обрабатывается", "Обрабатывается"])
        self.window.show_filter({"running"})
        self.assertEqual(self.window.proxy.rowCount(), 2)
        self.window.table.setCurrentIndex(self.window.proxy.index(0, 0)); self.app.processEvents()
        self.assertIn("Обрабатывается", self.window.company_meta.text())
        self.wait_idle()
        self.assertEqual(self.window.proxy.rowCount(), 0)
        self.assertEqual([self.window.model.index(i, 2).data() for i in range(2)], ["Обработан", "Обработан"])

    def test_researching_saved_company_temporarily_overrides_processed_label(self):
        from benchmark.ui_models import CompanyModel
        self.complete_one()
        company = copy.deepcopy(self.window.snapshot.companies[0])
        company["state"] = "running"
        model = CompanyModel(); model.replace([company])
        self.assertEqual(model.index(0, 2).data(), "Обрабатывается")
        self.window.show_company(company)
        self.assertIn("Обрабатывается", self.window.company_meta.text())
        company["state"] = "failed"
        model.replace([company])
        self.assertEqual(model.index(0, 2).data(), "Обработан")

    def test_logout_uses_codex_and_keeps_saved_research(self):
        self.complete_one()
        self.assertEqual(self.window.login_button.text(), "Выйти из ChatGPT")
        attempts = [company["attempts"] for company in self.window.snapshot.companies]
        self.window.login_button.click(); self.wait_idle()
        self.assertFalse(self.window.auth_ready)
        self.assertEqual(self.window.login_button.text(), "Войти через ChatGPT")
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertEqual(self.window.snapshot.saved, 1)
        self.assertEqual([company["attempts"] for company in self.window.snapshot.companies], attempts)
        self.assertIn("вышли", self.window.status.text())

    def test_failed_logout_does_not_blindly_clear_connection_state(self):
        self.connect_fake()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "logout_error"
        self.window.login_button.click(); self.wait_idle()
        self.assertTrue(self.window.auth_ready)
        self.assertEqual(self.window.login_button.text(), "Выйти из ChatGPT")
        self.assertTrue(self.window.status.property("error"))

    def test_logout_is_disabled_during_research(self):
        self.window.auth_ready = True; self.window.action = "run"
        self.window.update_controls()
        self.assertFalse(self.window.login_button.isEnabled())
        with patch.object(self.window.controller, "start") as start:
            self.window.logout(); start.assert_not_called()
        self.window.action = None

    def test_whole_queue_is_default_even_with_legacy_batch_setting(self):
        from benchmark.qt_gui import BenchmarkWindow
        self.settings.setValue("batch", 10)
        other = BenchmarkWindow(run_dir=self.run_dir, settings=self.settings, check_auth=False)
        try:
            self.assertEqual(other.batch.text(), "Вся очередь")
            self.assertEqual(other.batch_size(), 0)
            args = other.run_arguments(other.batch_size())
            self.assertEqual(args[args.index("--max-companies") + 1], "0")
        finally:
            other.close()

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
        self.window.batch.setText("0"); self.window.start_research(); self.wait_idle()
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
        self.settings.setValue("compact_rows", False)
        self.window.columns_menu.actions()[3].setChecked(False)
        self.window.columns_menu.actions()[4].setChecked(True)
        self.window.table.horizontalHeader().moveSection(0, 2)
        self.window.table.setColumnWidth(2, 175)
        self.window.batch.setText("37")
        self.window.save_preferences()
        other = BenchmarkWindow(run_dir=self.run_dir, settings=self.settings, check_auth=False)
        try:
            self.assertEqual(other.theme, "dark")
            self.assertTrue(other.compact_rows)
            self.assertTrue(other.table.isColumnHidden(3))
            self.assertEqual(other.splitter.orientation(), Qt.Orientation.Horizontal)
            self.assertFalse(hasattr(other, "sidebar"))
            self.assertEqual(other.table.verticalHeader().defaultSectionSize(), 36)
            self.assertEqual(other.batch.text(), "37")
            self.assertEqual(other.batch_size(), 37)
            self.assertEqual(other.table.horizontalHeader().visualIndex(0), 2)
            self.assertEqual(other.table.columnWidth(2), 175)
        finally:
            other.close()

    def test_column_visibility_and_order_can_be_reset(self):
        self.window.columns_menu.actions()[4].setChecked(True)
        self.window.table.horizontalHeader().moveSection(0, 2)
        self.window.reset_layout()
        self.assertFalse(self.window.table.isColumnHidden(0))
        self.assertTrue(self.window.table.isColumnHidden(4))
        self.assertEqual(self.window.table.horizontalHeader().visualIndex(0), 0)
        self.assertTrue(self.window.inspector.isVisible())
        self.assertFalse(hasattr(self.window, "sidebar"))

    def test_sources_are_clickable_and_untrusted_html_is_escaped(self):
        from benchmark.ui_models import company_documents
        self.complete_one()
        company = copy.deepcopy(self.window.snapshot.companies[0])
        company["card"]["facts"]["primary_activity"]["value"] = '<script>alert("x")</script>'
        info, sources = company_documents(company)
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
        self.window.auth_ready = True; self.window.batch.setText("20")
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
        self.assertFalse(self.window.batch.isReadOnly())
        for text, count in [("50", "50"), ("37", "37"), ("Вся очередь", "0"), ("0", "0")]:
            with self.subTest(text=text), patch.object(self.window.controller, "start") as start:
                self.window.batch.setText(text)
                self.window.start_research()
                args = start.call_args.args[1]
                self.assertEqual(args[args.index("--max-companies") + 1], count)
                self.window.action = None

    def test_count_can_be_typed_pasted_and_kept_during_refresh(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        field = self.window.batch
        field.setFocus(); field.selectAll(); QTest.keyClicks(field, "37")
        self.assertEqual(field.text(), "37")
        QTest.qWait(750)
        self.window.refresh()
        self.assertEqual(field.text(), "37")
        self.assertEqual(self.window.batch_size(), 37)
        previous = self.app.clipboard().text()
        self.addCleanup(lambda: self.app.clipboard().setText(previous))
        self.app.clipboard().setText("125")
        field.selectAll(); field.paste()
        self.assertEqual(field.text(), "125")
        self.assertEqual(self.window.batch_size(), 125)
        QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertEqual(field.text(), "125")

    def test_count_stays_editable_during_processing_for_the_next_run(self):
        from PySide6.QtTest import QTest
        self.window.auth_ready = True; self.window.batch.setText("10")
        with patch.object(self.window.controller, "start") as start:
            self.window.start_research()
            first_args = start.call_args.args[1]
            self.assertTrue(self.window.batch.isEnabled())
            self.window.batch.setFocus(); self.window.batch.selectAll()
            QTest.keyClicks(self.window.batch, "25")
            self.window.refresh()
            self.assertEqual(self.window.batch_size(), 25)
            self.assertEqual(first_args[first_args.index("--max-companies") + 1], "10")
            self.assertFalse(self.window.start_button.isEnabled())
            self.window.action = None
            self.window.start_research()
            args = start.call_args.args[1]
            self.assertEqual(args[args.index("--max-companies") + 1], "25")
            self.window.action = None
    def test_count_menu_keeps_keyboard_editing_available(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        field = self.window.batch
        field.dropdown_action.trigger(); self.app.processEvents()
        self.assertTrue(field.choices.isVisible())
        next(action for action in field.choices.actions() if action.text() == "Вся очередь").trigger()
        self.assertEqual(self.window.batch_size(), 0)
        self.assertFalse(field.choices.isVisible())
        field.setFocus(); field.selectAll(); QTest.keyClick(field, Qt.Key.Key_Backspace)
        QTest.keyClicks(field, "42")
        self.assertEqual(self.window.batch_size(), 42)

    def test_activity_and_functions_are_first_in_card_and_remain_visible_when_missing(self):
        from benchmark.ui_models import company_documents
        self.complete_one()
        company = copy.deepcopy(self.window.snapshot.companies[0])
        html, _ = company_documents(company)
        self.assertLess(html.index("ВИДЫ ДЕЯТЕЛЬНОСТИ"), html.index("ФУНКЦИИ КОМПАНИИ"))
        self.assertNotIn("ПРИНАДЛЕЖНОСТЬ САЙТА", html)
        self.assertIn(company["card"]["facts"]["primary_activity"]["value"], html)
        self.assertIn(company["card"]["facts"]["functions_summary"]["value"], html)
        for key in ["primary_activity", "functions_summary"]:
            company["card"]["facts"][key] = {"value": None, "source_ids": [], "note": "Страница не открылась <проверка>"}
        company["input"]["activity"] = "Непроверенная деятельность из Excel"
        html, _ = company_documents(company)
        self.assertIn("ВИДЫ ДЕЯТЕЛЬНОСТИ", html)
        self.assertIn("ФУНКЦИИ КОМПАНИИ", html)
        self.assertEqual(html.count("Нет данных."), 2)
        self.assertNotIn("&lt;проверка&gt;", html)
        self.assertNotIn("<проверка>", html)
        self.assertNotIn(company["input"].get("activity", "input-activity-not-present"), html)

    def test_pending_company_shows_activity_and_functions_placeholders(self):
        from benchmark.ui_models import company_documents
        html, _ = company_documents(self.window.snapshot.companies[0])
        self.assertIn("ВИДЫ ДЕЯТЕЛЬНОСТИ", html)
        self.assertIn("ФУНКЦИИ КОМПАНИИ", html)
        self.assertEqual(html.count("Исследование ещё не выполнено."), 2)

    def test_invalid_count_does_not_start_processing_or_replace_saved_value(self):
        self.window.auth_ready = True
        self.window.batch.setText("37"); self.window.save_preferences()
        for text in ["", "abc", "-1", "100001"]:
            with self.subTest(text=text), patch.object(self.window.controller, "start") as start:
                self.window.batch.setText(text)
                self.window.start_research()
                start.assert_not_called()
                self.assertTrue(self.window.status.property("error"))
                self.window.save_preferences()
                self.assertEqual(self.settings.value("company_limit", type=int), 37)

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
        self.assertIn("Лимит подписки", self.window.login_button.toolTip())

    def test_authentication_failure_enables_real_login_again(self):
        self.complete_one()
        self.window.controller.env["BENCHMARK_TEST_MODE"] = "auth_error"
        self.window.start_research(); self.wait_idle()
        self.assertFalse(self.window.auth_ready)
        self.assertEqual(self.window.login_button.text(), "Войти через ChatGPT")
        self.assertEqual(self.window.snapshot.saved, 1)
        self.assertFalse(self.window.start_button.isEnabled())

    def test_saved_metric_filters_without_mislabeling_the_results(self):
        self.complete_one()
        self.window.metrics["Обработано"].click()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.assertEqual(self.window.table_title.text(), "Обработан")

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
        self.assertEqual(self.window.login_url, "")
        self.window.login_progress("Open https://auth.openai.com/oauth/authorize?state=test in your browser")
        self.assertEqual(self.window.login_button.text(), "Открыть страницу входа")
        self.assertTrue(self.window.login_button.isEnabled())
        with patch("benchmark.qt_gui.QDesktopServices.openUrl") as opener:
            self.window.login_button.click()
        opener.assert_called_once_with(QUrl("https://auth.openai.com/oauth/authorize?state=test"))
        self.window.finished({"action": "login", "code": 1, "output": "Вход отменён", "cancelled": False})
        self.assertEqual(self.window.login_url, "")
        self.assertEqual(self.window.login_url, "")
        self.assertIn("Вход не завершён", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
