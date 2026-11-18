# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
sys.path.insert(0, str(root))
from benchmark import __version__
vendor = Path(os.environ["BENCHMARK_CODEX_DIR"])
datas = [(str(root / "AGENTS.md"), "."), (str(root / "skills"), "skills"),
         (str(root / "packaging" / "START.txt"), "."),
         (str(root / "benchmark" / "resources"), "benchmark/resources"),
         (str(root / "build" / "licenses"), "licenses")]
binaries = []
for path in vendor.rglob("*"):
    if path.is_file():
        target = "vendor/codex/" + str(path.parent.relative_to(vendor))
        if path.suffix.lower() in {".exe", ".dll", ".dylib"} or path.name in {"codex", "codex-code-mode-host"}:
            binaries.append((str(path), target))
        else:
            datas.append((str(path), target))
datas += collect_data_files("jsonschema_specifications")
datas += collect_data_files("certifi")
a = Analysis([str(root / "packaging" / "launcher.py")], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=["benchmark.qt_gui", "benchmark.browser_fetch"],
             excludes=["tkinter", "_tkinter", "benchmark.gui", "unittest", "pytest", "pip", "setuptools"],
             noarchive=False)
pyz = PYZ(a.pure)
icon = root / "build" / "icons" / ("Benchmark.icns" if sys.platform == "darwin" else "Benchmark.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Benchmark", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, argv_emulation=False, icon=str(icon))
# A console-subsystem helper allows reliable pipe output on Windows; the GUI
# starts it with CREATE_NO_WINDOW. It shares the embedded Python and libraries.
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Benchmark-worker", debug=False,
             bootloader_ignore_signals=False, strip=False, upx=False, console=True)
collection = COLLECT(exe, worker, a.binaries, a.datas, strip=False, upx=False, name="Benchmark")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="Benchmark.app", icon=str(icon),
                 bundle_identifier="com.hocchuspat.benchmark",
                 info_plist={"CFBundleDisplayName": "Benchmark", "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "14.0"})
