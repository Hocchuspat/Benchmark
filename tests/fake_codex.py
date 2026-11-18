#!/usr/bin/env python3
"""Offline Codex double. Never contacts a service or reads auth files."""
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("BENCHMARK_TEST_MODE", "good")
if args == ["login", "status"]:
    print("Logged in using API key" if mode == "api_auth" else "Logged in using ChatGPT")
    sys.exit(0)
if args == ["logout"]:
    print("Logged out")
    sys.exit(1 if mode == "logout_error" else 0)
if args == ["--version"]:
    print("fake-codex-test")
    sys.exit(0)
if "--help" in args:
    print("--output-schema --json --ephemeral --ignore-user-config")
    sys.exit(0)
assert "--search" in args and "read-only" in args and "--ignore-user-config" in args
assert 'forced_login_method="chatgpt"' in args
assert not os.environ.get("OPENAI_API_KEY")
if mode == "limit":
    print(json.dumps({"type": "turn.failed", "error": {"message": "You have hit your usage limit. Try again later."}}))
    sys.exit(1)
if mode == "auth_error":
    print(json.dumps({"type": "error", "message": "401 authentication expired"}))
    sys.exit(1)
if mode == "timeout":
    time.sleep(15)
if mode == "slow":
    time.sleep(1)
prompt = sys.stdin.read()
company = json.loads(prompt.rsplit("<company_input_json>", 1)[1].split("</company_input_json>", 1)[0])
schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
facts = {k: {"value": None, "source_ids": [], "note": "Не найдено"} for k in schema["properties"]["facts"]["properties"]}
values = {"inn": company.get("inn", "7709905490"), "primary_activity": "Монтаж инженерных систем.",
          "products_services": "Монтаж и обслуживание", "functional_profile": "строительство, сервис",
          "product_service_type": "Услуги", "functions_summary": "Компания выполняет монтаж инженерных систем. Она также обслуживает установленное оборудование."}
for key, value in values.items():
    facts[key] = {"value": value, "source_ids": ["s1"], "note": "Подтверждено на странице"}
card = {"company_id": company["company_id"], "research_status": "completed", "official_site": "https://example.test",
        "identity": {"status": "confirmed_by_inn", "reason": "На странице совпадает ИНН", "source_ids": ["s1"]},
        "facts": facts,
        "sources": [{"source_id": "s1", "url": "https://example.test/contacts", "title": "Контакты", "accessed_at": date.today().isoformat(),
                     "kind": "official", "excerpt": "Тестовое подтверждение реквизитов и услуг"}],
        "missing_information": [k for k, f in facts.items() if f["value"] is None], "warnings": [], "report_issues": []}
if mode == "local_only":
    local = json.loads(prompt.rsplit("<local_web_evidence_json>", 1)[1].split("</local_web_evidence_json>", 1)[0])
    assert local["pages"] and company["inn"] in local["pages"][0]["text"]
    card["official_site"] = local["pages"][0]["url"]
    card["sources"][0]["url"] = local["pages"][0]["url"]
if mode == "secondary_only":
    card.update(official_site=None, research_status="not_found")
    card["identity"] = {"status": "unconfirmed", "reason": "Официальный сайт не найден", "source_ids": ["s1"]}
    card["sources"][0]["kind"] = "secondary"
    card["warnings"] = ["Официальный сайт не найден, сведения собраны по ИНН из открытого справочника."]
if mode == "missing_activity":
    card["facts"]["primary_activity"] = {"value": None, "source_ids": [], "note": "Описание деятельности не найдено"}
    card["missing_information"].append("primary_activity")
output = Path(args[args.index("--output-last-message") + 1])
if mode == "wrong_missing":
    card["missing_information"] = []
output.write_text("invalid json" if mode == "invalid" else json.dumps(card, ensure_ascii=False), encoding="utf-8")
action = {"type": "open_page", "url": "https://example.test/contacts"}
results = [{"url": "https://example.test/contacts", "snippet": "Total lines: 100"}]
if mode == "mixed_search_open":
    action = {"type": "search", "query": company.get("inn", "")}
    results = [{"url": "https://example.test/contacts", "ref_id": "turn0search0", "snippet": "Результат поиска"},
               {"url": "https://example.test/contacts", "ref_id": "turn0view0", "snippet": "Total lines: 100"},
               {"title": "Internal Error", "ref_id": "turn0view1"}]
if mode != "local_only":
    print(json.dumps({"type": "item.completed", "item": {"type": "web_search", "id": "ws1",
          "action": action, "results": results}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 60}}))
