"""Standalone entry point. The same backend also powers the terminal version."""
from __future__ import annotations

import sys
import traceback

from .runtime import data_root


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--benchmark-worker"]:
        from .cli import main as cli_main
        return cli_main(args[1:])

    # Windowed executables on Windows have no stdout/stderr. Keep startup errors
    # available without making the user start the app from a terminal.
    log = None
    if sys.stdout is None or sys.stderr is None:
        folder = data_root() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        log = (folder / "application.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None: sys.stdout = log
        if sys.stderr is None: sys.stderr = log
    try:
        from .cli import main as cli_main
        return cli_main(["gui", *args])
    except Exception:
        folder = data_root() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "startup-error.log"
        path.write_text(traceback.format_exc(), encoding="utf-8")
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "Benchmark", f"Не удалось запустить приложение. Подробности: {path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
