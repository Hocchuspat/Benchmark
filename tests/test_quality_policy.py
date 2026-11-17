from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from benchmark.identity import address_key, details_match, phone_numbers
from benchmark.validation import FACT_FIELDS, matched_identity_details, validate_card
from benchmark.storage import Store


def sample():
    company = {"company_id": "test", "name": "Тест, ООО", "inn": "0123456789"}
    facts = {k: {"value": None, "source_ids": [], "note": "Не найдено"} for k in FACT_FIELDS}
    for key, value in {"inn": company["inn"], "primary_activity": "Монтаж систем", "products_services": "Монтаж",
            "functional_profile": "строительство", "product_service_type": "Услуги",
            "functions_summary": "Компания монтирует системы. Компания обслуживает оборудование."}.items():
        facts[key] = {"value": value, "source_ids": ["s1"], "note": "Тестовый источник"}
    card = {"company_id": "test", "research_status": "completed", "official_site": "https://example.test",
            "identity": {"status": "confirmed_by_inn", "reason": "Совпадает ИНН", "source_ids": ["s1"]},
            "facts": facts, "sources": [{"source_id": "s1", "url": "https://example.test/contacts", "kind": "official",
                "title": "Контакты", "accessed_at": date.today().isoformat(), "excerpt": "Тестовые сведения"}],
            "missing_information": [k for k, f in facts.items() if f["value"] is None], "warnings": []}
    return company, card


class DetailComparisonTests(unittest.TestCase):
    def test_multiple_phones_are_compared_separately(self):
        phones = "+7 (3812) 49-55-09; +7 (913) 123-45-67"
        self.assertEqual(phone_numbers(phones), {"73812495509", "79131234567"})
        self.assertTrue(details_match(phones, "9131234567", "phone"))
        self.assertTrue(details_match(phones, "8 (913) 123-45-67", "phone"))
        self.assertFalse(details_match(phones, "9131234568", "phone"))

    def test_extensions_and_delimiters_do_not_join_numbers(self):
        for separator in [";", ",", "\n", "/", " или "]:
            with self.subTest(separator=separator):
                self.assertTrue(details_match("+7 (3812) 49-55-09" + separator + "+7 913 1234567 доб. 55", "9131234567", "phone"))
        self.assertFalse(details_match("7381249550979131234567", "9131234567", "phone"))
        self.assertFalse(details_match("12345", "12345", "phone"))

    def test_address_abbreviations_and_duplicate_city(self):
        supplied = "Омская обл., г.о. город Омск, г. Омск, ул. 5-я Северная, д. 163"
        found = "Омская область, г. Омск, ул. 5-я Северная, д. 163"
        self.assertTrue(details_match(found, supplied, "legal_address"))
        self.assertIn("город омск", address_key(found))

    def test_house_building_and_unit_differences_are_preserved(self):
        base = "Омская обл., г. Омск, ул. 5-я Северная, д. 163"
        pairs = [(base, base.replace("163", "163/1")), (base, base.replace("163", "1630")),
                 (base + ", корп. 1", base + ", стр. 1"), (base + ", оф. 502", base + ", пом. 58Н"),
                 (base, base.replace("5-я", "6-я")), (base, base.replace("г. Омск", "г. Томск"))]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertFalse(details_match(a, b, "legal_address"))

    def test_two_details_with_multiple_phones_and_address_aliases_confirm_site(self):
        company, card = sample()
        company.update(phone="9131234567", address="Омская обл., г.о. город Омск, г. Омск, ул. 5-я Северная, д. 163")
        card["identity"]["status"] = "confirmed_by_details"
        for key, value in {"phone": "+7 (3812) 49-55-09; +7 (913) 123-45-67",
                           "legal_address": "Омская область, г. Омск, ул. 5-я Северная, д. 163"}.items():
            card["facts"][key] = {"value": value, "source_ids": ["s1"], "note": "На сайте"}
            card["missing_information"].remove(key)
        self.assertEqual(matched_identity_details(card, company), ["legal_address", "phone"])
        self.assertFalse(validate_card(card, company)["needs_review"])


