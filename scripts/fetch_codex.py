"""Download a pinned official CLI at build time, verify its release SHA-256."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "0.166.0"


def target_triple():
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64" if machine in {"amd64", "x86_64"} else None
    system = {"Darwin": "apple-darwin", "Windows": "pc-windows-msvc"}.get(platform.system())
    if not arch or not system:
        raise ValueError("Сборка поддерживается на macOS и Windows (arm64/x64).")
    return f"{arch}-{system}"


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Benchmark-build"}), timeout=120)


def fetch(destination: Path):
    target = target_triple()
    tag = f"rust-v{VERSION}"
    with request(f"https://api.github.com/repos/openai/codex/releases/tags/{tag}") as response:
        release = json.load(response)
    windows = platform.system() == "Windows"
    names = [f"codex-{target}{'.exe' if windows else ''}.tar.gz"]
    names.append(f"codex-code-mode-host-{target}{'.exe' if windows else ''}.tar.gz")
    if windows:
        names.extend(f"{name}-{target}.exe.tar.gz" for name in ["codex-command-runner", "codex-windows-sandbox-setup"])
    assets = {asset["name"]: asset for asset in release["assets"]}
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {"version": VERSION, "target": target, "assets": []}
    with tempfile.TemporaryDirectory(prefix="benchmark-codex-") as scratch:
        for name in names:
            asset = assets[name]
            digest = asset.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise ValueError(f"У официального файла нет SHA-256: {name}")
            archive_path = Path(scratch) / name
            print(f"Downloading {name} ({asset['size'] // 1024 // 1024} MB)", flush=True)
            with request(asset["browser_download_url"]) as response, archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
            with archive_path.open("rb") as downloaded:
                actual = "sha256:" + hashlib.file_digest(downloaded, "sha256").hexdigest()
            if actual != digest:
                raise ValueError(f"SHA-256 не совпадает: {name}")
            # Extract only ordinary files, flattening executable paths. Never use
            # archive paths as destination paths or follow links from an archive.
            with tarfile.open(archive_path) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    basename = Path(member.name).name
                    normalized = basename.replace(f"-{target}", "")
                    if normalized not in {"codex", "codex.exe", "codex-code-mode-host", "codex-code-mode-host.exe",
                                          "codex-command-runner.exe", "codex-windows-sandbox-setup.exe"}:
                        continue
                    with archive.extractfile(member) as source, (destination / normalized).open("wb") as output:
                        shutil.copyfileobj(source, output)
                    (destination / normalized).chmod(0o755)
            manifest["assets"].append({"name": name, "sha256": actual.split(":", 1)[1], "url": asset["browser_download_url"]})
    for filename in ["LICENSE", "NOTICE"]:
        with request(f"https://raw.githubusercontent.com/openai/codex/{tag}/{filename}") as response:
            (destination / filename).write_bytes(response.read())
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    required = ["codex", "codex-code-mode-host"]
    if windows: required += ["codex-command-runner", "codex-windows-sandbox-setup"]
    if any(not (destination / (name + (".exe" if windows else ""))).is_file() for name in required):
        raise ValueError("Архив Codex не содержит ожидаемые исполняемые файлы.")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("build/vendor/codex"))
    args = parser.parse_args()
    fetch(args.destination)
