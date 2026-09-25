"""Version-sensitive native SAL labels, derived from the private bench helper.

The automation API has no label setter. Preserve the source, edit only a copy,
then require Logic to reopen/resave it and verify the saved metadata.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile


def label_map(channel_map: dict) -> dict[tuple[str, int], str]:
    result = {}
    shared = {}
    for kind in ("digital", "analog"):
        for channel, entry in channel_map.get(kind, {}).items():
            name = entry.get("name") if isinstance(entry, dict) else entry
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Missing label for {kind} {channel}")
            index = int(channel)
            if index < 0 or (index in shared and shared[index] != name):
                raise ValueError("Analog/digital views of a physical channel must share a name")
            shared[index] = name
            result[(kind.capitalize(), index)] = name
    return result


def _metadata(path: Path) -> dict:
    with ZipFile(path) as archive:
        info = archive.getinfo("meta.json")
        if info.file_size > 16_000_000:
            raise ValueError("Oversized SAL metadata")
        return json.loads(archive.read(info))


def verify_labels(path: Path, channel_map: dict) -> None:
    expected = label_map(channel_map)
    found = set()
    for row in _metadata(path)["data"]["rowsSettings"]:
        channel = row.get("channel")
        if row.get("type") != "channel" or not channel:
            continue
        key = (channel["type"], int(channel["deviceChannel"]))
        if key in expected:
            if row.get("name") != expected[key]:
                raise ValueError(f"Native label mismatch: {key}")
            found.add(key)
    if found != set(expected):
        raise ValueError(f"Native channels missing: {set(expected) - found}")


def make_labeled_copy(source: Path, destination: Path, channel_map: dict) -> None:
    expected = label_map(channel_map)
    metadata = _metadata(source)
    for row in metadata["data"]["rowsSettings"]:
        channel = row.get("channel")
        if row.get("type") == "channel" and channel:
            key = (channel["type"], int(channel["deviceChannel"]))
            if key in expected:
                row["name"] = expected[key]
    # Exclusive creation protects both original evidence and previous labeled files.
    with (
        destination.open("xb") as output,
        ZipFile(source) as original,
        ZipFile(output, "w") as copy,
    ):
        for info in original.infolist():
            if info.filename == "meta.json":
                copy.writestr(info, json.dumps(metadata).encode())
            else:
                # Stream large captures without extracting paths from the archive.
                with original.open(info) as src, copy.open(info, "w") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
    verify_labels(destination, channel_map)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def label_and_verify(driver, source: Path, channel_map: dict) -> dict:
    original_hash = sha256(source)
    candidate = source.with_name(source.stem + "-labeled.sal")
    verified = source.with_name(source.stem + "-labeled-verified.sal")
    if verified.exists():
        raise FileExistsError(verified)
    make_labeled_copy(source, candidate, channel_map)
    reopened = await driver.load_capture(candidate)
    try:
        await driver.save_capture(reopened, verified)
    finally:
        await driver.close_capture(reopened)
    verify_labels(verified, channel_map)
    if sha256(source) != original_hash:
        raise ValueError("Original capture changed during labeling")
    return {
        "native_labels_applied": True,
        "native_reopen_verified": True,
        "original": source.name,
        "original_sha256": original_hash,
        "verified_capture": verified.name,
        "verified_sha256": sha256(verified),
    }
