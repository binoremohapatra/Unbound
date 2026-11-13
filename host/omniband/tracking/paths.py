"""paths.py — Canonical runtime resource resolution and model integrity verification (M-07)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("omniband.tracking.paths")


class ModelIntegrityError(RuntimeError):
    """Raised when a model file is missing or corrupted at runtime."""
    pass


def get_base_dir() -> Path:
    """Return sys._MEIPASS if running as a frozen PyInstaller bundle, else repository root."""
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS).resolve()
        # Fallback for onedir mode if _MEIPASS is not set
        return Path(sys.executable).parent.resolve()
    # Development mode: paths.py is at host/omniband/tracking/paths.py -> root is 3 levels up
    return Path(__file__).resolve().parent.parent.parent.parent


def resource_path(rel: str | Path) -> Path:
    """Resolve a relative resource path against the base directory.

    Works correctly across frozen PyInstaller bundles and dev source tree,
    including paths containing spaces and non-ASCII characters.
    """
    base = get_base_dir()
    target = base / rel

    # If target doesn't exist, also check _internal directory in PyInstaller onedir mode
    if not target.exists() and getattr(sys, "frozen", False):
        alt = Path(sys.executable).parent / "_internal" / rel
        if alt.exists():
            return alt.resolve()
        alt2 = Path(sys.executable).parent / rel
        if alt2.exists():
            return alt2.resolve()

    return target.resolve()


_VERIFIED_MODELS: Dict[str, bytes] = {}


def load_verified_model_bytes(model_name: str) -> bytes:
    """Load and return model bytes after verifying SHA-256 against models/MANIFEST.json.

    M-01: Load model from BYTES (`BaseOptions(model_asset_buffer=...)`) to avoid
    path-encoding issues on Windows.
    M-07: Verifies size and SHA-256; raises ModelIntegrityError on any mismatch.
    """
    if model_name in _VERIFIED_MODELS:
        return _VERIFIED_MODELS[model_name]

    # Resolve manifest
    manifest_file = resource_path("models/MANIFEST.json")
    if not manifest_file.is_file():
        # Dev fallback: try relative to models
        manifest_file = resource_path("models") / "MANIFEST.json"

    manifest_entries = {}
    if manifest_file.is_file():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                entries = json.load(f)
                manifest_entries = {e["name"]: e for e in entries}
        except Exception as e:
            log.warning("Could not read models/MANIFEST.json: %s", e)

    # Locate model file
    candidates = [
        resource_path(f"models/{model_name}"),
        resource_path(model_name),
    ]
    model_path = None
    for c in candidates:
        if c.is_file():
            model_path = c
            break

    if model_path is None:
        raise ModelIntegrityError(
            f"Model file '{model_name}' not found. Expected in models/{model_name} or {_MEIPASS_str()}"
        )

    data = model_path.read_bytes()
    expected = manifest_entries.get(model_name)
    if expected:
        expected_sha = expected["sha256"].lower()
        actual_sha = hashlib.sha256(data).hexdigest().lower()
        if actual_sha != expected_sha:
            raise ModelIntegrityError(
                f"Model '{model_name}' failed integrity check!\n"
                f"  File: {model_path}\n"
                f"  Expected SHA-256: {expected_sha}\n"
                f"  Actual SHA-256:   {actual_sha}"
            )
        expected_size = expected.get("size")
        if expected_size and len(data) != expected_size:
            raise ModelIntegrityError(
                f"Model '{model_name}' size mismatch: expected {expected_size}, got {len(data)}"
            )

    _VERIFIED_MODELS[model_name] = data
    log.info("Model '%s' verified and loaded (%d bytes)", model_name, len(data))
    return data


def _MEIPASS_str() -> str:
    return getattr(sys, "_MEIPASS", "repo_root")

