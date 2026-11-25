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
    receiver_type: str | None = Field(default=None, max_length=40)
    receiver_number: str | None = Field(default=None, max_length=40)
    receiver_version: str | None = Field(default=None, max_length=40)
    receiver_manufacturer: str | None = Field(default=None, max_length=40)
    antenna_type: str | None = Field(default=None, max_length=40)
    antenna_number: str | None = Field(default=None, max_length=40)


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
            receiver_type=payload.receiver_type,
            receiver_number=payload.receiver_number,
            receiver_version=payload.receiver_version,
            receiver_manufacturer=payload.receiver_manufacturer,
            antenna_type=payload.antenna_type,
            antenna_number=payload.antenna_number,
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
        for nav in summary.get("navigation_files") or []:
            nav_path = Path(nav)
            if not nav_path.is_file():
                nav_path = results / nav_path.name
            if nav_path.is_file():
                zf.write(nav_path, arcname=nav_path.name)
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


# ---------------------------------------------------------------------------
# Interface única: status e downloads separados (RINEX / RBMC / EFEMÉRIDES)
# ---------------------------------------------------------------------------


def _read_summary(job_id: int) -> dict | None:
    path = unified_root(STORAGE_ROOT, job_id) / "results" / "unified_summary.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _local_file(results: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_file():
        return path
    alt = results / path.name
    return alt if alt.is_file() else None


@router.get("/unified/status")
def unified_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resumo do motor único para todos os jobs do usuário (para a tabela)."""
    jobs = db.query(VRSJob.id).filter(VRSJob.user_id == current_user.id).all()
    out = {}
    for (job_id,) in jobs:
        summary = _read_summary(job_id)
        results = unified_root(STORAGE_ROOT, job_id) / "results"
        progress = None
        try:
            progress = json.loads((results / "progress.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        ready = bool(summary and _local_file(results, summary.get("rinex_path")))
        out[str(job_id)] = {
            "ready": ready,
            "status": (progress or {}).get("status"),
            "percent": (progress or {}).get("percent"),
            "message": (progress or {}).get("message"),
            "step_seconds": (summary or {}).get("step_seconds"),
            "epochs_written": (summary or {}).get("epochs_written"),
            "master_station": (summary or {}).get("master_station"),
            "master_distance_km": (summary or {}).get("master_distance_km"),
            "window_local": (summary or {}).get("window_local"),
            "rinex_name": Path((summary or {}).get("rinex_path") or "").name or None,
        }
    return {"jobs": out}


def _zip_response(files: list[tuple[Path, str]], filename: str) -> StreamingResponse:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname=arcname)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{job_id}/download/rinex")
def download_rinex(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_job(db, job_id, current_user)
    results = unified_root(STORAGE_ROOT, job_id) / "results"
    summary = _read_summary(job_id)
    rinex = _local_file(results, (summary or {}).get("rinex_path"))
    if rinex is None:
        raise HTTPException(status_code=404, detail="Processe o job primeiro.")
    packed = zipped_rinex(rinex)
    return StreamingResponse(
        open(packed, "rb"),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{packed.name}"',
            "Content-Length": str(packed.stat().st_size),
        },
    )


def zipped_rinex(rinex: Path) -> Path:
    """Pacote como o de um receptor: SSSSDDD0.YYo + .YYn (GPS) + .YYg (GLONASS).

    RINEX de 1 s passa de 60 MB; compactado cai ~5-8x. Gerado uma vez por
    processamento e refeito se algum dos arquivos mudar."""
    stem = rinex.name[:-1]  # "BASE2320.26"
    members = [rinex] + [
        rinex.with_name(stem + ext) for ext in ("n", "g")
        if rinex.with_name(stem + ext).exists()
    ]
    packed = rinex.with_name(rinex.name.split(".")[0] + ".zip")
    newest = max(m.stat().st_mtime for m in members)
    if not packed.exists() or packed.stat().st_mtime < newest:
        tmp = packed.with_name(packed.name + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for m in members:
                zf.write(m, arcname=m.name)
        tmp.replace(packed)
    return packed


def collect_ephemeris_files(results: Path, summary: dict) -> list[tuple[Path, str]]:
    files = []
    for raw in summary.get("navigation_files") or []:
        path = _local_file(results, raw)
        if path:
            files.append((path, path.name))
    if not files:
        # processamentos antigos: usa a navegação original da base mestra
        for raw in summary.get("nav_paths") or [summary.get("nav_path")]:
            path = _local_file(results, raw)
            if path:
                files.append((path, path.name))
    return files


@router.get("/{job_id}/download/ephemeris")
def download_ephemeris(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_job(db, job_id, current_user)
    results = unified_root(STORAGE_ROOT, job_id) / "results"
    summary = _read_summary(job_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Processe o job primeiro.")
    files = collect_ephemeris_files(results, summary)
    if not files:
        raise HTTPException(status_code=404, detail="Nenhum arquivo de efemérides encontrado.")
    return _zip_response(files, f"job_{job_id}_efemerides.zip")


def collect_rbmc_files(station_codes: list[str], session_date: str) -> list[tuple[Path, str]]:
    from app.services.rbmc_observation_sync_service import _find_station_files

    files: list[tuple[Path, str]] = []
    for code in station_codes:
        try:
            state = _find_station_files(code, session_date)
        except Exception:
            continue
        for key in ("best_observation_file", "best_navigation_file"):
            raw = state.get(key)
            if raw and Path(raw).is_file():
                files.append((Path(raw), f"{code.upper()}/{Path(raw).name}"))
    return files


@router.get("/{job_id}/download/rbmc")
def download_rbmc(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = _get_job(db, job_id, current_user)
    summary = _read_summary(job_id) or {}
    used = [c.upper() for c in summary.get("stations_used") or []]
    if summary.get("master_station") and summary["master_station"].upper() not in used:
        used.insert(0, summary["master_station"].upper())
    try:
        selected = [c.upper() for c in json.loads(job.selected_station_codes_json or "[]")]
    except Exception:
        selected = []
    codes = used + [c for c in selected if c not in used]
    files = collect_rbmc_files(codes, job.session_date)
    if not files:
        raise HTTPException(status_code=404, detail="Nenhum arquivo RBMC encontrado para este job.")
    return _zip_response(files, f"job_{job_id}_rbmc.zip")
