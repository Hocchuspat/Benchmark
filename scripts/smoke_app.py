"""Exercise the packaged app with no Python in PATH and no model requests."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook


def smoke(bundle):
    windows = sys.platform == "win32"
    executables = bundle / "Contents" / "MacOS" if sys.platform == "darwin" else bundle
    resources = bundle / "Contents" / "Resources" if sys.platform == "darwin" else bundle / "_internal"
    app = executables / ("Benchmark.exe" if windows else "Benchmark")
    worker = executables / ("Benchmark-worker.exe" if windows else "Benchmark-worker")
    codex = resources / "vendor" / "codex" / ("codex.exe" if windows else "codex")
    for path in [app, worker, codex, resources / "AGENTS.md", resources / "skills" / "benchmark-find-site" / "SKILL.md",
                 resources / "benchmark" / "resources" / "chevron-down.svg"]:
        if not path.is_file(): raise AssertionError(f"Missing bundle resource: {path}")
    env = os.environ.copy()
    for name in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"]:
        env.pop(name, None)
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32") if windows else "/usr/bin:/bin"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    with tempfile.TemporaryDirectory(prefix="benchmark-bundle-smoke-") as folder:
        root = Path(folder)
        # Isolate preferences and authentication from the developer's account.
        env.update(HOME=str(root), USERPROFILE=str(root), LOCALAPPDATA=str(root / "local"),
                   XDG_CONFIG_HOME=str(root / "config"), CODEX_HOME=str(root / "codex-home"))
        def cli(*args, expected_code=0):
            result = subprocess.run([str(worker), "--benchmark-worker", *map(str, args)], cwd=root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=40)
            if result.returncode != expected_code:
                raise AssertionError(f"Worker failed: {args}\n{result.stdout}\n{result.stderr}")
            return result.stdout
        source = root / "companies.xlsx"; run_dir = root / "research"
        book = Workbook(); book.active.append(["Название компании", "ИНН", "Сайт", "Телефон", "Адрес"])
        book.active.append(["Проверка автономной сборки", "0123456789", "example.test", "9131234567",
                            "Омская обл., г.о. город Омск, г. Омск, ул. 5-я Северная, д. 163"])
        book.save(source); book.close()
        original = source.read_bytes()
        cli("init", source, "--run-dir", run_dir)
        cli("status", "--run-dir", run_dir)
        cli("export", "--run-dir", run_dir)
        report = load_workbook(run_dir / "reports" / "report.xlsx", read_only=True)
        assert report.sheetnames == ["Досье компаний"]
        assert list(next(report.active.values)) == ["Название компании", "ИНН", "Электронная страница", "Оквэд", "Продукция/Услуга", "Вид деятельности", "Функции"]
        report.close()
        # Native fixtures exercise the frozen prompt/schema, persistence and
        # resume without Python in PATH or any real Codex research calls.
        with closing(sqlite3.connect(run_dir / "state.sqlite3")) as db:
            company_id = db.execute("SELECT id FROM companies").fetchone()[0]
        schema = json.loads((resources / "benchmark" / "resources" / "company.schema.json").read_text())
        facts = {key: {"value": None, "source_ids": [], "note": "Offline packaging test"}
                 for key in schema["properties"]["facts"]["properties"]}
        card = {"company_id": company_id, "research_status": "not_found", "official_site": None,
                "identity": {"status": "unconfirmed", "reason": "Offline packaging test", "source_ids": []},
                "facts": facts, "sources": [], "missing_information": list(facts), "warnings": ["Offline packaging test"]}
        fixture = root / "card.json"; fixture.write_text(json.dumps(card), encoding="utf-8")
        env["BENCHMARK_SMOKE_CARD"] = str(fixture)
        if windows:
            fake = root / "fake-codex.exe"
            compiler = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
            if not compiler.is_file():
                raise AssertionError("The Windows build runner needs the .NET Framework C# compiler for its offline test fixture.")
            source_cs = root / "fake-codex.cs"
            source_cs.write_text(r'''using System;
using System.IO;
using System.Text;
class OfflineCodex {
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args.Length > 0 && args[0] == "login") {
            Console.WriteLine("Logged in using ChatGPT"); return 0;
        }
        if (Array.IndexOf(args, "--version") >= 0) {
            Console.WriteLine("offline-fixture"); return 0;
        }
        if (Array.IndexOf(args, "--help") >= 0) {
            Console.WriteLine("--output-schema --json --ephemeral --ignore-user-config"); return 0;
        }
        if (Environment.GetEnvironmentVariable("BENCHMARK_SMOKE_DELAY") == "1") System.Threading.Thread.Sleep(15000);
        Console.In.ReadToEnd();
        int index = Array.IndexOf(args, "--output-last-message");
        if (index < 0 || index + 1 >= args.Length) return 2;
        File.Copy(Environment.GetEnvironmentVariable("BENCHMARK_SMOKE_CARD"), args[index + 1], true);
        string events = Environment.GetEnvironmentVariable("BENCHMARK_SMOKE_EVENTS");
        if (!String.IsNullOrEmpty(events)) Console.Write(File.ReadAllText(events));
        else Console.WriteLine("{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":0,\"output_tokens\":0}}");
        return 0;
    }
}''', encoding="utf-8")
            subprocess.run([str(compiler), "/nologo", "/target:exe", f"/out:{fake}", str(source_cs)],
                           cwd=root, env=env, check=True, timeout=30)
        else:
            fake = root / "fake-codex"
            fake.write_text('''#!/bin/sh
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "--version" ]; then echo "offline-fixture"; exit 0; fi
for arg in "$@"; do
  if [ "$arg" = "--help" ]; then echo "--output-schema --json --ephemeral --ignore-user-config"; exit 0; fi
done
out=""
if [ "$BENCHMARK_SMOKE_DELAY" = "1" ]; then /bin/sleep 15; fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then out="$2"; shift; fi
  shift
done
/bin/cat > /dev/null
/bin/cp "$BENCHMARK_SMOKE_CARD" "$out"
if [ -n "$BENCHMARK_SMOKE_EVENTS" ]; then
  /bin/cat "$BENCHMARK_SMOKE_EVENTS"
else
  echo '{"type":"turn.completed","usage":{"input_tokens":0,"output_tokens":0}}'
fi
''', encoding="utf-8")
            fake.chmod(0o755)
        cli("doctor", "--codex", fake)
        cli("run", "--run-dir", run_dir, "--max-companies", "1", "--codex", fake)
        cli("run", "--run-dir", run_dir, "--max-companies", "1", "--codex", fake)
        with closing(sqlite3.connect(run_dir / "state.sqlite3")) as db:
            state, attempts, saved = db.execute("SELECT state,attempts,card_json IS NOT NULL FROM companies").fetchone()
            assert (state, attempts, saved) == ("needs_review", 1, 1), (state, attempts, saved)
        prompt = next((run_dir / "attempts").rglob("prompt.txt")).read_text(encoding="utf-8")
        assert "Обязательные навыки обработки" in prompt
        # Explicit repeat is available even when the only saved card needs review.
        cli("run", "--run-dir", run_dir, "--company-id", company_id, "--codex", fake)
        with closing(sqlite3.connect(run_dir / "state.sqlite3")) as db:
            previous_card, attempts = db.execute("SELECT card_json,attempts FROM companies").fetchone()
            assert attempts == 2
            assert db.execute("SELECT count(*) FROM card_history WHERE company_id=?", (company_id,)).fetchone()[0] == 1
        # One matching name must never qualify as confirmation by two details.
        bad = json.loads(json.dumps(card))
        bad["official_site"] = "https://example.test"
        bad["identity"] = {"status": "confirmed_by_details", "reason": "Only the name matches", "source_ids": ["s1"]}
        bad["facts"]["legal_name"] = {"value": "Проверка автономной сборки", "source_ids": ["s1"], "note": "Offline fixture"}
        bad["missing_information"].remove("legal_name")
        bad["sources"] = [{"source_id": "s1", "url": "https://example.test/contacts", "kind": "official",
                           "title": "Contacts", "accessed_at": date.today().isoformat(), "excerpt": "Offline fixture"}]
        fixture.write_text(json.dumps(bad), encoding="utf-8")
        cli("run", "--run-dir", run_dir, "--company-id", company_id, "--codex", fake, expected_code=1)
        with closing(sqlite3.connect(run_dir / "state.sqlite3")) as db:
            state, retained = db.execute("SELECT state,card_json FROM companies").fetchone()
            assert state == "failed" and retained == previous_card
        fixture.write_text(json.dumps(card), encoding="utf-8")
        cli("run", "--run-dir", run_dir, "--company-id", company_id, "--codex", fake)
        # Realistic multi-phone and abbreviated address matching, with harmless notes.
        complete = json.loads(json.dumps(card))
        complete["official_site"] = "https://example.test"
        complete["research_status"] = "completed"
        complete["identity"] = {"status": "confirmed_by_details", "reason": "Address and phone match", "source_ids": ["s1"]}
        complete["sources"] = bad["sources"]
        for key, value in {"phone": "+7 (3812) 49-55-09; +7 (913) 123-45-67",
                "legal_address": "Омская область, г. Омск, ул. 5-я Северная, д. 163",
                "primary_activity": "Монтаж систем", "products_services": "Монтаж", "functional_profile": "строительство", "product_service_type": "Услуги",
                "functions_summary": "Компания монтирует системы. Она обслуживает оборудование."}.items():
            complete["facts"][key] = {"value": value, "source_ids": ["s1"], "note": "Offline fixture"}
        complete["missing_information"] = [key for key, fact in complete["facts"].items() if fact["value"] is None]
        complete["warnings"] = ["Страница Т-Банка не открылась из-за тайм-аута; сведения её поискового сниппета не использованы."]
        events_path = root / "fixture-events.jsonl"
        event = {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "open_page"},
                 "results": [{"url": "https://example.test/contacts", "snippet": "Total lines: 10"}]}}
        events_path.write_text(json.dumps(event) + '\n' + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}) + '\n', encoding="utf-8")
        env["BENCHMARK_SMOKE_EVENTS"] = str(events_path)
        fixture.write_text(json.dumps(complete), encoding="utf-8")
        cli("run", "--run-dir", run_dir, "--company-id", company_id, "--codex", fake)
        with closing(sqlite3.connect(run_dir / "state.sqlite3")) as db:
            state, quality_json = db.execute("SELECT state,quality_json FROM companies").fetchone()
            quality = json.loads(quality_json)
            assert state == "completed" and quality["site_confirmed"] and len(quality["info_notes"]) == 1
        complete["warnings"] = ["Сведения относятся к группе компаний, принадлежность ООО не установлена."]
        fixture.write_text(json.dumps(complete), encoding="utf-8")
        cli("run", "--run-dir", run_dir, "--company-id", company_id, "--codex", fake)
        cli("recheck", "--run-dir", run_dir)
        with closing(sqlite3.connect(run_dir / "state.sqlite3")) as db:
            state, quality_json = db.execute("SELECT state,quality_json FROM companies").fetchone()
            assert state == "needs_review" and json.loads(quality_json)["site_confirmed"]
        assert list((run_dir / "backups").glob("*.sqlite3"))
        unsafe = root / "unsafe"; unsafe_source = unsafe / "reports" / "report.xlsx"
        unsafe_source.parent.mkdir(parents=True); unsafe_source.write_bytes(original)
        cli("init", unsafe_source, "--run-dir", unsafe, expected_code=1)
        assert unsafe_source.read_bytes() == original
        assert not (unsafe / "state.sqlite3").exists()
        version = subprocess.run([str(codex), "--version"], capture_output=True, text=True, env=env, timeout=30, check=True)
        print("Bundled CLI:", version.stdout.strip())
        # Import and initialize the packaged Chromium engine in its native helper.
        # An empty queue performs no requests and uses no personal browser profile.
        browser = subprocess.run([str(worker), "--benchmark-browser-worker"],
                                 input=json.dumps({"urls": [], "timeout": 2}), cwd=root, env=env,
                                 capture_output=True, text=True, encoding="utf-8", timeout=30)
        if browser.returncode or json.loads(browser.stdout) != {"pages": [], "attempts": []}:
            raise AssertionError(f"Packaged browser failed: {browser.stdout}\n{browser.stderr[-2000:]}")
        before = (run_dir / "state.sqlite3").read_bytes()
        # Verify the packaged GUI and its Qt plugins start outside the repository.
        with (root / "gui.log").open("w+") as log:
            process = subprocess.Popen([str(app), "--run-dir", str(run_dir)], cwd=root, env=env, stdout=log, stderr=log)
            try:
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
                if code is not None:
                    log.seek(0)
                    raise AssertionError(f"GUI exited during startup ({code}): {log.read()}")
            finally:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        assert (run_dir / "state.sqlite3").read_bytes() == before
        assert source.read_bytes() == original
        # Drive actual frozen Qt widgets and its event loop, including native
        # Quit, without Python in PATH or any real model calls.
        gui_env = {**env, "BENCHMARK_SMOKE_DELAY": "1"}
        result = subprocess.run([str(worker), "--benchmark-gui-smoke", str(run_dir), str(fake)],
                                cwd=root, env=gui_env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        if result.returncode:
            raise AssertionError(f"Frozen GUI regression: {result.stdout}\n{result.stderr[-2000:]}")
        assert json.loads(result.stdout)["stop_and_quit"] == "passed"
    print("Bundle smoke check passed: import, status, export, resources, native CLI, Chromium and GUI startup.")
    print("Offline processing and resume passed using the packaged worker, without system Python.")
    print("Frozen GUI count input, Stop and native Quit passed.")
    print("Audit regressions passed: selected retry, retention of previous card, two-detail identity and source-file protection.")
    print("Phone/address normalization, informational notes, blocking group claims and offline recheck passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    smoke(args.bundle.resolve())
