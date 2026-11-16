from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from benchmark.cli import main
from benchmark.codex import execute, check_login, parse_events
from benchmark.excel import read_companies
from benchmark.report import report_tables
from benchmark.runner import run_queue
from benchmark.storage import Store, run_lock
from benchmark.validation import CardError, validate_card
from benchmark.provenance import enforce_opened_sources, enforce_entity_scope

FAKE = Path(__file__).parent.resolve() / "fake_codex.py"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        self.input = self.root / "input.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Компании"
        ws.append(["Название компании", "ИНН", "Электронная страница", "Уточненный адрес электронной страницы", "код ОКВЭД КДЕС Ред. 2"])
        ws.append(["Компания А", "0123456789", "old.example.test", "new.example.test/contacts", "43.21"])
        ws.append(["Компания А", "9876543210", None, None, "43.22"])
        ws.append([None] * 5)
        wb.save(self.input)
        wb.close()
        FAKE.chmod(0o755)
        self.env = patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "good", "OPENAI_API_KEY": "must-not-be-forwarded"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def initialize(self):
        store = Store(self.run)
        store.initialize(read_companies(self.input), {"test": True})
        store.close()

    def run_queue(self, **kwargs):
        return run_queue(self.run, binary=str(FAKE), emit=lambda _: None, **kwargs)

    def card(self):
        company = read_companies(self.input)[0]
        result = execute(company, self.root / "attempt", str(FAKE), timeout=5)
        return company, result["card"]

    def test_import_links_ids_and_input_preserved(self):
        before = self.input.read_bytes()
        records = read_companies(self.input)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["inn"], "0123456789")
        self.assertEqual(records[0]["website"], ["https://old.example.test"])
        self.assertEqual(records[0]["refined_website"], ["https://new.example.test/contacts"])
        self.assertNotEqual(records[0]["company_id"], records[1]["company_id"])
        self.assertEqual(records, read_companies(self.input))
        self.assertEqual(before, self.input.read_bytes())

    def test_hyperlink_and_formula_url(self):
        wb = load_workbook(self.input)
        ws = wb.active
        ws["C2"].value = "Официальный сайт"
        ws["C2"].hyperlink = "https://hyper.example.test"
        ws["D2"] = '=HYPERLINK("https://formula.example.test";"сайт")'
        wb.save(self.input)
        wb.close()
        company = read_companies(self.input)[0]
        self.assertEqual(company["website"], ["https://hyper.example.test"])
        self.assertEqual(company["refined_website"], ["https://formula.example.test"])

    def test_ambiguous_sheet_requires_explicit_selection(self):
        wb = load_workbook(self.input)
        wb.copy_worksheet(wb.active)
        wb.save(self.input)
        wb.close()
        with self.assertRaisesRegex(ValueError, "Несколько"):
            read_companies(self.input)
        self.assertEqual(len(read_companies(self.input, "Компании")), 2)

    def test_valid_card_and_cross_field_rejections(self):
        company, card = self.card()
        self.assertFalse(validate_card(card, company)["needs_review"])
        for change in [
            lambda c: c.update(company_id="wrong"),
            lambda c: c["facts"]["inn"].update(source_ids=["missing"]),
            lambda c: c["facts"]["inn"].update(value="1111111111"),
            lambda c: c.update(missing_information=[]),
            lambda c: c["sources"][0].update(accessed_at="2099-01-01"),
            lambda c: c["sources"][0].update(kind="secondary"),
        ]:
            bad = copy.deepcopy(card)
            change(bad)
            with self.assertRaises(CardError):
                validate_card(bad, company)

    def test_success_resume_and_atomic_export(self):
        self.initialize()
        self.assertEqual(self.run_queue(max_companies=1), 0)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 1, "pending": 1})
        store.close()
        self.assertEqual(self.run_queue(), 0)
        self.assertEqual(self.run_queue(), 0)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 2})
        self.assertEqual([r["attempts"] for r in store.rows()], [1, 1])
        self.assertEqual(store.summary()["usage"]["input_tokens"], 200)
        store.close()
        wb = load_workbook(self.run / "reports" / "report.xlsx")
        self.assertEqual(wb.sheetnames, ["Досье компаний", "Реквизиты и контроль", "Источники"])
        self.assertEqual(wb.active["B2"].value, "0123456789")
        self.assertEqual(wb.active["B2"].data_type, "s")
        wb.close()

    def test_limit_keeps_progress_and_can_continue(self):
        self.initialize()
        self.run_queue(max_companies=1)
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "limit"}):
            self.assertEqual(self.run_queue(), 75)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 1, "pending": 1})
        self.assertEqual(store.meta("last_stop")["reason"], "subscription_limit")
        store.close()
        self.assertEqual(self.run_queue(), 0)
        store = Store(self.run)
        self.assertEqual([r["attempts"] for r in store.rows()], [1, 2])
        store.close()

    def test_bad_output_failed_then_retry_only_failed(self):
        self.initialize()
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "invalid"}):
            self.run_queue(max_companies=1)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"failed": 1, "pending": 1})
        self.assertIsNone(store.rows()[0]["card_json"])
        store.close()
        self.run_queue(retry_failed=True)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 2})
        store.close()

    def test_api_auth_is_refused_before_spending(self):
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "api_auth"}):
            with self.assertRaisesRegex(ValueError, "ChatGPT"):
                check_login(str(FAKE))

    def test_timeout_leaves_retryable_error(self):
        self.initialize()
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "timeout"}):
            self.run_queue(max_companies=1, timeout=0.1)
        store = Store(self.run)
        self.assertEqual(store.rows()[0]["state"], "failed")
        self.assertIn("Превышено", store.rows()[0]["error"])
        store.close()

    def test_interrupt_returns_current_to_queue(self):
        self.initialize()
        with patch("benchmark.codex.execute", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_queue(), 130)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"pending": 2})
        store.close()
        self.assertTrue((self.run / "reports" / "report.xlsx").exists())

    def test_crash_recovery(self):
        self.initialize()
        store = Store(self.run)
        store.start(store.rows()[0]["id"])
        store.close()
        self.run_queue(max_companies=1)
        store = Store(self.run)
        self.assertEqual(store.rows()[0]["state"], "completed")
        self.assertEqual(store.rows()[0]["attempts"], 2)
        self.assertEqual(store.db.execute("SELECT outcome FROM attempts ORDER BY number LIMIT 1").fetchone()[0], "interrupted")
        store.close()

    def test_completed_file_recovered_without_spending(self):
        self.initialize()
        store = Store(self.run)
        row = store.rows()[0]
        attempt = store.start(row["id"])
        folder = self.run / "attempts" / row["id"] / f"{attempt:03d}"
        execute(json.loads(row["input_json"]), folder, str(FAKE))
        store.close()
        # Parent dies after receiving JSON, before committing it.
        self.run_queue(max_companies=1)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 2})
        self.assertEqual([r["attempts"] for r in store.rows()], [1, 1])
        self.assertEqual(store.summary()["usage"]["input_tokens"], 200)
        store.close()

    def test_lock_and_duplicate_init(self):
        self.initialize()
        with run_lock(self.run):
            with self.assertRaisesRegex(ValueError, "другой"):
                with run_lock(self.run):
                    pass
        store = Store(self.run)
        with self.assertRaisesRegex(ValueError, "уже создана"):
            store.initialize(read_companies(self.input), {})
        store.close()

    def test_formula_injection_and_separate_source_okved(self):
        self.initialize()
        store = Store(self.run)
        headers, rows, _, _ = report_tables(store)
        self.assertEqual(rows[0][headers.index("Оквэд")], "")
        self.assertEqual(rows[0][headers.index("ОКВЭД (входной)")], "43.21")
        store.close()
        from benchmark.report import safe
        self.assertEqual(safe('=HYPERLINK("https://x.test")'), '\'=HYPERLINK("https://x.test")')

    def test_token_budget_stops_between_companies(self):
        self.initialize()
        self.run_queue(token_budget=150)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 1, "pending": 1})
        self.assertEqual(store.meta("last_stop")["reason"], "token_budget")
        store.close()

    def test_search_snippet_facts_excluded_but_raw_retained(self):
        company, card = self.card()
        raw_copy = copy.deepcopy(card)
        events = self.root / "search-only.jsonl"
        events.write_text(json.dumps({"type": "item.completed", "item": {"type": "web_search", "action": {"type": "search"},
                                     "results": [{"url": "https://example.test/contacts", "snippet": "INN 0123456789"}]}}))
        checked, provenance = enforce_opened_sources(card, events)
        self.assertEqual(card, raw_copy)
        self.assertIsNone(checked["facts"]["inn"]["value"])
        self.assertEqual(checked["sources"], [])
        self.assertEqual(checked["identity"]["status"], "unconfirmed")
        self.assertEqual(provenance["excluded_source_ids"], ["s1"])
        self.assertTrue(validate_card(checked, company)["needs_review"])

    def test_recheck_uses_saved_response_without_codex(self):
        self.initialize()
        self.run_queue(max_companies=1)
        with patch("benchmark.codex.execute", side_effect=AssertionError("No paid calls")):
            self.assertEqual(main(["recheck", "--run-dir", str(self.run)]), 0)
        store = Store(self.run)
        self.assertEqual(store.rows()[0]["state"], "completed")
        self.assertEqual(store.rows()[0]["attempts"], 1)
        store.close()

    def test_derived_missing_fields_can_be_fixed_without_new_research(self):
        self.initialize()
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "wrong_missing"}):
            self.run_queue(max_companies=1)
        store = Store(self.run)
        row = store.rows()[0]
        self.assertEqual(row["state"], "needs_review")
        card = json.loads(row["card_json"])
        self.assertEqual(set(card["missing_information"]), {k for k, f in card["facts"].items() if f["value"] is None})
        self.assertEqual(row["attempts"], 1)
        store.close()

    def test_wrong_company_activity_not_transferred(self):
        company, card = self.card()
        card["identity"]["status"] = "conflict"
        card["research_status"] = "needs_review"
        card["facts"]["inn"]["value"] = "1111111111"
        validate_card(card, company)
        checked, scope = enforce_entity_scope(card, company)
        self.assertIsNone(checked["facts"]["functions_summary"]["value"])
        self.assertIsNone(checked["facts"]["inn"]["value"])
        self.assertIn("primary_activity", scope["excluded_fact_fields"])
        self.assertEqual(checked["identity"]["status"], "conflict")
        self.assertTrue(validate_card(checked, company)["needs_review"])

    def test_authentication_failure_pauses_current_company(self):
        self.initialize()
        with patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "auth_error"}):
            self.assertEqual(self.run_queue(), 75)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"pending": 2})
        self.assertEqual(store.meta("last_stop")["reason"], "authentication")
        store.close()

    def test_stop_request_finishes_current_company(self):
        self.initialize()
        original_execute = execute
        def execute_and_stop(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            (self.run / "stop.request").write_text("stop")
            return result
        with patch("benchmark.codex.execute", side_effect=execute_and_stop):
            self.run_queue(max_companies=0)
        store = Store(self.run)
        self.assertEqual(store.summary()["states"], {"completed": 1, "pending": 1})
        self.assertEqual(store.meta("last_stop")["reason"], "user_stop")
        store.close()


if __name__ == "__main__":
    unittest.main()
