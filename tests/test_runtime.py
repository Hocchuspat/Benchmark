from __future__ import annotations

import os
import io
import tempfile
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from benchmark import runtime
from benchmark.codex import resolve_binary


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process group signals")
    def test_stop_terminates_owned_posix_process_group_then_kills_if_needed(self):
        proc = Mock(pid=123)
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("fake", .75), 0]
        with patch.object(runtime.os, "name", "posix"), patch.object(runtime.os, "killpg", create=True) as kill:
            runtime.stop_child_process(proc)
        self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123, signal.SIGKILL))
        self.assertEqual(proc.wait.call_count, 2)

    def test_stop_on_windows_targets_owned_process_and_child_tree(self):
        proc = Mock(pid=456)
        proc.poll.return_value = None
        with patch.object(runtime.os, "name", "nt"), patch.object(runtime, "hidden_process", return_value={}), patch.object(runtime.subprocess, "run", return_value=Mock(returncode=0)) as taskkill:
            runtime.stop_child_process(proc)
        self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "456", "/T", "/F"])
        proc.wait.assert_called_once()
        proc.kill.assert_not_called()

    def test_stopping_finished_process_does_not_send_signals(self):
        proc = Mock(); proc.poll.return_value = 0
        with patch.object(runtime.os, "killpg", create=True) as kill, patch.object(runtime.subprocess, "run") as taskkill:
            runtime.stop_child_process(proc)
        kill.assert_not_called(); taskkill.assert_not_called()

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
