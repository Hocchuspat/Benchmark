"""Read account quota through Codex's own read-only app-server protocol."""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time

from . import __version__
from .codex import codex_env, resolve_binary
from .runtime import external_program, hidden_process, stop_child_process


def quota_windows(result):
    """Keep quota windows only; account IDs, tokens and credit-reset actions stay out."""
    if not isinstance(result, dict):
        return []
    snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        by_id = result.get("rateLimitsByLimitId") or {}
        snapshot = by_id.get("codex", {}) if isinstance(by_id, dict) else {}
    windows = []
    for key in ["primary", "secondary"]:
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        used, duration, reset = window.get("usedPercent"), window.get("windowDurationMins"), window.get("resetsAt")
        if (isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used)
                or not 0 <= used <= 100 or not isinstance(duration, int) or duration <= 0):
            continue
        name = "неделя" if duration == 10080 else f"{duration // 60} ч" if duration % 60 == 0 else f"{duration} мин"
        windows.append({"name": name, "remaining": 100 - used,
                        "resets_at": reset if isinstance(reset, (int, float)) and math.isfinite(reset) else None})
    return windows


def read_limits(binary="codex", *, timeout=8, cancel_event=None):
    """No model turns, threads or login actions: only initialize and quota read."""
    proc = None
    messages = queue.Queue()
    deadline = time.monotonic() + timeout

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n"); proc.stdin.flush()

    def response(wanted):
        while time.monotonic() < deadline and not cancelled():
            try:
                message = messages.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                if proc.poll() is not None:
                    return None
                continue
            if isinstance(message, dict) and message.get("id") == wanted:
                return message.get("result") if "error" not in message else None
        return None

    try:
        if cancelled():
            return None
        args = [resolve_binary(binary), "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"', "app-server"]
        with external_program():
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", env=codex_env(),
                                    start_new_session=os.name != "nt", **hidden_process())

        def receive():
            with proc.stdout:
                for line in proc.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        pass
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "benchmark", "version": __version__}}})
        if response(1) is None:
            return None
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        windows = quota_windows(response(2))
        return {"windows": windows, "fetched_at": time.time()} if windows else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            stop_child_process(proc)
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