class WarningPolicyTests(unittest.TestCase):
    def test_unused_failed_source_is_information_for_confirmed_site(self):
        company, card = sample()
        card["warnings"] = ["Страница Т-Банка не открылась из-за тайм-аута; сведения её поискового сниппета не использованы."]
        original = copy.deepcopy(card)
        quality = validate_card(card, company)
        self.assertTrue(quality["site_confirmed"])
        self.assertFalse(quality["needs_review"])
        self.assertEqual(quality["info_notes"], card["warnings"])
        self.assertEqual(quality["warnings"], card["warnings"])
        self.assertEqual(card, original)

    def test_different_contact_and_legal_address_explanation_is_information(self):
        company, card = sample()
        card["warnings"] = ["Контактный адрес сайта отличается от юридического адреса; назначение адресов различается, конфликт реквизитов не установлен."]
        self.assertFalse(validate_card(card, company)["needs_review"])

    def test_group_scope_conflicts_unknown_and_unverified_claims_require_review(self):
        company, card = sample()
        warnings = ["Адрес расходится между источниками.", "Сведения относятся к группе компаний, принадлежность ООО не установлена.",
                    "Нужна дополнительная ручная проверка.", "Реквизиты не подтверждены вторичным источником; государственный реестр не проверен.",
                    "Государственный реестр не проверен; есть противоречия в реквизитах."]
        for warning in warnings:
            with self.subTest(warning=warning):
                card["warnings"] = [warning]
                quality = validate_card(card, company)
                self.assertTrue(quality["needs_review"])
                self.assertEqual(quality["info_notes"], [])

    def test_confirmed_site_can_still_have_blocking_card_issues(self):
        company, card = sample()
        card["warnings"] = ["Тождество помещений не установлено."]
        quality = validate_card(card, company)
        self.assertTrue(quality["site_confirmed"])
        self.assertTrue(quality["needs_review"])

    def test_unconfirmed_site_never_becomes_completed_with_empty_warnings(self):
        company, card = sample()
        card["identity"]["status"] = "unconfirmed"; card["research_status"] = "needs_review"
        quality = validate_card(card, company)
        self.assertFalse(quality["site_confirmed"])
        self.assertTrue(quality["needs_review"])

    def test_unverified_input_email_is_not_information_when_used_as_a_fact(self):
        company, card = sample(); company["email"] = "tender@example.test"
        card["warnings"] = ["Адрес tender@example.test из входной карточки не подтверждён открытыми страницами."]
        self.assertFalse(validate_card(card, company)["needs_review"])
        card["facts"]["email"] = {"value": company["email"], "source_ids": ["s1"], "note": "Тест"}
        card["missing_information"].remove("email")
        self.assertTrue(validate_card(card, company)["needs_review"])

    def test_bad_summary_length_remains_a_review_reason(self):
        company, card = sample(); card["facts"]["functions_summary"]["value"] = "Одно предложение."
        self.assertTrue(validate_card(card, company)["needs_review"])

    def test_recovered_attempt_keeps_original_finish_time_and_usage(self):
        company, card = sample()
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)); store.initialize([company], {})
            try:
                number = store.start("test")
                usage = {"input_tokens": 100, "output_tokens": 10}
                store.finish("test", number, "failed", error="Old validation error", usage=usage)
                before = store.db.execute("SELECT finished_at FROM attempts").fetchone()[0]
                with patch("benchmark.storage.now", return_value="2099-01-01T00:00:00+00:00"):
                    store.finish("test", number, "completed", card=card, quality=validate_card(card, company), usage=usage)
                after = store.db.execute("SELECT finished_at,usage_json FROM attempts").fetchone()
                self.assertEqual(after[0], before)
                self.assertEqual(json.loads(after[1]), usage)
                self.assertIsNone(store.rows()[0]["error"])
            finally: store.close()
