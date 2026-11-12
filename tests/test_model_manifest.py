"""test_model_manifest.py — Verifies model integrity against models/MANIFEST.json (M-06)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pytest

from host.omniband.tracking.paths import (
    load_verified_model_bytes,
    resource_path,
    ModelIntegrityError,
)


def test_manifest_file_exists():
    p = resource_path("models/MANIFEST.json")
    assert p.is_file(), f"models/MANIFEST.json missing at {p}"


def test_manifest_entries_and_hashes():
    p = resource_path("models/MANIFEST.json")
    with open(p, "r", encoding="utf-8") as f:
        entries = json.load(f)

    assert isinstance(entries, list)
    assert len(entries) >= 2, "Expected at least face_landmarker.task and YuNet onnx"

    names = {e["name"] for e in entries}
    assert "face_landmarker.task" in names
    assert "face_detection_yunet_2023mar.onnx" in names

    for entry in entries:
        model_name = entry["name"]
        expected_sha = entry["sha256"].lower()
        expected_size = entry["size"]

        model_file = resource_path(f"models/{model_name}")
        assert model_file.is_file(), f"Model file missing: {model_file}"

        data = model_file.read_bytes()
        actual_size = len(data)
        actual_sha = hashlib.sha256(data).hexdigest().lower()

        assert actual_size == expected_size, (
            f"Size mismatch for {model_name}: expected {expected_size}, got {actual_size}"
        )
        assert actual_sha == expected_sha, (
            f"SHA-256 mismatch for {model_name}: expected {expected_sha}, got {actual_sha}"
        )


def test_load_verified_model_bytes_success():
    b = load_verified_model_bytes("face_landmarker.task")
    assert len(b) == 3758596


def test_load_verified_model_bytes_missing_raises():
    with pytest.raises(ModelIntegrityError):
        load_verified_model_bytes("non_existent_model_file.xyz")

