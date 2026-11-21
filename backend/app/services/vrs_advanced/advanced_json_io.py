from __future__ import annotations

import json
import os
import resource
from datetime import datetime
from pathlib import Path


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dump_json_file(path, payload, indent: int | None = 2) -> str:
    """Grava JSON em streaming (json.dump), sem montar a string inteira na memória."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, indent=indent, ensure_ascii=False, default=_json_default)
    return str(path)


def debug_seed_dumps_enabled() -> bool:
    """Seeds completos (pre/post/final) só são gravados com ORBITBASE_ADV_DUMP_SEEDS=1.

    Com passo de 1 s em várias horas eles chegam a centenas de MB cada.
    """
    return os.environ.get("ORBITBASE_ADV_DUMP_SEEDS", "").strip().lower() in {"1", "true", "yes"}


def max_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
