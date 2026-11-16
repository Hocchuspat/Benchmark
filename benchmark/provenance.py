"""Require opened-page events, not just search hits, for accepted evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def url_key(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def opened_urls(events_path: Path):
    opened = set()
    if not events_path.is_file():
        return opened
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or item.get("type") != "web_search":
            continue
        action = item.get("action") or {}
        if action.get("type") in {"search", "search_query"}:
            continue
        for result in item.get("results", []):
            # Codex 0.166.0 emits view results with actual URL and 'Total lines'.
            # Unknown event formats are conservative: no invented proof of opening.
            if isinstance(result, dict) and result.get("url") and "Total lines:" in result.get("snippet", ""):
                opened.add(url_key(result["url"]))
    return opened


def enforce_opened_sources(card, events_path: Path):
    """Keep raw output on disk, remove unsupported claims from the accepted card."""
    card = copy.deepcopy(card)
    opened = opened_urls(events_path)
    # Shape validation runs before this function; malformed output is never repaired.
    unsupported = {s["source_id"] for s in card["sources"] if url_key(s["url"]) not in opened}
    removed_fields = []
    if unsupported:
        for key, fact in card["facts"].items():
            lost_refs = set(fact["source_ids"]) & unsupported
            fact["source_ids"] = [s for s in fact["source_ids"] if s not in unsupported]
            if fact["value"] is not None and not fact["source_ids"]:
                fact.update(value=None, note="Нет подтверждённого открытия страницы в журнале CLI; исходный ответ сохранён в attempts.")
                removed_fields.append(key)
            elif lost_refs:
                fact["note"] = "Приняты только источники с подтверждённым открытием; первоначальное пояснение сохранено в attempts."
        identity = card["identity"]
        identity["source_ids"] = [s for s in identity["source_ids"] if s not in unsupported]
        if identity["status"].startswith("confirmed") and not identity["source_ids"]:
            identity["status"] = "unconfirmed"
            identity["reason"] = "Принадлежность сайта не подтверждена открытыми страницами в журнале CLI. " + identity["reason"]
        if identity["status"] == "confirmed_by_inn" and card["facts"]["inn"]["value"] is None:
            identity["status"] = "unconfirmed"
            identity["reason"] = "Подтверждение ИНН исключено при проверке открытия источников. " + identity["reason"]
        card["sources"] = [s for s in card["sources"] if s["source_id"] not in unsupported]
        card["missing_information"] = [k for k, f in card["facts"].items() if f["value"] is None]
        card["warnings"].append("Исключены источники без подтверждённого открытия страницы: " + ", ".join(sorted(unsupported)))
        if card["research_status"] == "completed":
            card["research_status"] = "needs_review"
    return card, {"opened_page_urls": sorted(opened), "excluded_source_ids": sorted(unsupported),
                  "excluded_fact_fields": removed_fields}


def enforce_entity_scope(card, company):
    """A correctly opened page about another legal entity is still wrong evidence."""
    card = copy.deepcopy(card)
    removed = []
    status = card["identity"]["status"]
    official_ids = {s["source_id"] for s in card["sources"] if s["kind"] == "official"}
    for key, fact in card["facts"].items():
        if fact["value"] is None:
            continue
        if status in {"conflict", "unconfirmed"}:
            fact["source_ids"] = [sid for sid in fact["source_ids"] if sid not in official_ids]
        wrong_inn = key == "inn" and company.get("inn") and fact["value"] != company["inn"]
        if not fact["source_ids"] or wrong_inn:
            fact.update(value=None, source_ids=[], note="Сведения нельзя уверенно отнести к исходному юридическому лицу; требуется проверка принадлежности сайта.")
            removed.append(key)
    if removed:
        card["missing_information"] = [k for k, f in card["facts"].items() if f["value"] is None]
        card["warnings"].append("Исключены сведения без подтверждённой принадлежности юридическому лицу: " + ", ".join(removed))
        if card["research_status"] == "completed":
            card["research_status"] = "needs_review"
    return card, {"excluded_fact_fields": removed}
