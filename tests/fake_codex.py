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
        "missing_information": [k for k, f in facts.items() if f["value"] is None], "warnings": []}
output = Path(args[args.index("--output-last-message") + 1])
if mode == "wrong_missing":
    card["missing_information"] = []
output.write_text("invalid json" if mode == "invalid" else json.dumps(card, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"type": "item.completed", "item": {"type": "web_search", "id": "ws1",
      "action": {"type": "open_page", "url": "https://example.test/contacts"},
      "results": [{"url": "https://example.test/contacts", "snippet": "Total lines: 100"}]}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 60}}))
