"""Validate completed, local evidence before deriving further results."""

from __future__ import annotations

import json
from pathlib import Path

from scopeloop.saleae_labels import sha256


def validate_bundle(bundle: Path) -> dict:
    root = bundle.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "complete":
        raise ValueError("Evidence bundle must have status complete")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Evidence bundle has no artifact hashes")
    for name, info in files.items():
        path = (root / name).resolve()
        digest = info.get("sha256") if isinstance(info, dict) else info
        if root not in path.parents or not path.is_file() or sha256(path) != digest:
            raise ValueError(f"Evidence artifact hash mismatch or invalid path: {name}")
    return manifest


def require_artifact(manifest: dict, name: str) -> None:
    if name not in manifest["files"]:
        raise ValueError(f"Evidence artifact has no recorded hash: {name}")
