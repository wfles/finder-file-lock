#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
MANIFEST = INSTALLER / "dependencies.json"
OWNER = "io.github.wfles.finder-file-lock"
ARCHITECTURES = ("arm64", "x86_64")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(*args):
    subprocess.run(args, check=True)


def get_asset(asset, cache):
    path = cache / asset["name"]
    if path.exists() and sha256(path) == asset["sha256"]:
        return path
    with NamedTemporaryFile(dir=cache, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            run("/usr/bin/curl", "--fail", "--silent", "--show-error", "--location", "--retry", "2",
                "--proto", "=https", "--proto-redir", "=https", "--output", temporary, asset["url"])
            if sha256(temporary) != asset["sha256"]:
                raise ValueError(f"SHA-256 mismatch: {asset['name']}")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def build_icon_helper(workspace, destination):
    parts = []
    for arch in ARCHITECTURES:
        target = workspace / ("setup-icon-" + arch)
        run("/usr/bin/xcrun", "swiftc", "-O", "-target", f"{arch}-apple-macos15.0",
            ROOT / "bin/setup-icon.swift", "-o", target)
        parts.append(target)
    run("/usr/bin/lipo", "-create", *parts, "-output", destination)
    run("/usr/bin/codesign", "--force", "--sign", "-", destination)


def build_payload(workspace, assets):
    payload = workspace / "payload"
    for name in ("actions", "templates"):
        shutil.copytree(ROOT / name, payload / name, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    executables = payload / "bin"
    executables.mkdir()
    for name in ("run-action", "uninstall-actions"):
        shutil.copy2(ROOT / "bin" / name, executables)
    build_icon_helper(workspace, executables / "setup-icon")

    runtime = payload / "runtime"
    for arch in ARCHITECTURES:
        target = runtime / arch
        qpdf = target / "qpdf"
        qpdf.mkdir(parents=True)
        python_arch = "aarch64" if arch == "arm64" else arch
        python = next(path for name, path in assets.items()
                      if f"-{python_arch}-apple-darwin-install_only_stripped" in name)
        qpdf_archive = next(path for name, path in assets.items() if name.endswith(f"-macos-{arch}.zip"))
        run("/usr/bin/tar", "-xf", python, "-C", target)
        run("/usr/bin/unzip", "-q", qpdf_archive, "-d", qpdf)
    lxml = next(path for name, path in assets.items() if name.endswith(".whl"))
    run("/usr/bin/unzip", "-q", lxml, "-d", runtime / "python-packages")

    licenses = payload / "licenses"
    python_licenses = licenses / "python-standalone"
    python_licenses.mkdir(parents=True)
    run("/usr/bin/tar", "-xf", assets["python-full-arm64.tar.zst"],
        "-C", python_licenses, "--strip-components", "2", "python/licenses")
    for name in ("qpdf-LICENSE.txt", "qpdf-Artistic-2.0"):
        shutil.copy2(assets[name], licenses / name)
    shutil.copy2(MANIFEST, licenses / "dependency-sources.json")
    shutil.copy2(ROOT / "LICENSE", licenses / "Finder-File-Lock-MIT.txt")
    return payload


def main():
    parser = argparse.ArgumentParser(description="Build the Finder File Lock macOS installer.")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args()
    if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0]) < 15:
        parser.error("Building requires macOS 15 or later and Xcode Command Line Tools")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("--version must use major.minor.patch")

    cache = args.cache_dir.resolve()
    output = args.output_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        downloaded = pool.map(lambda asset: get_asset(asset, cache), manifest)
        assets = {path.name: path for path in downloaded}

    name = f"Finder-File-Lock-{args.version}-macOS.pkg"
    with TemporaryDirectory(prefix=".build-", dir=output) as temporary:
        workspace = Path(temporary)
        payload = build_payload(workspace, assets)
        scripts = workspace / "scripts"
        scripts.mkdir()
        for script in ("preinstall", "postinstall"):
            shutil.copy2(INSTALLER / script, scripts)
        component = workspace / "file-actions-component.pkg"
        package = workspace / name
        run("/usr/bin/pkgbuild", "--root", payload, "--scripts", scripts,
            "--identifier", OWNER, "--version", args.version, "--install-location",
            "/Library/Application Support/FinderFileLock", component)
        run("/usr/bin/productbuild", "--distribution", INSTALLER / "Distribution.xml",
            "--resources", INSTALLER / "resources", "--package-path", workspace, package)
        checksum = workspace / "SHA256SUMS.txt"
        checksum.write_text(f"{sha256(package)}  {name}\n", encoding="utf-8")
        os.replace(package, output / name)
        os.replace(checksum, output / checksum.name)
    print(output / name)


if __name__ == "__main__":
    main()
