"""Paths and subprocess commands shared by source and standalone applications."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parent.parent


def frozen():
    return bool(getattr(sys, "frozen", False))


def data_root():
    """Never write inside the installed app (which may be read-only)."""
    if not frozen():
        return RESOURCE_ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Benchmark"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Benchmark"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Benchmark"


def runs_root():
    return data_root() / "runs"


def worker_executable():
    suffix = ".exe" if sys.platform == "win32" else ""
    return Path(sys.executable).with_name("Benchmark-worker" + suffix)


def worker_command(arguments, *, python=None):
    if frozen() and python is None:
        return [str(worker_executable()), "--benchmark-worker", *arguments]
    return [str(python or sys.executable), "-u", "-m", "benchmark", *arguments]


def bundled_codex():
    return RESOURCE_ROOT / "vendor" / "codex" / ("codex.exe" if sys.platform == "win32" else "codex")


def hidden_process():
    """Console-based tools still send output to pipes, without a Windows console."""
    if os.name == "nt":
        import subprocess
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


@contextmanager
def external_program():
    """Avoid passing PyInstaller's Windows DLL search path to native Codex."""
    if not frozen() or sys.platform != "win32":
        yield
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel.SetDllDirectoryW.restype = ctypes.c_int
    if not kernel.SetDllDirectoryW(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.SetDllDirectoryW(getattr(sys, "_MEIPASS", None))
