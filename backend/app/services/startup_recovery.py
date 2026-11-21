"""Ao iniciar o servidor, marca como falhos os processamentos que ficaram "running".

Se o processo morre no meio (falta de memória, reinício), o arquivo de progresso e o
status do job ficavam presos em "running" e a interface parecia ainda processar.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

INTERRUPTED_MESSAGE = "Processamento interrompido"
INTERRUPTED_DETAIL = "O servidor foi reiniciado durante o processamento. Tente novamente."


def _mark_progress_file(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict) or payload.get("status") != "running":
        return False
    payload.update(
        {
            "status": "failed",
            "step": "error",
            "message": INTERRUPTED_MESSAGE,
            "detail": INTERRUPTED_DETAIL,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True


def recover_interrupted_processing(storage_root: Path, db=None) -> dict:
    processing_root = Path(storage_root) / "processing"
    marked_files = 0
    if processing_root.is_dir():
        for pattern in ("*/advanced_progress.json", "*/progress.json", "*/results/progress.json"):
            for path in processing_root.glob(pattern):
                if _mark_progress_file(path):
                    marked_files += 1

    marked_jobs = 0
    if db is not None:
        from app.models import VRSJob

        for job in db.query(VRSJob).filter(VRSJob.processing_status == "running").all():
            job.processing_status = "failed"
            marked_jobs += 1
        if marked_jobs:
            db.commit()

    if marked_files or marked_jobs:
        print(
            f"[STARTUP] processamentos interrompidos marcados como falhos: "
            f"arquivos={marked_files} jobs={marked_jobs}",
            flush=True,
        )
    return {"files": marked_files, "jobs": marked_jobs}
