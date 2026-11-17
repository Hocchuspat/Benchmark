"""SQLite is authoritative; one transaction per company, recoverable attempts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.run_dir / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS companies (
                id TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE,
                input_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, card_json TEXT,
                quality_json TEXT, error TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                company_id TEXT NOT NULL, number INTEGER NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT,
                usage_json TEXT, error TEXT, PRIMARY KEY(company_id, number)
            );
        """)

    def close(self):
        self.db.close()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, dumps(value)))

    def initialize(self, records, source_info):
        with self.db:
            if self.db.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
                raise ValueError("Очередь уже создана. Используйте run для продолжения или другой --run-dir.")
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('source',?)", (dumps(source_info),))
            self.db.executemany(
                "INSERT INTO companies(id, position, input_json, updated_at) VALUES (?,?,?,?)",
                [(r["company_id"], i, dumps(r), now()) for i, r in enumerate(records, 1)],
            )

    def recover(self, company_id=None):
        with self.db:
            suffix = " AND company_id=?" if company_id else ""
            params = (now(), company_id) if company_id else (now(),)
            self.db.execute("UPDATE attempts SET outcome='interrupted',finished_at=? WHERE outcome IS NULL" + suffix, params)
            suffix = " AND id=?" if company_id else ""
            self.db.execute("UPDATE companies SET state='pending',error='Предыдущий запуск прерван',updated_at=? WHERE state='running'" + suffix, params)

    def rows(self):
        return self.db.execute("SELECT * FROM companies ORDER BY position").fetchall()

    def next_pending(self, company_id=None):
        if company_id:
            return self.db.execute("SELECT * FROM companies WHERE state='pending' AND id=?", (company_id,)).fetchone()
        return self.db.execute("SELECT * FROM companies WHERE state='pending' ORDER BY position LIMIT 1").fetchone()

    def retry_company(self, company_id):
        with self.db:
            cursor = self.db.execute("UPDATE companies SET state='pending',error=NULL,updated_at=? WHERE id=?", (now(), company_id))
            if not cursor.rowcount:
                raise ValueError("Компания не найдена в исследовании.")

    def start(self, company_id):
        with self.db:
            self.db.execute("UPDATE companies SET state='running',attempts=attempts+1,error=NULL,updated_at=? WHERE id=?", (now(), company_id))
            number = self.db.execute("SELECT attempts FROM companies WHERE id=?", (company_id,)).fetchone()[0]
            self.db.execute("INSERT INTO attempts(company_id,number,started_at) VALUES (?,?,?)", (company_id, number, now()))
        return number

    def finish(self, company_id, number, state, *, card=None, quality=None, error=None, usage=None):
        with self.db:
            self.db.execute("""UPDATE companies SET state=?,card_json=COALESCE(?,card_json),
                quality_json=COALESCE(?,quality_json),error=?,updated_at=? WHERE id=?""",
                (state, dumps(card) if card is not None else None,
                 dumps(quality) if quality is not None else None, error, now(), company_id))
            self.db.execute("""UPDATE attempts SET finished_at=?,outcome=?,usage_json=?,error=?
                WHERE company_id=? AND number=?""", (now(), state, dumps(usage or {}), error, company_id, number))

    def retry_failed(self):
        with self.db:
            self.db.execute("UPDATE companies SET state='pending',updated_at=? WHERE state='failed'", (now(),))

    def update_card(self, company_id, card, quality):
        with self.db:
            self.db.execute("UPDATE companies SET card_json=?,quality_json=?,state=?,updated_at=? WHERE id=?",
                            (dumps(card), dumps(quality), "needs_review" if quality["needs_review"] else "completed", now(), company_id))

    def summary(self):
        counts = dict(self.db.execute("SELECT state,COUNT(*) FROM companies GROUP BY state"))
        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        measured = 0
        for row in self.db.execute("SELECT usage_json FROM attempts WHERE usage_json IS NOT NULL"):
            u = json.loads(row[0])
            if u:
                measured += 1
            for k in usage:
                usage[k] += u.get(k, 0) or 0
        total_attempts = self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        return {"total": sum(counts.values()), "states": counts, "usage": usage,
                "attempts_total": total_attempts, "attempts_with_usage": measured,
                "attempts_without_usage": total_attempts - measured, "last_stop": self.meta("last_stop")}


@contextmanager
def run_lock(run_dir: Path):
    """OS lock releases on crash. Both POSIX and Windows supported."""
    path = run_dir.resolve() / "runner.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Для этой очереди уже запущен другой обработчик.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
