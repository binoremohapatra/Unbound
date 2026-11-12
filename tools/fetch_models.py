#!/usr/bin/env python3
"""tools/fetch_models.py — Download official models at dev/build time with SHA-256 integrity verification.

Rules:
- Never runs at application runtime (R06).
- Verifies SHA-256 against models/MANIFEST.json.
- On mismatch: halts immediately and prints expected vs actual.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"
MANIFEST_PATH = MODELS_DIR / "MANIFEST.json"


def compute_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def fetch_models(force: bool = False) -> int:
    if not MANIFEST_PATH.is_file():
        print(f"ERROR: Manifest not found at {MANIFEST_PATH}", file=sys.stderr)
        return 1

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    all_ok = True

    for entry in manifest:
        name = entry["name"]
        url = entry["url"]
        expected_sha = entry["sha256"].lower()
        expected_size = entry.get("size")
        target_path = MODELS_DIR / name

        print(f"\n[Model] {name}")
        needs_download = force or not target_path.is_file()

        if not needs_download and target_path.is_file():
            actual_sha = compute_sha256(target_path).lower()
            if actual_sha == expected_sha:
                print(f"  Verified existing file: SHA-256 matches ({actual_sha[:16]}...)")
                continue
            else:
                print(f"  Existing file SHA mismatch! Re-downloading...")
                needs_download = True

        if needs_download:
            print(f"  Downloading from: {url}")
            temp_path = target_path.with_suffix(".tmp")
            try:
                urllib.request.urlretrieve(url, temp_path)
            except Exception as e:
                print(f"  ERROR downloading {name}: {e}", file=sys.stderr)
                if temp_path.exists():
                    temp_path.unlink()
                all_ok = False
                continue

            actual_sha = compute_sha256(temp_path).lower()
            actual_size = temp_path.stat().st_size

            if expected_sha and actual_sha != expected_sha:
                print(f"  INTEGRITY FAILURE for {name}!", file=sys.stderr)
                print(f"    Expected SHA-256: {expected_sha}", file=sys.stderr)
                print(f"    Actual SHA-256:   {actual_sha}", file=sys.stderr)
                temp_path.unlink()
                all_ok = False
                continue

            if temp_path.exists():
                if target_path.exists():
                    target_path.unlink()
                temp_path.rename(target_path)

            print(f"  Downloaded & verified: {actual_size} bytes, SHA-256: {actual_sha[:16]}...")

    if not all_ok:
        print("\n[FAIL] One or more models failed integrity verification.", file=sys.stderr)
        return 2

    print("\n[OK] All models verified against manifest.")
    return 0


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    sys.exit(fetch_models(force=force_flag))

