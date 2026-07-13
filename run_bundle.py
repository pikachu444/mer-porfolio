"""Crash-recoverable multi-file JSON commits for one portfolio run."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping


def _encoded(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temporary, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.sha256(file.read()).hexdigest()


def recover_pending_bundle(manifest_path: Path) -> bool:
    """Finish a prepared commit after an interruption; return whether work existed."""
    if not manifest_path.exists():
        return False
    with open(manifest_path, encoding="utf-8") as file:
        manifest = json.load(file)
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("invalid pending run bundle manifest")

    for item in items:
        target = Path(item["target"])
        staged = Path(item["staged"])
        expected = str(item["sha256"])
        if staged.exists():
            if _file_sha256(staged) != expected:
                raise ValueError(f"staged bundle checksum mismatch: {staged}")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
        if not target.exists() or _file_sha256(target) != expected:
            raise ValueError(f"committed bundle checksum mismatch: {target}")

    manifest_path.unlink()
    return True


def commit_json_bundle(
    payloads: Mapping[Path, Any],
    *,
    manifest_path: Path,
) -> None:
    """Prepare every JSON file, then publish them with crash recovery metadata."""
    if not payloads:
        raise ValueError("run bundle must contain at least one payload")
    recover_pending_bundle(manifest_path)
    transaction_id = uuid.uuid4().hex
    items = []
    try:
        for raw_target, payload in sorted(payloads.items(), key=lambda pair: str(pair[0])):
            target = Path(raw_target).resolve()
            data = _encoded(payload)
            staged = target.with_name(f".{target.name}.{transaction_id}.staged")
            _atomic_bytes(staged, data)
            items.append({
                "target": str(target),
                "staged": str(staged),
                "sha256": _sha256(data),
            })
        _atomic_bytes(
            manifest_path,
            _encoded({"transaction_id": transaction_id, "items": items}),
        )
        recover_pending_bundle(manifest_path)
    except Exception:
        # Once the manifest exists, the next run must finish the exact prepared commit.
        if not manifest_path.exists():
            for item in items:
                Path(item["staged"]).unlink(missing_ok=True)
        raise
