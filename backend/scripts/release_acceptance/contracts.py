from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from scripts.release_acceptance.models import ReleaseEvidence, ReviewReport


def canonical_json_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def contract_digest(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return canonical_digest(value)


def schema_bytes(model: type[Any]) -> bytes:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("schema model must be a Pydantic model")
    encoded = json.dumps(model.model_json_schema(), sort_keys=True, indent=2, ensure_ascii=False)
    return (encoded + "\n").encode("utf-8")


def write_contract_schemas(repository_root: Path) -> None:
    contracts = repository_root / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "m8_release_evidence.schema.json").write_bytes(schema_bytes(ReleaseEvidence))
    (contracts / "m8_review_report.schema.json").write_bytes(schema_bytes(ReviewReport))
