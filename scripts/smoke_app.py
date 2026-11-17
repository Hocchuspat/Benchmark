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
        def cli(*args):
            result = subprocess.run([str(worker), "--benchmark-worker", *map(str, args)], cwd=root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=40)
            if result.returncode:
                raise AssertionError(f"Worker failed: {args}\n{result.stdout}\n{result.stderr}")
            return result.stdout
        source = root / "companies.xlsx"; run_dir = root / "research"
        book = Workbook(); book.active.append(["Название компании", "ИНН", "Сайт"])
        book.active.append(["Проверка автономной сборки", "0123456789", "example.test"])
        book.save(source); book.close()
        original = source.read_bytes()
        cli("init", source, "--run-dir", run_dir)
        cli("status", "--run-dir", run_dir)
        cli("export", "--run-dir", run_dir)
        report = load_workbook(run_dir / "reports" / "report.xlsx", read_only=True)
        assert report.sheetnames; report.close()
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
        Console.In.ReadToEnd();
        int index = Array.IndexOf(args, "--output-last-message");
        if (index < 0 || index + 1 >= args.Length) return 2;
        File.Copy(Environment.GetEnvironmentVariable("BENCHMARK_SMOKE_CARD"), args[index + 1], true);
        Console.WriteLine("{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":0,\"output_tokens\":0}}");
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
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then out="$2"; shift; fi
  shift
done
/bin/cat > /dev/null
/bin/cp "$BENCHMARK_SMOKE_CARD" "$out"
echo '{"type":"turn.completed","usage":{"input_tokens":0,"output_tokens":0}}'
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
        version = subprocess.run([str(codex), "--version"], capture_output=True, text=True, env=env, timeout=30, check=True)
        print("Bundled CLI:", version.stdout.strip())
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
    print("Bundle smoke check passed: import, status, export, resources, native CLI and GUI startup.")
    print("Offline processing and resume passed using the packaged worker, without system Python.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    smoke(args.bundle.resolve())
