from __future__ import annotations

import json
import copy
from pathlib import Path

from . import codex
from .provenance import enforce_opened_sources, enforce_entity_scope
from .report import check_export_paths, export_reports
from .storage import Store, now, run_lock
from .validation import CardError, validate_card, validate_structure


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
    quality = validate_card(card, company)
    quality["provenance"] = provenance
    quality["entity_scope"] = entity_scope
    quality["web_calls"] = result.get("web_calls", 0)
    if not result.get("web_calls"):
        quality["warnings"].append("CLI не зарегистрировал веб-вызовы; проверить происхождение сведений")
        quality["needs_review"] = True
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


def run_queue(run_dir: Path, *, binary="codex", max_companies=10, timeout=420,
              model=None, retry_failed=False, token_budget=None, company_id=None, emit=print):
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
            try:
                while item := store.next_pending(company_id):
                    if stop_file.exists():
                        reason = "user_stop"
                        break
                    if max_companies and count >= max_companies:
                        reason = "company_budget"
                        break
                    usage = store.summary()["usage"]
                    spent = usage["input_tokens"] + usage["output_tokens"] - initial_tokens
                    if token_budget and spent >= token_budget:
                        reason = "token_budget"
                        break
                    company = json.loads(item["input_json"])
                    attempt = store.start(item["id"])
                    count += 1
                    emit(f"[{count}] {company['name']} · ИНН {company.get('inn', '—')}")
                    attempt_dir = store.run_dir / "attempts" / item["id"] / f"{attempt:03d}"
                    result = {}
                    try:
                        result = codex.execute(company, attempt_dir, resolved, timeout=timeout, model=model)
                        if "card" in result:
                            result["card"], quality = prepare_card(result, company, attempt_dir)
                            state = "needs_review" if quality["needs_review"] else "completed"
                            store.finish(item["id"], attempt, state, card=result["card"], quality=quality, usage=result.get("usage"))
                            failures = 0
                            emit(f"  Сохранено: {state}; источников: {quality['source_count']}")
                        else:
                            kind = result["error_kind"]
                            pause = kind in {"subscription_limit", "authentication"}
                            store.finish(item["id"], attempt, "pending" if pause else "failed",
                                         error=result["error"], usage=result.get("usage"))
                            emit(f"  Ошибка: {kind}. Подробности: {attempt_dir / 'stderr.log'}")
                            failures += 1
                            if pause:
                                code, reason = 75, kind
                                break
                    except KeyboardInterrupt:
                        store.finish(item["id"], attempt, "pending", error="Остановлено пользователем")
                        raise
                    except (CardError, OSError, ValueError) as exc:
                        store.finish(item["id"], attempt, "failed", error=str(exc), usage=result.get("usage"))
                        emit(f"  Карточка не сохранена как результат: {exc}")
                        failures += 1
                    if company_id and failures:
                        reason, code = "selected_company_error", 1
                        break
                    if failures >= 3:
                        reason, code = "consecutive_errors", 1
                        break
            except KeyboardInterrupt:
                reason, code = "interrupted", 130
                emit("Остановлено. Текущая компания возвращена в очередь.")
            finally:
                store.set_meta("last_stop", {"reason": reason, "at": now(), "attempted": count})
                export_reports(store)
            emit(json.dumps(store.summary(), ensure_ascii=False, indent=2))
            return code
        finally:
            store.close()
