from __future__ import annotations

import os
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark import runtime
from benchmark.codex import resolve_binary


class RuntimeTests(unittest.TestCase):
    def test_worker_outputs_russian_in_utf8_even_with_ascii_system_streams(self):
        from benchmark.app import main
        buffers = [io.BytesIO() for _ in range(3)]
        streams = [io.TextIOWrapper(buffer, encoding="ascii") for buffer in buffers]
        def command(args):
            print("Найдено компаний: 1")
            return 0
        with patch.object(runtime.sys, "stdin", streams[0]), patch.object(runtime.sys, "stdout", streams[1]), \
             patch.object(runtime.sys, "stderr", streams[2]), patch("benchmark.cli.main", side_effect=command):
            result = main(["--benchmark-worker", "status"])
        self.assertEqual(result, 0)
        self.assertEqual(buffers[1].getvalue().decode("utf-8"), "Найдено компаний: 1\n")
        for stream in streams: stream.close()

    def test_source_keeps_project_paths_and_python_command(self):
        with patch.object(runtime.sys, "frozen", False, create=True):
            self.assertEqual(runtime.data_root(), runtime.RESOURCE_ROOT)
            self.assertEqual(runtime.worker_command(["status"], python="python-test"),
                             ["python-test", "-u", "-m", "benchmark", "status"])

    def test_mac_bundle_uses_writable_data_and_sibling_worker(self):
        with patch.object(runtime.sys, "frozen", True, create=True), patch.object(runtime.sys, "platform", "darwin"), \
             patch.object(runtime.sys, "executable", "/Applications/Benchmark.app/Contents/MacOS/Benchmark"), \
             patch.object(Path, "home", return_value=Path("/users/client")):
            self.assertEqual(runtime.runs_root(), Path("/users/client/Library/Application Support/Benchmark/runs"))
            self.assertEqual(runtime.worker_command(["export", "--run-dir", "/data/research"]),
                             ["/Applications/Benchmark.app/Contents/MacOS/Benchmark-worker", "--benchmark-worker", "export", "--run-dir", "/data/research"])

    def test_windows_bundle_uses_local_appdata_and_exe_helper(self):
        with patch.object(runtime.sys, "frozen", True, create=True), patch.object(runtime.sys, "platform", "win32"), \
             patch.object(runtime.sys, "executable", "/installed/Benchmark.exe"), \
             patch.dict(os.environ, {"LOCALAPPDATA": "/profile/AppData/Local"}):
            self.assertEqual(runtime.data_root(), Path("/profile/AppData/Local/Benchmark"))
            self.assertEqual(runtime.worker_command(["doctor"]),
                             ["/installed/Benchmark-worker.exe", "--benchmark-worker", "doctor"])

    def test_bundled_codex_works_without_shell_path_and_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            native = Path(folder) / "codex"
            native.touch()
            with patch("benchmark.codex.bundled_codex", return_value=native), patch("benchmark.codex.shutil.which", return_value=None):
                self.assertEqual(resolve_binary(), str(native))
            with patch("benchmark.codex.bundled_codex", return_value=native), patch("benchmark.codex.shutil.which", return_value="/test/fake"):
                self.assertEqual(resolve_binary("/test/fake"), "/test/fake")
