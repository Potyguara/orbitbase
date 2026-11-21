from __future__ import annotations

from pathlib import Path
from datetime import date
import io
import zipfile
import json
import base64

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
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

        if not (earthdata_username and earthdata_password):
            if settings.earthdata_username and settings.earthdata_password:
                earthdata_username = settings.earthdata_username.strip()
                earthdata_password = settings.earthdata_password

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


ADVANCED_EXPORT_FILES = (
    "advanced_debug_report.json",
    "advanced_ppp_filter_diagnostics.json",
)


def _advanced_job_root(job_id: int) -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "storage"
        / "processing"
        / f"job_{job_id}_advanced"
    )


def advanced_rinex_export_name(marker: str | None, session_date: str | None) -> str:
    """Nome RINEX 2 curto: SSSSDDD0.YYo (ex.: ADVB2320.26o)."""
    code = "".join(ch for ch in (marker or "") if ch.isalnum()).upper()[:4] or "ADVB"
    code = code.ljust(4, "0")
    try:
        d = date.fromisoformat(str(session_date or "")[:10])
        return f"{code}{d.timetuple().tm_yday:03d}0.{d.year % 100:02d}o"
    except ValueError:
        return f"{code}.obs"


def build_advanced_export_zip(job_root: Path, rinex_name: str) -> bytes | None:
    results = job_root / "results"
    obs_path = results / "ADVANCED_BASE.obs"
    if not obs_path.is_file():
        return None

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(obs_path, arcname=rinex_name)
        for name in ADVANCED_EXPORT_FILES:
            path = results / name
            if path.is_file():
                zf.write(path, arcname=f"relatorios/{name}")
    return buffer.getvalue()


@router.get("/{job_id}/export-advanced")
def export_job_advanced(
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

    rinex_name = advanced_rinex_export_name(
        getattr(job, "output_marker_name", None), getattr(job, "session_date", None)
    )
    payload = build_advanced_export_zip(_advanced_job_root(job_id), rinex_name)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail="Nenhum arquivo do modo avançado para este job. Processe o job primeiro.",
        )

    filename = f"job_{job_id}_avancado.zip"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
