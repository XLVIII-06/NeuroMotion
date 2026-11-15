"""Experiment manifests and implementation-stage evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def file_info(path: str | Path) -> dict:
    source = Path(path).resolve()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(source), "size_bytes": source.stat().st_size, "sha256": digest.hexdigest()}


def create_session(path: str | Path, source: dict) -> dict:
    root = Path(path).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"Experiment directory must be empty: {root}")
    (root / "acquisition").mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 2, "session_id": uuid4().hex, "created_at": datetime.now().astimezone().isoformat(),
             "state": "recording", "source": source, "hardware_validated": False}
    write_json(root / "session.json", value)
    return value


def set_stage(stage: str, status: str, evidence: str) -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "Docs" / "EXECUTION_STATUS.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"plan": "NEUROMOTION_INTEGRATION_PLAN.md", "stages": {}}
    value["updated_at"] = datetime.now().astimezone().isoformat()
    value["stages"][stage] = {"status": status, "evidence": evidence}
    write_json(path, value)
