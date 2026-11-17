"""Structural and cross-field gates; does not claim independent fact verification."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .excel import normalize_url
from .identity import detail_key, details_match
from .quality import RESEARCH_REVIEW_REASONS, classify_warnings

SCHEMA_PATH = Path(__file__).parent / "resources" / "company.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
FACT_FIELDS = tuple(SCHEMA["properties"]["facts"]["properties"])
PROFILES = {"производство", "дистрибуция", "сервис", "НИОКР", "управление", "логистика", "строительство", "проектирование", "прочее"}


class CardError(ValueError):
    pass


def host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def matched_identity_details(card, company):
    """Count distinct matching facts supported on the candidate's own domain."""
    site = card.get("official_site")
    if not site:
        return []
    identity = card["identity"]
    proof_ids = {s["source_id"] for s in card["sources"] if s["kind"] == "official"
                 and host(s["url"]) == host(site)} & set(identity["source_ids"])
    inputs = {"legal_name": [company.get("legal_name"), company.get("name")],
              "legal_address": [company.get("address")], "phone": [company.get("phone")],
              "ogrn": [company.get("ogrn")]}
    matched = []
    for field, candidates in inputs.items():
        fact = card["facts"][field]
        found = detail_key(fact["value"], field)
        if found and set(fact["source_ids"]) & proof_ids and any(
                candidate and details_match(fact["value"], candidate, field) for candidate in candidates):
            matched.append(field)
    return matched


def validate_structure(card, company):
    errors = list(Draft202012Validator(SCHEMA).iter_errors(card))
    if errors:
        e = errors[0]
        raise CardError(f"JSON {'.'.join(map(str, e.absolute_path))}: {e.message}")
    if card["company_id"] != company["company_id"]:
        raise CardError("company_id не совпадает с заданием")


def validate_card(card, company):
    validate_structure(card, company)
    sources = {}
    for s in card["sources"]:
        sid = s["source_id"]
        if not sid.strip() or sid in sources:
            raise CardError("Пустой или повторный source_id")
        if not normalize_url(s["url"]) or not s["url"].startswith(("https://", "http://")):
            raise CardError(f"Некорректный URL источника {sid}")
        try:
            day = date.fromisoformat(s["accessed_at"])
        except ValueError as exc:
            raise CardError(f"Некорректная дата источника {sid}") from exc
        if day > date.today():
            raise CardError(f"Дата источника {sid} находится в будущем")
        if not s["excerpt"].strip() or not s["title"].strip():
            raise CardError(f"Нет подтверждающего фрагмента/названия {sid}")
        sources[sid] = s
    identity = card["identity"]
    if not identity["reason"].strip():
        raise CardError("Не объяснена принадлежность сайта")
    if set(identity["source_ids"]) - sources.keys():
        raise CardError("Неизвестный источник принадлежности сайта")
    site = card["official_site"]
    if site is not None and (not normalize_url(site) or not site.startswith(("https://", "http://"))):
        raise CardError("Некорректный official_site")
    missing = []
    for key, fact in card["facts"].items():
        if set(fact["source_ids"]) - sources.keys():
            raise CardError(f"Неизвестный источник у {key}")
        if fact["value"] is None:
            missing.append(key)
            if fact["source_ids"] or not fact["note"].strip():
                raise CardError(f"Для отсутствующего {key} нужны пустые source_ids и причина")
        elif not fact["value"].strip() or not fact["source_ids"]:
            raise CardError(f"Факт {key} пуст или не подтверждён источниками")
    if set(missing) != set(card["missing_information"]):
        raise CardError("missing_information не совпадает с null-полями facts")
    for key, lengths in {"inn": {10, 12}, "ogrn": {13, 15}, "kpp": {9}}.items():
        value = card["facts"][key]["value"]
        if value is not None and (not value.isdigit() or len(value) not in lengths):
            raise CardError(f"Некорректный формат {key}")
    found_inn = card["facts"]["inn"]["value"]
    if company.get("inn") and found_inn and company["inn"] != found_inn and identity["status"] != "conflict":
        raise CardError("Конфликт ИНН не отмечен как conflict")
    if identity["status"].startswith("confirmed"):
        if not site or not identity["source_ids"]:
            raise CardError("Подтверждённый сайт требует URL и источники идентификации")
    if identity["status"] == "confirmed_by_inn":
        if not company.get("inn") or found_inn != company["inn"]:
            raise CardError("confirmed_by_inn требует совпавшего ИНН")
        proof = set(card["facts"]["inn"]["source_ids"]) & set(identity["source_ids"])
        if not any(sources[s]["kind"] == "official" and host(sources[s]["url"]) == host(site) for s in proof):
            raise CardError("ИНН не подтверждён на официальном домене")
    if identity["status"] == "confirmed_by_details" and len(matched_identity_details(card, company)) < 2:
        raise CardError("confirmed_by_details требует минимум два разных совпавших реквизита с подтверждением на официальном домене")
    if card["research_status"] == "completed":
        if not identity["status"].startswith("confirmed") or not site:
            raise CardError("completed требует подтверждённого сайта")
        for key in ["primary_activity", "products_services", "functional_profile", "functions_summary"]:
            if card["facts"][key]["value"] is None:
                raise CardError(f"completed требует {key}")
        if not any(s["kind"] == "official" and host(s["url"]) == host(site) for s in sources.values()):
            raise CardError("completed требует страницу официального сайта")
    value = card["facts"]["product_service_type"]["value"]
    if value is not None and value not in {"Продукция", "Услуги", "Продукция и услуги"}:
        raise CardError("Некорректный product_service_type")
    value = card["facts"]["functional_profile"]["value"]
    if value is not None and not set(v.strip() for v in value.split(",")) <= PROFILES:
        raise CardError("Неизвестный функциональный профиль")
    warnings = list(card["warnings"])
    summary = card["facts"]["functions_summary"]["value"]
    if summary:
        sentences = re.split(r"(?<=[.!?])\s+(?=[А-ЯA-ZЁ])", summary.strip())
        if not 2 <= len(sentences) <= 5:
            warnings.append("Описание функций требует проверки длины: ожидается 2–5 предложений")
    review_reasons, info_notes = classify_warnings(warnings, card, company)
    site_confirmed = identity["status"].startswith("confirmed")
    if not site_confirmed:
        review_reasons.insert(0, "Принадлежность сайта не подтверждена." if identity["status"] != "conflict" else "Обнаружен конфликт принадлежности сайта.")
    if card["research_status"] != "completed":
        review_reasons.append(RESEARCH_REVIEW_REASONS[card["research_status"]])
    needs_review = bool(review_reasons)
    return {"structural_checks_passed": True, "independent_fact_check": False,
            "needs_review": needs_review, "warnings": warnings,
            "site_confirmed": site_confirmed, "review_reasons": review_reasons, "info_notes": info_notes,
            "populated_facts": len(FACT_FIELDS) - len(missing), "source_count": len(sources)}
