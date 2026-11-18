from __future__ import annotations

import json
import copy
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from . import codex
from .provenance import enforce_opened_sources, enforce_entity_scope
from .report import check_export_paths, export_reports
from .storage import Store, now, run_lock
from .validation import CardError, host, validate_card, validate_structure


def prepare_card(result, company, attempt_dir):
    validate_structure(result["card"], company)
    raw = copy.deepcopy(result["card"])
    missing = [k for k, f in raw["facts"].items() if f["value"] is None]
    if set(raw["missing_information"]) != set(missing):
        raw["missing_information"] = missing
        raw["warnings"].append("Локальная проверка исправила missing_information по null-полям карточки.")
    validate_card(raw, company)
    card, provenance = enforce_opened_sources(raw, attempt_dir / "events.jsonl")
    card, entity_scope = enforce_entity_scope(card, company)
    if card["research_status"] == "site_unavailable" and card["official_site"] and any(
            host(url) == host(card["official_site"]) for url in provenance["local_page_urls"]):
        card["research_status"] = "needs_review"
        card["warnings"].append("Локальный загрузчик открыл страницы сайта-кандидата; общий вывод о недоступности сайта требует уточнения.")
    quality = validate_card(card, company)
    quality["provenance"] = provenance
    quality["entity_scope"] = entity_scope
    quality["web_calls"] = result.get("web_calls", 0)
    if not result.get("web_calls") and not provenance["local_page_urls"]:
        message = "CLI не зарегистрировал веб-вызовы; проверить происхождение сведений"
        quality["warnings"].append(message)
        quality["review_reasons"].append(message)
        quality["needs_review"] = True
        quality["report_status"] = "needs_review"
    quality["original_research_status"] = raw["research_status"]
    card["research_status"] = "needs_review" if quality["needs_review"] else "completed"
    return card, quality


def recover_completed_files(store, emit, company_id=None):
    """Recover a completed response if the parent died before the SQLite commit."""
    for row in store.rows():
        if company_id and row["id"] != company_id:
            continue
        if row["state"] not in {"pending", "running"} or not row["attempts"]:
            continue
        folder = store.run_dir / "attempts" / row["id"] / f"{row['attempts']:03d}"
        try:
            events_path = folder / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            if not any(isinstance(e, dict) and e.get("type") == "turn.completed" for e in events):
                continue
            usage, failures, calls = codex.parse_events(events_path)
            if failures:
                continue
            result = {"card": json.loads((folder / "card.json").read_text(encoding="utf-8")), "web_calls": calls}
            card, quality = prepare_card(result, json.loads(row["input_json"]), folder)
            store.finish(row["id"], row["attempts"], "needs_review" if quality["needs_review"] else "completed",
                         card=card, quality=quality, usage=usage)
            emit(f"Восстановлен готовый ответ из журнала: {row['id']} (без нового запроса)")
        except (OSError, ValueError):
            continue


