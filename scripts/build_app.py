"""Build a native client bundle. Run on each target OS using its own Python."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def make_icons(folder):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    folder.mkdir(parents=True, exist_ok=True)
    iconset = folder / "Benchmark.iconset"
    if iconset.exists(): shutil.rmtree(iconset)
    iconset.mkdir()
    for size in [16, 32, 64, 128, 256, 512, 1024]:
        image = QPixmap(size, size); image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor("#4566ED"))
        painter.drawRoundedRect(QRectF(size * .04, size * .04, size * .92, size * .92), size * .21, size * .21)
        font = QFont("Arial"); font.setPixelSize(round(size * .64)); font.setBold(True)
        painter.setFont(font); painter.setPen(QColor("white"))
        painter.drawText(QRectF(0, -size * .01, size, size), Qt.AlignmentFlag.AlignCenter, "B"); painter.end()
        image.save(str(folder / f"icon-{size}.png"))
        if size in [16, 32, 128, 256, 512]:
            image.save(str(iconset / f"icon_{size}x{size}.png"))
        if size in [32, 64, 256, 512, 1024]:
            half = size // 2
            image.save(str(iconset / f"icon_{half}x{half}@2x.png"))
    png = (folder / "icon-256.png").read_bytes()
    # Windows Vista+ supports PNG payloads in .ico files.
    (folder / "Benchmark.ico").write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(folder / "Benchmark.icns")], check=True)


def collect_licenses(folder):
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in ["PyInstaller", "PySide6-Essentials", "shiboken6", "openpyxl", "et-xmlfile", "jsonschema",
                 "attrs", "jsonschema-specifications", "referencing", "rpds-py"]:
        dist = importlib.metadata.distribution(name)
        rows.append(f"{name} {dist.version} — {dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'see included license files'}")
        for entry in dist.files or []:
            if any(word in str(entry).lower() for word in ["license", "licence", "copying", "notice", "authors"]) and ".dist-info/" in str(entry):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    target = folder / name / Path(*Path(str(entry)).parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)
    for source in (ROOT / "packaging" / "licenses").glob("*.txt"):
        shutil.copyfile(source, folder / source.name)
    (folder / "THIRD_PARTY.txt").write_text("Benchmark includes these unmodified dependencies:\n\n" + "\n".join(rows) +
        "\n\nCodex CLI: Apache-2.0, see vendor/codex/LICENSE and NOTICE.\n"
        "Qt/PySide6: LGPLv3; https://www.qt.io/qt-for-python and https://code.qt.io/pyside/pyside-setup\n"
        "Libraries remain separate shared files and may be replaced with compatible versions.\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-dir", type=Path, help="Already downloaded native CLI directory with LICENSE, NOTICE and manifest.json")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist", help="Output folder for this build")
    args = parser.parse_args()
    dist = args.dist_dir.resolve()
    if sys.platform not in {"darwin", "win32"}:
        parser.error("Build on macOS or Windows; cross-compilation is not supported.")
    vendor = args.codex_dir.resolve() if args.codex_dir else ROOT / "build" / "vendor" / "codex"
    if args.codex_dir is None:
        from fetch_codex import fetch
        fetch(vendor)
    for name in ["codex.exe" if sys.platform == "win32" else "codex", "LICENSE", "NOTICE", "manifest.json"]:
        if not (vendor / name).is_file():
            parser.error(f"Missing {vendor / name}")
    make_icons(ROOT / "build" / "icons")
    collect_licenses(ROOT / "build" / "licenses")
    env = os.environ.copy()
    env["BENCHMARK_CODEX_DIR"] = str(vendor)
    # Keep build caches inside the project, away from user settings/data.
    env["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build" / "pyinstaller-cache")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(dist), str(ROOT / "packaging" / "Benchmark.spec")],
                   cwd=ROOT, env=env, check=True)
    from benchmark import __version__
    target = f"{'macOS' if sys.platform == 'darwin' else 'Windows'}-{platform.machine().lower()}"
    if sys.platform == "darwin":
        bundle = dist / "Benchmark.app"
        archive = dist / f"Benchmark-{__version__}-{target}.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(archive)], check=True)
    else:
        bundle = dist / "Benchmark"
        archive = Path(shutil.make_archive(str(dist / f"Benchmark-{__version__}-{target}"), "zip", dist, "Benchmark"))
    print(f"\nApplication: {bundle}\nArchive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
