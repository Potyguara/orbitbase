from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User, VRSJob
from app.services.vrs_profile_service import resolve_effective_profile_for_job
from app.services.vrs_unified.unified_service import (
    SUPPORTED_STEPS,
    process_unified_vrs,
    unified_root,
)

router = APIRouter(prefix="/jobs", tags=["VRS Unified"])

STORAGE_ROOT = Path(__file__).resolve().parents[2] / "storage"


class UnifiedProcessPayload(BaseModel):
    start_time: str | None = None  # HH:MM ou HH:MM:SS (hora local)
    end_time: str | None = None
    step_seconds: int = Field(default=1, ge=1, le=30)


def _get_job(db: Session, job_id: int, user: User) -> VRSJob:
    job = db.query(VRSJob).filter(VRSJob.id == job_id, VRSJob.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return job


@router.post("/{job_id}/process-unified")
def process_job_unified(
    job_id: int,
    payload: UnifiedProcessPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = _get_job(db, job_id, current_user)
    if payload.step_seconds not in SUPPORTED_STEPS:
        raise HTTPException(status_code=400, detail=f"Intervalo deve ser um de {SUPPORTED_STEPS} s.")
    try:
        profile = resolve_effective_profile_for_job(db=db, user_id=current_user.id, job=job)
        summary = process_unified_vrs(
            job,
            storage_root=STORAGE_ROOT,
            step_seconds=payload.step_seconds,
            profile_config=profile["config"],
            start_time=payload.start_time,
            end_time=payload.end_time,
        )
    except Exception as exc:  # noqa: BLE001
        results = unified_root(STORAGE_ROOT, job_id) / "results"
        try:
            results.mkdir(parents=True, exist_ok=True)
            (results / "progress.json").write_text(
                json.dumps({"percent": 100, "step": "error", "status": "failed",
                            "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")
    return {"ok": True, "job_id": job_id, "result": summary}


@router.get("/{job_id}/progress-unified")
def progress_unified(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_job(db, job_id, current_user)
    path = unified_root(STORAGE_ROOT, job_id) / "results" / "progress.json"
    if not path.exists():
        return {"percent": 0, "step": "idle", "status": "idle", "message": "Aguardando processamento."}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"percent": 0, "step": "idle", "status": "running", "message": "Progresso indisponível."}


def build_unified_export_zip(results: Path) -> tuple[bytes, str] | None:
    summary_path = results / "unified_summary.json"
    if not summary_path.is_file():
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rinex = Path(summary.get("rinex_path") or "")
    if not rinex.is_file():
        rinex = results / Path(summary.get("rinex_path") or "x").name
    if not rinex.is_file():
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(rinex, arcname=rinex.name)
        zf.write(summary_path, arcname="relatorios/resumo_processamento.json")
    return buffer.getvalue(), rinex.name


@router.get("/{job_id}/export-unified")
def export_unified(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_job(db, job_id, current_user)
    built = build_unified_export_zip(unified_root(STORAGE_ROOT, job_id) / "results")
    if built is None:
        raise HTTPException(status_code=404, detail="Processe o job no motor único primeiro.")
    payload, _ = built
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="job_{job_id}_base.zip"'},
    )
