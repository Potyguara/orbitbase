from __future__ import annotations

from pathlib import Path
import json
import base64

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User, VRSJob, UserAdvancedCredentials
from app.services.vrs_advanced.advanced_ephemeris_service import (
    save_manual_sp3_uploads,
)
from app.services.vrs_advanced.advanced_pipeline_service import (
    process_real_vrs_advanced,
)

router = APIRouter(prefix="/jobs", tags=["VRS Advanced"])


class AdvancedConfigPayload(BaseModel):
    target_step_seconds: int = Field(default=1, ge=1)
    use_precise_ephemeris: bool = True
    use_precise_clock: bool = False
    interpolate_code_observables: bool = True
    interpolate_phase_observables: bool = False
    receiver_brand: str = "ORBITBASE"
    receiver_model: str = "VIRTUAL_GNSS_ADV"
    receiver_version: str = "0.1-alpha"
    antenna_model: str = "VIRTUAL_ADV_ANT"


class AdvancedProcessPayload(BaseModel):
    session_date: str
    start_time: str
    end_time: str
    advanced_config: AdvancedConfigPayload


def _decode_optional_base64(value: str | None) -> str | None:
    if not value:
        return None

    try:
        return base64.b64decode(value.encode("utf-8")).decode("utf-8")
    except Exception:
        return value


@router.post("/{job_id}/advanced-ephemeris-upload")
async def upload_job_advanced_ephemeris(
    job_id: int,
    session_date: str = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    if not files:
        raise HTTPException(
            status_code=400,
            detail="Nenhum arquivo de efeméride foi enviado.",
        )

    try:
        storage_root = Path(__file__).resolve().parents[2] / "storage"

        uploads: list[tuple[str, bytes]] = []
        for file in files:
            if not file.filename:
                continue
            content = await file.read()
            uploads.append((file.filename, content))

        if not uploads:
            raise HTTPException(
                status_code=400,
                detail="Os arquivos enviados estão vazios ou sem nome válido.",
            )

        result = save_manual_sp3_uploads(
            session_date=session_date,
            storage_root=storage_root,
            uploads=uploads,
        )

        job.session_date = session_date
        db.add(job)
        db.commit()
        db.refresh(job)

        return {
            "ok": True,
            "job_id": job.id,
            "message": (
                f"Efemérides manuais processadas com sucesso. "
                f"{result.get('valid_sp3_count', 0)} arquivo(s) SP3 válido(s)."
            ),
            "result": result,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{type(exc).__name__}: {exc}",
        )


@router.post("/{job_id}/process-advanced")
def process_job_advanced(
    job_id: int,
    payload: AdvancedProcessPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    print(
        f"[ADV-ROUTE] job encontrado id={job.id} user_id={current_user.id}",
        flush=True,
    )

    try:
        job.session_date = payload.session_date
        job.start_time = payload.start_time
        job.end_time = payload.end_time

        db.add(job)
        db.commit()
        db.refresh(job)

        print(
            f"[ADV-ROUTE] janela recebida: "
            f"{payload.session_date} {payload.start_time} -> {payload.end_time}",
            flush=True,
        )

        credentials = (
            db.query(UserAdvancedCredentials)
            .filter(UserAdvancedCredentials.user_id == current_user.id)
            .first()
        )

        earthdata_username = (
            credentials.earthdata_username.strip()
            if credentials and credentials.earthdata_username
            else None
        )

        earthdata_password_raw = (
            credentials.earthdata_password
            if credentials and credentials.earthdata_password
            else None
        )

        earthdata_password = _decode_optional_base64(earthdata_password_raw)

        print(
            f"[ADV-ROUTE] credentials found={bool(credentials)} "
            f"user={earthdata_username} has_password={bool(earthdata_password)}",
            flush=True,
        )
        print("[ADV-ROUTE] chamando process_real_vrs_advanced...", flush=True)

        result = process_real_vrs_advanced(
            job=job,
            profile_config=payload.advanced_config.model_dump(),
            earthdata_username=earthdata_username,
            earthdata_password=earthdata_password,
        )

        return {
            "ok": True,
            "job_id": job.id,
            "message": "Processamento avançado concluído.",
            "result": result,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{type(exc).__name__}: {exc}",
        )


@router.get("/{job_id}/progress-advanced")
def get_job_advanced_progress(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    progress_path = (
        Path(__file__).resolve().parents[2]
        / "storage"
        / "processing"
        / f"job_{job_id}_advanced"
        / "advanced_progress.json"
    )

    if not progress_path.exists():
        return {
            "job_id": job_id,
            "percent": 0,
            "step": "idle",
            "message": "Aguardando processamento avançado.",
            "detail": "",
            "status": "idle",
        }

    try:
        return json.loads(progress_path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "job_id": job_id,
            "percent": 0,
            "step": "idle",
            "message": "Arquivo de progresso temporariamente indisponível.",
            "detail": "",
            "status": "running",
        }
