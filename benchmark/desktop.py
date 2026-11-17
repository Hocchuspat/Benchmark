"""Desktop-facing state and process control; research stays in the existing CLI."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree

from .runtime import RESOURCE_ROOT, data_root, frozen, worker_command

PROJECT_ROOT = RESOURCE_ROOT
STATE_LABELS = {"pending": "В очереди", "running": "Обрабатывается", "completed": "Обработано",
                "needs_review": "Нужна проверка", "failed": "Ошибка"}
IDENTITY_LABELS = {"confirmed_by_inn": "Подтверждён по ИНН", "confirmed_by_details": "Подтверждён по реквизитам",
                   "unconfirmed": "Не подтверждён", "conflict": "Конфликт реквизитов"}
STOP_LABELS = {"queue_complete": "Очередь обработана", "selected_company": "Выбранная компания обработана",
               "selected_company_error": "Повторная обработка завершилась ошибкой. Подробности — в карточке компании.",
               "company_budget": "Достигнуто число компаний за запуск",
               "token_budget": "Достигнут бюджет токенов", "user_stop": "Остановлено пользователем",
               "interrupted": "Обработка прервана", "subscription_limit": "Достигнут лимит подписки. Продолжите после восстановления лимита",
               "authentication": "Нужно заново войти в Codex через ChatGPT", "consecutive_errors": "Обработка остановлена после трёх ошибок подряд"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass
class RunSnapshot:
    run_dir: Path
    source: dict
    companies: list[dict]
    states: dict[str, int]
    usage: dict[str, int]
    last_stop: dict | None

    @property
    def total(self):
        return len(self.companies)

    @property
    def saved(self):
        return sum(c["has_card"] for c in self.companies)


def read_snapshot(run_dir: Path) -> RunSnapshot:
    """Read-only connection: opening the UI never creates or changes a queue."""
    run_dir = run_dir.expanduser().resolve()
    db_path = run_dir / "state.sqlite3"
    if not db_path.is_file():
        raise ValueError("В этой папке нет исследования: файл state.sqlite3 не найден.")
    try:
        db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.15)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Не удалось открыть исследование: {exc}") from exc
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        metadata = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key,value FROM metadata")}
        companies, states = [], {}
        for row in db.execute("SELECT * FROM companies ORDER BY position"):
            original = json.loads(row["input_json"])
            card = json.loads(row["card_json"]) if row["card_json"] else None
            quality = json.loads(row["quality_json"]) if row["quality_json"] else {}
            states[row["state"]] = states.get(row["state"], 0) + 1
            companies.append({"id": row["id"], "position": row["position"], "name": original["name"],
                              "inn": original.get("inn", ""), "state": row["state"], "has_card": card is not None,
                              "site": (card or {}).get("official_site") or "", "card": card,
                              "input": original, "quality": quality, "error": row["error"] or "",
                              "attempts": row["attempts"]})
        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        for row in db.execute("SELECT usage_json FROM attempts WHERE usage_json IS NOT NULL"):
            data = json.loads(row[0])
            for key in usage:
                usage[key] += data.get(key, 0) or 0
        return RunSnapshot(run_dir, metadata.get("source", {}), companies, states, usage, metadata.get("last_stop"))
    except (sqlite3.DatabaseError, ValueError, KeyError) as exc:
        raise ValueError(f"Не удалось прочитать исследование: {exc}") from exc
    finally:
        db.close()


def sheet_names(path: Path) -> list[str]:
    """Only sheet names for the picker. Column detection/import belong to excel.py."""
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Выберите файл Excel в формате .xlsx.")
    try:
        with ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ValueError("Файл не является исправной книгой Excel .xlsx.") from exc
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [s.attrib["name"] for s in root.findall("x:sheets/x:sheet", ns)]


def save_report_copy(run_dir: Path, destination: Path) -> Path:
    snapshot = read_snapshot(run_dir)
    destination = destination.expanduser().resolve()
    if destination.suffix.lower() != ".xlsx":
        raise ValueError("Сохраните отчёт с расширением .xlsx.")
    if snapshot.source.get("path"):
        original = Path(snapshot.source["path"]).resolve()
        same_file = destination == original or (destination.exists() and original.exists() and destination.samefile(original))
        if same_file:
            raise ValueError("Нельзя заменить исходный Excel отчётом. Выберите другое имя файла.")
    source = snapshot.run_dir / "reports" / "report.xlsx"
    if not source.is_file():
        raise ValueError("Отчёт ещё не создан. Сначала обновите отчёт.")
    if destination == source:
        return destination
    fd, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".benchmark-", suffix=".xlsx")
    os.close(fd)
    try:
        shutil.copyfile(source, temp_name)
        os.replace(temp_name, destination)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return destination


class ProcessController:
    """No Tk calls in worker threads. All UI updates travel through an event queue."""
    def __init__(self, *, python=None, cwd=None, env=None):
        self.python = str(python) if python is not None else None
        self.cwd = Path(cwd or data_root())
        if frozen() and cwd is None:
            self.cwd.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy() if env is None else dict(env)
        self.env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        if frozen():
            self.env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        self.events = queue.Queue()
        self._guard = threading.Lock()
        self._busy = False
        self._process = None
        self._cancelled = False

    @property
    def busy(self):
        with self._guard:
            return self._busy

    def start(self, action: str, arguments: list[str]):
        with self._guard:
            if self._busy:
                raise ValueError("Дождитесь завершения текущей операции.")
            self._busy, self._cancelled = True, False
        threading.Thread(target=self._work, args=(action, list(arguments)), daemon=True).start()

    def _work(self, action, arguments):
        output, code = [], 1
        try:
            options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NO_WINDOW}
            process = subprocess.Popen(worker_command(arguments, python=self.python),
                                       cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", **options)
            with self._guard:
                self._process = process
                cancelled = self._cancelled
            if cancelled:
                self.cancel()
            with process.stdout:
                for line in process.stdout:
                    line = ANSI.sub("", line.rstrip("\r\n"))
                    output.append(line)
                    self.events.put({"type": "log", "action": action, "text": line})
            code = process.wait()
        except (OSError, ValueError) as exc:
            output.append(str(exc))
            self.events.put({"type": "log", "action": action, "text": str(exc)})
        finally:
            with self._guard:
                self._process, self._busy = None, False
                cancelled = self._cancelled
            self.events.put({"type": "finished", "action": action, "code": code,
                             "output": "\n".join(output), "cancelled": cancelled})

    def cancel(self):
        with self._guard:
            self._cancelled = True
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGINT)
        except (OSError, subprocess.SubprocessError):
            pass
