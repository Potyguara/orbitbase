from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_current_user, get_db
from app.models import User, UserAdvancedCredentials
from app.services.gnss_downloads_service import (
    MAX_STATIONS,
    date_range,
    read_status,
    start_download_task,
    tasks_root,
)

router = APIRouter(prefix="/downloads", tags=["Downloads GNSS"])

STORAGE_ROOT = Path(__file__).resolve().parents[2] / "storage"
_TASK_ID = re.compile(r"^[0-9a-f]{16}$")
_STATION = re.compile(r"^[A-Z0-9]{4}$")


class DownloadRequest(BaseModel):
    start_date: str
    end_date: str | None = None
    ephemeris: bool = True
    include_rinex3: bool = False
    stations: list[str] = Field(default_factory=list)
    convert_hatanaka: bool = True


def _earthdata(db: Session, user: User) -> tuple[str | None, str | None]:
    from app.routers.vrs_advanced import _decode_optional_base64

    record = (
        db.query(UserAdvancedCredentials)
        .filter(UserAdvancedCredentials.user_id == user.id)
        .first()
    )
    username = record.earthdata_username.strip() if record and record.earthdata_username else None
    password = _decode_optional_base64(record.earthdata_password) if record else None
    if not (username and password) and settings.earthdata_username and settings.earthdata_password:
        return settings.earthdata_username.strip(), settings.earthdata_password
    return username, password


def _task_dir(user: User, task_id: str) -> Path:
    if not _TASK_ID.match(task_id or ""):
        raise HTTPException(status_code=404, detail="Download não encontrado.")
    path = tasks_root(STORAGE_ROOT, user.id) / task_id
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Download não encontrado ou expirado.")
    return path


@router.post("")
def create_download(
    payload: DownloadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        days = date_range(payload.start_date, payload.end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stations = list(dict.fromkeys(s.strip().upper() for s in payload.stations if s.strip()))
    bad = [s for s in stations if not _STATION.match(s)]
    if bad:
        raise HTTPException(status_code=400, detail=f"Código de estação inválido: {', '.join(bad)}")
    if len(stations) > MAX_STATIONS:
        raise HTTPException(status_code=400, detail=f"Escolha no máximo {MAX_STATIONS} estações.")
    if not payload.ephemeris and not stations:
        raise HTTPException(status_code=400, detail="Marque as efemérides ou escolha ao menos uma estação RBMC.")
    if len(days) * len(stations) > 62:
        raise HTTPException(
            status_code=400,
            detail="Pedido grande demais: reduza o período ou o número de estações (até 62 arquivos RBMC por vez).",
        )

    user, password = _earthdata(db, current_user)
    task_id = start_download_task(
        STORAGE_ROOT, current_user.id, days,
        ephemeris=payload.ephemeris,
        include_rinex3=payload.include_rinex3,
        stations=stations,
        convert_hatanaka=payload.convert_hatanaka,
        earthdata_username=user,
        earthdata_password=password,
    )
    return {"task_id": task_id, "days": len(days), "stations": stations}


@router.get("/{task_id}")
def get_download_status(task_id: str, current_user: User = Depends(get_current_user)):
    status = read_status(_task_dir(current_user, task_id))
    if status is None:
        raise HTTPException(status_code=404, detail="Download não encontrado.")
    return status


@router.get("/{task_id}/file")
def get_download_file(task_id: str, current_user: User = Depends(get_current_user)):
    task_dir = _task_dir(current_user, task_id)
    status = read_status(task_dir) or {}
    name = status.get("file_name")
    if status.get("status") != "done" or not name:
        raise HTTPException(status_code=409, detail="O pacote ainda não está pronto.")
    path = task_dir / Path(name).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo expirado. Gere o download de novo.")
    return FileResponse(path, media_type="application/zip", filename=path.name)
