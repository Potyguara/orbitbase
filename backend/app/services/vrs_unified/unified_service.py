"""Motor único de base VRS.

Usa o motor básico (que já funciona e não é alterado) para gerar as
observações virtuais corrigidas pela rede na grade nativa da RBMC (15 s), e
então adensa essas observações para os horários exatos pedidos e para o
intervalo escolhido (1, 5, 15 ou 30 s).

Adensamento: para cada satélite, o resíduo r = obs - (ρ + T) no próprio ponto
alvo varia devagar (relógios, ionosfera, ambiguidade constante). Ele é
interpolado linearmente entre as épocas de 15 s e o observável é reconstruído
como r(t) + ρ(t) + T(t), com a posição do satélite no instante t. Se faltar
o resíduo de um lado, ou houver salto (perda de ciclo), o satélite fica de fora
só naquele intervalo.
"""

from __future__ import annotations

import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.services.vrs_advanced.advanced_observation_interpolation_service import (
    _build_output_epochs_phase_dense,
    _epoch_to_iso,
)
from app.services.vrs_advanced.advanced_rinex_observation_reader_service import (
    extract_rinex_observations_for_window,
)
from app.services.vrs_advanced.advanced_rinex_writer_service import (
    write_advanced_rinex_from_seed,
)

LOCAL_TIMEZONE = "America/Belem"
SUPPORTED_STEPS = (1, 5, 10, 15, 30)
# margem de épocas nativas antes/depois da janela para interpolar as bordas
PAD_MINUTES = 1
MAX_GAP_SECONDS = 30.0


def parse_local_time(session_date: str, value: str) -> datetime:
    """Aceita HH:MM ou HH:MM:SS (hora local) e devolve datetime ingênuo local."""
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) == 2:
        text = f"{text}:00"
    elif len(parts) != 3:
        raise ValueError(f"Hora inválida: {value!r}. Use HH:MM ou HH:MM:SS.")
    return datetime.fromisoformat(f"{session_date}T{text}")


def local_to_utc(dt_local: datetime) -> datetime:
    return dt_local.replace(tzinfo=ZoneInfo(LOCAL_TIMEZONE)).astimezone(timezone.utc)


def build_target_grid(start_utc: datetime, end_utc: datetime, step: int) -> list[datetime]:
    grid = []
    t = start_utc
    while t <= end_utc:
        grid.append(t)
        t += timedelta(seconds=step)
    if grid and grid[-1] != end_utc and (end_utc - grid[-1]).total_seconds() > 0:
        grid.append(end_utc)
    return grid


def _padded_basic_window(start_local: datetime, end_local: datetime) -> tuple[str, str]:
    start = start_local.replace(second=0) - timedelta(minutes=PAD_MINUTES)
    end = end_local.replace(second=0) + timedelta(minutes=PAD_MINUTES + 1)
    return start.strftime("%H:%M"), end.strftime("%H:%M")


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, h: float) -> tuple[float, float, float]:
    a = 6378137.0
    e2 = 6.69437999014e-3
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return (
        (n + h) * math.cos(lat) * math.cos(lon),
        (n + h) * math.cos(lat) * math.sin(lon),
        (n * (1 - e2) + h) * math.sin(lat),
    )