def run_queue(run_dir: Path, *, binary="codex", max_companies=0, timeout=420,
              model=None, retry_failed=False, token_budget=None, company_id=None, workers=2, emit=print):
    if not 1 <= workers <= 4:
        raise ValueError("Число одновременных компаний должно быть от 1 до 4.")
    # An explicit token budget retains a strict check between companies.
    concurrency = 1 if company_id or token_budget else workers
    with run_lock(run_dir):
        store = Store(run_dir)
        try:
            if not store.rows():
                raise ValueError("Очередь пуста. Сначала выполните init.")
            check_export_paths(store.run_dir, store.meta("source", {}).get("path"))
            if company_id and retry_failed:
                raise ValueError("Выберите повтор одной компании или всех ошибок.")
            if company_id and not any(row["id"] == company_id for row in store.rows()):
                raise ValueError("Компания не найдена в исследовании.")
            resolved = codex.resolve_binary(binary)
            codex.check_login(resolved)
            recover_completed_files(store, emit, company_id)
            store.recover(company_id)
            if company_id:
                store.retry_company(company_id)
            if retry_failed:
                store.retry_failed()
            stop_file = store.run_dir / "stop.request"
            stop_file.unlink(missing_ok=True)
            store.set_meta("last_stop", None)
            count, failures, code, reason = 0, 0, 0, "selected_company" if company_id else "queue_complete"
            initial_usage = store.summary()["usage"]
            initial_tokens = initial_usage["input_tokens"] + initial_usage["output_tokens"]
            cancelled = threading.Event()
            active = {}
            halted = False
            priorities = {"queue_complete": 0, "selected_company": 0, "company_budget": 1, "token_budget": 2,
                "user_stop": 3, "selected_company_error": 4, "consecutive_errors": 4,
                "subscription_limit": 5, "authentication": 5, "interrupted": 6}

            def halt(why, exit_code=0):
                nonlocal halted, reason, code
                halted = True
                if priorities[why] >= priorities[reason]:
                    reason, code = why, exit_code

            pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="benchmark-company")
            try:
                while active or not halted:
                    try:
                        if stop_file.exists():
                            cancelled.set(); halt("user_stop")
                        while not halted and len(active) < concurrency:
                            if stop_file.exists():
                                cancelled.set(); halt("user_stop"); break
                            item = store.next_pending(company_id)
                            if item is None:
                                break
                            if max_companies and count >= max_companies:
                                halt("company_budget"); break
                            usage = store.summary()["usage"]
                            spent = usage["input_tokens"] + usage["output_tokens"] - initial_tokens
                            if token_budget and spent >= token_budget:
                                halt("token_budget"); break
                            company = json.loads(item["input_json"])
                            if item["card_json"]:
                                # A discovered site may be absent from Excel. Keep it a
                                # candidate, and let the normal identity checks verify it.
                                prior_site = json.loads(item["card_json"]).get("official_site")
                                if prior_site:
                                    company["candidate_websites"] = [prior_site]
                            attempt = store.start(item["id"])
                            count += 1
                            folder = store.run_dir / "attempts" / item["id"] / f"{attempt:03d}"
                            emit(f"[{count}] {company['name']} · ИНН {company.get('inn', '—')}")
                            future = pool.submit(codex.execute, company, folder, resolved, timeout=timeout,
                                                 model=model, cancel_event=cancelled)
                            active[future] = (item["id"], attempt, company, folder)
                        if not active:
                            break
                        finished, _ = wait(active, timeout=.15, return_when=FIRST_COMPLETED)
                        for future in sorted(finished, key=lambda f: active[f][0]):
                            item_id, attempt, company, folder = active[future]
                            result = {}
                            try:
                                result = future.result()
                                if "card" in result:
                                    card, quality = prepare_card(result, company, folder)
                                    state = "needs_review" if quality["needs_review"] else "completed"
                                    store.finish(item_id, attempt, state, card=card, quality=quality, usage=result.get("usage"))
                                    failures = 0
                                    emit(f"  Сохранено: {company['name']}; источников: {quality['source_count']}")
                                else:
                                    kind = result["error_kind"]
                                    pause = kind in {"subscription_limit", "authentication", "interrupted"}
                                    store.finish(item_id, attempt, "pending" if pause else "failed",
                                                 error=result["error"], usage=result.get("usage"))
                                    emit(f"  Ошибка: {company['name']}: {kind}. Подробности: {folder / 'stderr.log'}")
                                    if kind != "interrupted":
                                        failures += 1
                                    if pause:
                                        if kind == "interrupted" and stop_file.exists():
                                            halt("user_stop")
                                        else:
                                            halt(kind, 130 if kind == "interrupted" else 75)
                            except KeyboardInterrupt:
                                cancelled.set(); halt("interrupted", 130)
                                store.finish(item_id, attempt, "pending", error="Остановлено пользователем")
                            except Exception as exc:
                                store.finish(item_id, attempt, "failed", error=str(exc), usage=result.get("usage"))
                                emit(f"  Карточка не сохранена как результат: {company['name']}: {exc}")
                                failures += 1
                            finally:
                                active.pop(future, None)
                            if company_id and failures:
                                halt("selected_company_error", 1)
                            if failures >= 3:
                                halt("consecutive_errors", 1)
                    except KeyboardInterrupt:
                        cancelled.set(); halt("interrupted", 130)
                        emit("Останавливаем запущенные задания и сохраняем готовые результаты…")
            finally:
                # SQLite is only accessed by this coordinating thread. Workers
                # write separate attempt folders; every accepted result commits
                # immediately, before waiting for any other company.
                pool.shutdown(wait=True)
                recover_completed_files(store, emit, company_id)
                store.recover(company_id)
                store.set_meta("last_stop", {"reason": reason, "at": now(), "attempted": count, "workers": concurrency})
                export_reports(store)
            emit(json.dumps(store.summary(), ensure_ascii=False, indent=2))
            return code
        finally:
            store.close()
