#!/usr/bin/env python3
"""Assemble a user-facing release zip: tracked files + templates + OCR models.

Usage:  python tools/build_release.py [version]
Output: dist/simauto-v<version>-win64.zip
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracked_files() -> list[str]:
    """Tracked paths.

    Under a restricted sandbox Python cannot create pipes for subprocess, so the
    caller may pre-generate the list with `git ls-files > list.txt` and point
    RELEASE_FILELIST at it.
    """
    preset = os.environ.get("RELEASE_FILELIST")
    if preset:
        with open(preset, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    return [p for p in out.splitlines() if p.strip()]


def extra_files() -> list[str]:
    """Files kept out of git (game screenshots, model weights, built exe) but shipped in the zip."""
    extras: list[str] = []
    exe = os.path.join(ROOT, "dist", "exe", "Simauto.exe")
    if os.path.isfile(exe):
        extras.append("Simauto.exe")
    tpl = os.path.join(ROOT, "templates")
    if os.path.isdir(tpl):
        for name in sorted(os.listdir(tpl)):
            if name.lower().endswith((".png", ".jpg", ".jpeg")):
                extras.append(f"templates/{name}")
    model = os.path.join(ROOT, "vendor", "ocr-model")
    if os.path.isdir(model):
        for name in sorted(os.listdir(model)):
            if name.lower().endswith((".onnx", ".txt")):
                extras.append(f"vendor/ocr-model/{name}")
    return extras


def main() -> int:
    ver = sys.argv[1] if len(sys.argv) > 1 else "0.1.0"
    stage = os.path.join(ROOT, "dist", f"simauto-v{ver}")
    zip_path = os.path.join(ROOT, "dist", f"simauto-v{ver}-win64.zip")

    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage, exist_ok=True)

    tracked = set(tracked_files())
    files = sorted(tracked) + [p for p in extra_files() if p not in tracked]
    skipped: list[str] = []
    for rel in files:
        src = os.path.join(ROOT, rel)
        if rel == "Simauto.exe":
            src = os.path.join(ROOT, "dist", "exe", "Simauto.exe")
        if not os.path.isfile(src):
            skipped.append(rel)
            continue
        dst = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    if os.path.exists(zip_path):
        os.remove(zip_path)
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in sorted(files):
            src = os.path.join(stage, rel)
            if os.path.isfile(src):
                zf.write(src, arcname=f"simauto-v{ver}/{rel}")
                count += 1

    size = os.path.getsize(zip_path) / 1024 / 1024
    print(f"packed {count} files -> {zip_path} ({size:.1f} MB)")
    if skipped:
        print("missing (skipped):", ", ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
