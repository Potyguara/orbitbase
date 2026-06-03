from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import GnssStation, User
from app.schemas import GnssStationRead, RbmcSyncResponse
from app.services.rbmc_sync_service import sync_rbmc_stations

router = APIRouter(prefix="/gnss-stations", tags=["GNSS Stations"])


@router.get("", response_model=list[GnssStationRead])
def list_gnss_stations(
    state: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    warning_only: bool = Query(default=False),
    rinex_1s_only: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(GnssStation)

    if state:
        query = query.filter(GnssStation.state == state.strip().upper())

    if active_only:
        query = query.filter(GnssStation.is_active.is_(True))

    if warning_only:
        query = query.filter(GnssStation.is_in_warning.is_(True))

    if rinex_1s_only:
        query = query.filter(GnssStation.supports_rinex_1s.is_(True))

    stations = (
        query.order_by(
            GnssStation.state.asc(),
            GnssStation.station_code.asc(),
        )
        .limit(limit)
        .all()
    )

    return stations


@router.get("/states", response_model=list[str])
def list_gnss_station_states(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(GnssStation.state)
        .filter(GnssStation.state.isnot(None))
        .distinct()
        .order_by(GnssStation.state.asc())
        .all()
    )

    return [row[0] for row in rows if row[0]]


@router.get("/{station_code}", response_model=GnssStationRead)
def get_gnss_station(
    station_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    code = station_code.strip().upper()

    station = db.query(GnssStation).filter(GnssStation.station_code == code).first()

    if not station:
        raise HTTPException(status_code=404, detail="Estação GNSS não encontrada.")

    return station


@router.post("/sync", response_model=RbmcSyncResponse)
def sync_gnss_stations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        storage_root = str(Path(__file__).resolve().parents[2] / "storage")

        result = sync_rbmc_stations(
            db=db,
            storage_root=storage_root,
        )

        return RbmcSyncResponse(**result)

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Falha ao sincronizar estações RBMC: {exc}",
        )
