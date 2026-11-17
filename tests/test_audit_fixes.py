"""Regressions from the specification review. Only offline Codex fixtures."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from benchmark.cli import main
from benchmark.excel import read_companies
from benchmark.report import export_reports
from benchmark.runner import prepare_card, run_queue
from benchmark.storage import Store
from benchmark.validation import CardError, FACT_FIELDS, validate_card

FAKE = Path(__file__).parent.resolve() / "fake_codex.py"


class ExportProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "research"
        self.source = self.run / "reports" / "report.xlsx"
        self.source.parent.mkdir(parents=True)
        book = Workbook(); book.active.append(["Название компании", "ИНН", "Сайт"])
        book.active.append(["Тест", "0123456789", "example.test"]); book.save(self.source); book.close()
        self.original = self.source.read_bytes()

    def test_import_rejects_source_at_generated_report_path(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["init", str(self.source), "--run-dir", str(self.run)]), 1)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.run / "state.sqlite3").exists())

    def legacy_queue(self):
        store = Store(self.run)
        store.initialize(read_companies(self.source), {"path": str(self.source)})
        return store

    def test_legacy_queue_export_rejects_collision_before_any_output_is_written(self):
        store = self.legacy_queue(); self.addCleanup(store.close)
        with self.assertRaisesRegex(ValueError, "совпадает"):
            export_reports(store)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.run / "reports" / "report.csv").exists())

    def test_collision_is_checked_before_research_or_authentication(self):
        self.legacy_queue().close()
        with patch("benchmark.codex.check_login", side_effect=AssertionError("No account calls")):
            with self.assertRaisesRegex(ValueError, "совпадает"):
                run_queue(self.run)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_alias_to_source_is_also_protected(self):
        from benchmark.report import check_export_paths
        original = self.run.parent / "original.xlsx"
        original.write_bytes(self.original)
        self.source.unlink()
        try:
            self.source.symlink_to(original)
        except OSError:
            self.skipTest("Creating symlinks is unavailable")
        with self.assertRaisesRegex(ValueError, "совпадает"):
            check_export_paths(self.run, original)
        self.assertEqual(original.read_bytes(), self.original)


class IdentityDetailsTests(unittest.TestCase):
    def setUp(self):
        self.company = {"company_id": "test", "name": "Тест, ООО", "phone": "8 (495) 123-45-67"}
        facts = {k: {"value": None, "source_ids": [], "note": "Нет сведений"} for k in FACT_FIELDS}
        facts["legal_name"] = {"value": "ООО «Тест»", "source_ids": ["s1"], "note": "Название на сайте"}
        facts["phone"] = {"value": "+7 495 1234567", "source_ids": ["s1"], "note": "Телефон на сайте"}
        self.card = {"company_id": "test", "research_status": "needs_review", "official_site": "https://example.test",
                     "identity": {"status": "confirmed_by_details", "reason": "Совпали название и телефон", "source_ids": ["s1"]},
                     "facts": facts, "sources": [{"source_id": "s1", "url": "https://example.test/contacts", "kind": "official",
                        "title": "Контакты", "accessed_at": date.today().isoformat(), "excerpt": "Тестовые реквизиты"}],
                     "missing_information": [k for k, f in facts.items() if f["value"] is None], "warnings": []}

    def test_two_distinct_matching_official_facts_are_accepted(self):
        self.assertTrue(validate_card(self.card, self.company)["structural_checks_passed"])

    def test_one_matching_fact_cannot_confirm_site(self):
        self.card["facts"]["phone"] = {"value": None, "source_ids": [], "note": "Не найден"}
        self.card["missing_information"].append("phone")
        with self.assertRaisesRegex(CardError, "два разных"):
            validate_card(self.card, self.company)

    def test_nonmatching_or_wrong_domain_or_secondary_evidence_is_rejected(self):
        for variant in ["wrong_phone", "wrong_domain", "secondary"]:
            card = copy.deepcopy(self.card)
            if variant == "wrong_phone": card["facts"]["phone"]["value"] = "+7 495 1112233"
            elif variant == "wrong_domain": card["sources"][0]["url"] = "https://other.test/contacts"
            else: card["sources"][0]["kind"] = "secondary"
            with self.subTest(variant=variant), self.assertRaisesRegex(CardError, "два разных"):
                validate_card(card, self.company)

    def test_lost_second_source_downgrades_identity_without_losing_raw_response(self):
        source = {**self.card["sources"][0], "source_id": "s2", "url": "https://example.test/phone"}
        self.card["sources"].append(source); self.card["identity"]["source_ids"].append("s2")
        self.card["facts"]["phone"]["source_ids"] = ["s2"]
        original = copy.deepcopy(self.card)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            event = {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "open_page"},
                     "results": [{"url": "https://example.test/contacts", "snippet": "Total lines: 10"}]}}
            (path / "events.jsonl").write_text(json.dumps(event), encoding="utf-8")
            checked, quality = prepare_card({"card": self.card, "web_calls": 1}, self.company, path)
        self.assertEqual(checked["identity"]["status"], "unconfirmed")
        self.assertTrue(quality["needs_review"])
        self.assertEqual(self.card, original)


class SelectedRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.run = root / "research"; self.source = root / "companies.xlsx"
        book = Workbook(); book.active.append(["Название компании", "ИНН", "Сайт"])
        book.active.append(["Первая", "0123456789", "example.test"])
        book.active.append(["Вторая", "9876543210", "example.test"]); book.save(self.source); book.close()
        store = Store(self.run); store.initialize(read_companies(self.source), {"path": str(self.source)})
        self.ids = [row["id"] for row in store.rows()]; store.close()
        env = patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "good"}); env.start(); self.addCleanup(env.stop)

    def run_target(self, index=1):
        return run_queue(self.run, binary=str(FAKE), company_id=self.ids[index], emit=lambda _: None)

    def test_targeted_retry_never_processes_other_pending_companies(self):
        self.assertEqual(self.run_target(), 0)
        self.assertEqual(self.run_target(), 0)
        store = Store(self.run); self.addCleanup(store.close)
        self.assertEqual([(r["state"], r["attempts"]) for r in store.rows()], [("pending", 0), ("completed", 2)])
        self.assertEqual(store.meta("last_stop")["reason"], "selected_company")
        self.assertEqual(store.db.execute("select count(*) from attempts where outcome='completed'").fetchone()[0], 2)

    def test_failed_retry_and_subscription_limit_keep_previous_card_and_files(self):
        self.run_target()
        store = Store(self.run); before = store.rows()[1]["card_json"]; store.close()
        raw = self.run / "attempts" / self.ids[1] / "001" / "card.json"; raw_before = raw.read_bytes()
        for mode, code, state in [("invalid", 1, "failed"), ("limit", 75, "pending")]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"BENCHMARK_TEST_MODE": mode}):
                self.assertEqual(self.run_target(), code)
                store = Store(self.run)
                try:
                    rows = store.rows()
                    self.assertEqual(rows[1]["state"], state)
                    self.assertEqual(rows[1]["card_json"], before)
                    self.assertEqual(rows[0]["attempts"], 0)
                    self.assertEqual(raw.read_bytes(), raw_before)
                finally: store.close()

    def test_reviewed_card_can_be_explicitly_researched_again(self):
        self.run_target()
        store = Store(self.run); row = store.rows()[1]; card = json.loads(row["card_json"])
        card["warnings"].append("Нужна дополнительная проверка")
        store.update_card(self.ids[1], card, {"needs_review": True, "warnings": card["warnings"]}); store.close()
        self.assertEqual(self.run_target(), 0)
        store = Store(self.run); self.addCleanup(store.close)
        self.assertEqual(store.rows()[1]["attempts"], 2)
        self.assertEqual(store.rows()[1]["state"], "completed")
        self.assertEqual(store.rows()[0]["state"], "pending")

    def test_unknown_target_never_starts_authentication(self):
        with patch("benchmark.codex.check_login", side_effect=AssertionError("No account calls")):
            with self.assertRaisesRegex(ValueError, "не найдена"):
                run_queue(self.run, company_id="missing")
