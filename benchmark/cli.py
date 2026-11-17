from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .codex import check_login, codex_env, resolve_binary, parse_events
from .excel import read_companies
from .report import check_export_paths, export_reports
from .runner import run_queue, prepare_card
from .storage import Store, now, run_lock
from .runtime import external_program, hidden_process


def positive_int(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("Нужно положительное целое число")
    return n


def nonnegative_int(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("Число не может быть отрицательным")
    return n


def nonempty_text(value):
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("Значение не может быть пустым")
    return value


def parser():
    p = argparse.ArgumentParser(prog="benchmark", description="Досье компаний через Codex CLI и подписку ChatGPT")
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Проверить CLI и вход через ChatGPT")
    doctor.add_argument("--codex", default="codex")
    login = commands.add_parser("login", help="Войти в Codex через ChatGPT")
    login.add_argument("--codex", default="codex")
    login.add_argument("--device-auth", action="store_true")
    gui = commands.add_parser("gui", help="Открыть настольное приложение")
    gui.add_argument("--run-dir", type=Path, help="Открыть существующее исследование")
    gui.add_argument("--codex", default="codex")
    gui.add_argument("--classic", action="store_true", help="Открыть прежний интерфейс Tkinter")
    init = commands.add_parser("init", help="Прочитать Excel и создать неизменяемую очередь")
    init.add_argument("input", type=Path)
    init.add_argument("--run-dir", type=Path, required=True)
    init.add_argument("--sheet")
    init.add_argument("--header-row", type=positive_int)
    init.add_argument("--limit", type=positive_int, help="Импортировать только первые N компаний")
    run = commands.add_parser("run", help="Запустить или продолжить обработку")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--codex", default="codex")
    run.add_argument("--max-companies", type=nonnegative_int, default=10, help="Попыток за запуск (по умолчанию 10; 0 — вся очередь)")
    run.add_argument("--timeout", type=positive_int, default=420, help="Секунд на компанию")
    run.add_argument("--token-budget", type=positive_int, help="Остановиться между компаниями после этого расхода токенов за запуск")
    run.add_argument("--model", help="Модель, доступная в вашей подписке; по умолчанию выбор CLI")
    selection = run.add_mutually_exclusive_group()
    selection.add_argument("--retry-failed", action="store_true", help="Вернуть ошибки в очередь; готовые карточки не трогать")
    selection.add_argument("--company-id", type=nonempty_text, help="Повторно исследовать только выбранную компанию, сохранив прежнюю карточку и историю")
    for name, help_text in [("status", "Показать прогресс"), ("export", "Пересобрать Excel и CSV"), ("stop", "Завершить после текущей компании"),
                            ("recheck", "Проверить сохранённые карточки по журналам без новых запросов")]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--run-dir", type=Path, required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "gui":
            try:
                if args.classic:
                    from .gui import launch
                else:
                    from .qt_gui import launch
            except ImportError as exc:
                hint = "Для прежнего интерфейса нужен Tkinter." if args.classic else 'Для интерфейса Qt установите зависимости: python -m pip install -e ".[gui]"'
                raise ValueError(hint) from exc
            return launch(args.run_dir, args.codex)
        if args.command == "doctor":
            binary = resolve_binary(args.codex)
            auth = check_login(binary)
            with external_program():
                version = subprocess.run([binary, "--version"], capture_output=True, text=True, env=codex_env(), timeout=20, **hidden_process())
                help_result = subprocess.run([binary, "exec", "--help"], capture_output=True, text=True, env=codex_env(), timeout=20, **hidden_process())
            missing = [flag for flag in ["--output-schema", "--json", "--ephemeral", "--ignore-user-config"] if flag not in help_result.stdout]
            if missing:
                raise ValueError("Обновите Codex CLI: отсутствуют " + ", ".join(missing))
            print(json.dumps({"codex": binary, "version": version.stdout.strip(), "login": auth, "api_key_required": False}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "login":
            command = [resolve_binary(args.codex), "login"]
            if args.device_auth:
                command.append("--device-auth")
            with external_program():
                return subprocess.call(command, env=codex_env(), **hidden_process())
        if args.command == "init":
            source = args.input.expanduser().resolve()
            check_export_paths(args.run_dir, source)
            if source.suffix.lower() != ".xlsx":
                raise ValueError("Первая версия принимает .xlsx. Пересохраните .xls/.xlsm в .xlsx.")
            records = read_companies(source, args.sheet, args.header_row)
            total_found = len(records)
            if args.limit:
                records = records[:args.limit]
            with run_lock(args.run_dir):
                store = Store(args.run_dir)
                try:
                    store.initialize(records, {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                               "imported_at": now(), "total_found": total_found, "imported": len(records)})
                    print(f"Найдено {total_found}; добавлено в очередь {len(records)}. Лист: {records[0]['sheet']}")
                finally:
                    store.close()
            return 0
        if not (args.run_dir / "state.sqlite3").is_file():
            raise ValueError("Очередь не найдена. Сначала выполните init.")
        if args.command == "run":
            return run_queue(args.run_dir, binary=args.codex, max_companies=args.max_companies, timeout=args.timeout,
                             model=args.model, retry_failed=args.retry_failed, token_budget=args.token_budget, company_id=args.company_id)
        if args.command == "stop":
            (args.run_dir / "stop.request").write_text(now(), encoding="utf-8")
            print("Запрошена остановка после сохранения текущей компании.")
            return 0
        if args.command == "recheck":
            with run_lock(args.run_dir):
                store = Store(args.run_dir)
                try:
                    for row in store.rows():
                        if not row["attempts"]:
                            continue
                        # Use the latest accepted attempt, not a newer failed response.
                        accepted = store.db.execute("SELECT number FROM attempts WHERE company_id=? AND outcome IN ('completed','needs_review') ORDER BY number DESC LIMIT 1", (row["id"],)).fetchone()
                        number = accepted[0] if accepted else row["attempts"]
                        attempt_dir = store.run_dir / "attempts" / row["id"] / f"{number:03d}"
                        try:
                            company = json.loads(row["input_json"])
                            raw = json.loads((attempt_dir / "card.json").read_text(encoding="utf-8"))
                            usage, failures, calls = parse_events(attempt_dir / "events.jsonl")
                            if failures:
                                continue
                            events = (attempt_dir / "events.jsonl").read_text(encoding="utf-8")
                            if not any(json.loads(line).get("type") == "turn.completed" for line in events.splitlines()):
                                continue
                            card, quality = prepare_card({"card": raw, "web_calls": calls}, company, attempt_dir)
                            if accepted:
                                store.update_card(row["id"], card, quality)
                            else:
                                state = "needs_review" if quality["needs_review"] else "completed"
                                store.finish(row["id"], number, state, card=card, quality=quality, usage=usage)
                        except (ValueError, OSError) as exc:
                            print(f"Не удалось перепроверить {row['id']}: {exc}", file=sys.stderr)
                    print(export_reports(store))
                finally:
                    store.close()
            return 0
        store = Store(args.run_dir)
        try:
            if args.command == "status":
                print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
            elif args.command == "export":
                print(export_reports(store))
        finally:
            store.close()
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