def _basic_epochs_to_master(rinex_path: Path, target_xyz) -> dict:
    data = extract_rinex_observations_for_window(
        rinex_path,
        datetime(1980, 1, 1, tzinfo=timezone.utc),
        datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    epochs = []
    for ep in data["epochs"]:
        dt = datetime.fromisoformat(ep["epoch_utc"].replace("Z", "+00:00"))
        obs_map = {}
        for item in ep["satellites"]:
            sat = (item["satellite"] or "").strip().upper()
            if sat.isdigit():
                sat = f"G{int(sat):02d}"
            obs_map[sat] = {k: v for k, v in item["observations"].items() if v}
        epochs.append({"epoch": dt, "epoch_iso": _epoch_to_iso(dt), "obs_map": obs_map})
    epochs.sort(key=lambda e: e["epoch"])
    return {"station_code": "VRS", "xyz": target_xyz, "epochs": epochs}


def build_geometry_map(nav_path: str, start_utc: datetime, end_utc: datetime) -> dict:
    """Posições GPS (efeméride transmitida) a cada 1 s, chave ISO 'Z'."""
    from app.services.vrs_relative_correction_service import (
        build_satellite_xyz_cache_from_rinex_nav,
    )

    cache = build_satellite_xyz_cache_from_rinex_nav(
        nav_file_path=nav_path,
        start_dt=start_utc.replace(tzinfo=None),
        end_dt=end_utc.replace(tzinfo=None),
        step_seconds=1,
    )
    out = {}
    for key, sats in cache.items():
        dt = datetime.fromisoformat(key).replace(tzinfo=timezone.utc)
        out[_epoch_to_iso(dt)] = {
            s: {"x": v["x"], "y": v["y"], "z": v["z"]} for s, v in sats.items()
        }
    return out


def densify_basic_output(
    basic_rinex: Path,
    target_xyz: tuple[float, float, float],
    geometry_map: dict,
    target_grid: list[datetime],
    min_satellites_per_epoch: int = 4,
) -> tuple[list[dict], int]:
    master = _basic_epochs_to_master(basic_rinex, target_xyz)
    return _build_output_epochs_phase_dense(
        master_base=master,
        geometry_epoch_map=geometry_map,
        target_xyz=target_xyz,
        time_grid=target_grid,
        max_gap_seconds=MAX_GAP_SECONDS,
        min_satellites_per_epoch=min_satellites_per_epoch,
    )


def _first_nav_file(station_codes: list[str], session_date: str) -> str | None:
    from app.services.rbmc_observation_sync_service import ensure_rbmc_station_files

    for code in station_codes:
        try:
            state = ensure_rbmc_station_files(code, session_date) or {}
        except Exception:
            continue
        nav = state.get("best_navigation_file")
        if nav and Path(nav).exists():
            return str(nav)
    return None


def _write_progress(results_dir: Path, percent: int, step: str, message: str, status="running"):
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "progress.json").write_text(
            json.dumps(
                {"percent": percent, "step": step, "message": message, "status": status,
                 "updated_at": datetime.utcnow().isoformat() + "Z"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def unified_root(storage_root: Path, job_id: Any) -> Path:
    return storage_root / "processing" / f"job_{job_id}_unified"


def process_unified_vrs(
    job,
    storage_root: Path,
    step_seconds: int = 1,
    profile_config: dict | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict:
    """Roda o motor básico com margem e adensa para a janela exata pedida."""
    from app.services.vrs_processing_service import process_real_vrs

    if int(step_seconds) not in SUPPORTED_STEPS:
        raise ValueError(f"Intervalo não suportado: {step_seconds}s. Use {SUPPORTED_STEPS}.")
    step_seconds = int(step_seconds)

    start_local = parse_local_time(job.session_date, start_time or job.start_time)
    end_local = parse_local_time(job.session_date, end_time or job.end_time)
    if end_local <= start_local:
        raise ValueError("A hora final deve ser maior que a hora inicial.")
    start_utc, end_utc = local_to_utc(start_local), local_to_utc(end_local)

    out_root = unified_root(storage_root, job.id)
    results_dir = out_root / "results"
    _write_progress(results_dir, 5, "basic", "Gerando observações de rede (motor básico)")

    pad_start, pad_end = _padded_basic_window(start_local, end_local)
    padded_minutes = int(
        (parse_local_time(job.session_date, pad_end) - parse_local_time(job.session_date, pad_start)).total_seconds() // 60
    )
    proxy = SimpleNamespace(
        id=f"{job.id}_unified",
        name=getattr(job, "name", None) or "BASE",
        session_date=job.session_date,
        start_time=pad_start,
        end_time=pad_end,
        occupation_minutes=padded_minutes,
        requested_minutes=padded_minutes,
        target_lat=job.target_lat,
        target_lon=job.target_lon,
        target_height=job.target_height,
        selected_station_codes_json=job.selected_station_codes_json,
        network_validation_status="completed",
        network_validation_summary=getattr(job, "network_validation_summary", "") or "",
        output_rinex_name=getattr(job, "output_rinex_name", None) or "BASE",
        output_marker_name=getattr(job, "output_marker_name", None) or "BASE",
    )

    basic_result = process_real_vrs(proxy, profile_config=profile_config)
    basic_rinex = Path(basic_result["base_ppp_path"])
    basic_summary = json.dumps(basic_result, default=str)

    _write_progress(results_dir, 70, "geometry", "Calculando geometria dos satélites")
    target_xyz = _geodetic_to_ecef(job.target_lat, job.target_lon, job.target_height)
    codes = json.loads(job.selected_station_codes_json or "[]")
    nav_path = _first_nav_file(codes, job.session_date)
    if not nav_path:
        raise ValueError("Nenhum arquivo de navegação disponível nas bases selecionadas.")

    geom_start = start_utc - timedelta(minutes=PAD_MINUTES + 1)
    geom_end = end_utc + timedelta(minutes=PAD_MINUTES + 1)
    geometry_map = build_geometry_map(nav_path, geom_start, geom_end)

    _write_progress(results_dir, 80, "densify", f"Gerando épocas a {step_seconds} s")
    grid = build_target_grid(start_utc, end_utc, step_seconds)
    epochs, skipped = densify_basic_output(basic_rinex, target_xyz, geometry_map, grid)
    if not epochs:
        raise ValueError("Nenhuma época válida após o adensamento.")

    marker = (getattr(job, "output_marker_name", None) or "BASE")[:60]
    out_name = f"{marker[:4].upper():<4}".replace(" ", "X")
    doy = start_utc.timetuple().tm_yday
    rinex_name = f"{out_name}{doy:03d}0.{start_utc.year % 100:02d}o"
    output_path = write_advanced_rinex_from_seed(
        output_path=str(results_dir / rinex_name),
        advanced_seed={"epochs": epochs},
        marker_name=marker,
        receiver_brand="ORBITBASE",
        receiver_model="ORBITBASE VRS",
        receiver_version="1.0",
        antenna_model="VIRTUAL",
        approx_position_xyz=target_xyz,
        antenna_delta_hen=(0.0, 0.0, 0.0),
        interval_seconds=float(step_seconds),
        require_phase_observables=True,
        output_systems=["G"],
        forced_obs_types=["C1", "P1", "L1", "C2", "P2", "L2"],
    )

    summary = {
        "engine": "unified_v1",
        "step_seconds": step_seconds,
        "window_local": [start_local.isoformat(), end_local.isoformat()],
        "window_utc": [_epoch_to_iso(start_utc), _epoch_to_iso(end_utc)],
        "requested_epochs": len(grid),
        "epochs_written": len(epochs),
        "epochs_skipped": skipped,
        "basic_fallback_to_reference_base": "reference_base_fallback" in basic_summary,
        "basic_job_root": basic_result.get("job_root"),
        "rinex_path": output_path,
        "nav_path": nav_path,
    }
    (results_dir / "unified_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # o motor básico gravou numa pasta própria do proxy; guardamos só o RINEX dele
    basic_root = Path(basic_result.get("job_root") or "")
    if basic_root.exists():
        shutil.copy2(basic_rinex, results_dir / "network_15s.obs")
        shutil.rmtree(basic_root, ignore_errors=True)

    _write_progress(results_dir, 100, "done", "Base gerada", status="completed")
    return summary
