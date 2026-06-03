from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json


def write_advanced_progress(
    job_root: Path,
    percent: int,
    step: str,
    message: str,
    detail: str | None = None,
    status: str = "running",
) -> str:
    progress_path = Path(job_root) / "advanced_progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "percent": int(percent),
        "step": step,
        "message": message,
        "detail": detail or "",
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    tmp_path = progress_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(progress_path)

    return str(progress_path)
