"""Internal offline check of the frozen Qt application and graceful shutdown."""
from pathlib import Path
import json


def main(arguments):
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QTimer, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from .qt_gui import BenchmarkWindow

    run_dir, binary = map(str, arguments)
    app = QApplication([])
    # Exercise explicit process exit even if Qt would retain a macOS application.
    app.setQuitOnLastWindowClosed(False)
    settings = QSettings(str(Path(run_dir) / "offline-ui.ini"), QSettings.Format.IniFormat)
    window = BenchmarkWindow(run_dir=Path(run_dir), binary=binary, settings=settings, check_auth=False)
    window.quit_on_close = True; window.auth_ready = True; window.limits_due = float("inf")
    window.show(); window.refresh()
    window.batch.choose("Вся очередь")
    QCoreApplication.sendEvent(window.batch, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_1, Qt.KeyboardModifier.NoModifier, "1"))
    if window.batch_size() != 1:
        raise AssertionError("Custom count cannot be typed over the whole-queue placeholder")
    window.show_limits({"windows": [{"name": "5 ч", "remaining": 76}, {"name": "неделя", "remaining": 96}]})
    window.table.setCurrentIndex(window.proxy.index(0, 0))
    window.retry_selected()
    phase = [0]

    def poll():
        if phase[0] == 0 and window.snapshot.states.get("running"):
            phase[0] = 1; window.stop_button.click()
        elif phase[0] == 1 and window.action is None:
            phase[0] = 2; window.quit_action.trigger()

    timer = QTimer(); timer.timeout.connect(poll); timer.start(20)
    QTimer.singleShot(10000, lambda: app.exit(99))
    code = app.exec()
    if code or phase[0] != 2 or not window.closed:
        window.controller.cancel()
        raise AssertionError(f"Qt failed to stop and quit: {code}, {phase[0]}, {window.closed}")
    print(json.dumps({"gui": "passed", "custom_count": "passed", "stop_and_quit": "passed"}))
    return 0
