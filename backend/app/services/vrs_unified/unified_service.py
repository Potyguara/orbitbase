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
import re
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
# O motor básico filtra satélites por arco mínimo e blocos contínuos; para que
# qualquer tempo de ocupação funcione (inclusive 1-2 min), ele roda sempre numa
# janela de pelo menos isto, centrada na pedida, e o resultado é recortado.
MIN_BASIC_WINDOW_MINUTES = 60
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
    missing = MIN_BASIC_WINDOW_MINUTES - (end - start).total_seconds() / 60
    if missing > 0:
        half = timedelta(minutes=math.ceil(missing / 2))
        start, end = start - half, end + half
    day_start = start_local.replace(hour=0, minute=0, second=0)
    day_end = start_local.replace(hour=23, minute=59, second=0)
    if start < day_start:
        end, start = end + (day_start - start), day_start
    if end > day_end:
        start, end = start - (end - day_end), day_end
    start = max(start, day_start)
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
            sat = (item["satellite"] or "").strip().upper().replace(" ", "0")
            if sat.isdigit():
                sat = f"G{int(sat):02d}"
            elif len(sat) == 2 and sat[0].isalpha():
                sat = f"{sat[0]}0{sat[1]}"
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
    by_time = {
        datetime.fromisoformat(key).replace(tzinfo=timezone.utc): sats
        for key, sats in cache.items()
    }
    one = timedelta(seconds=1)
    out = {}
    for dt, sats in by_time.items():
        prev_sats = by_time.get(dt - one) or {}
        next_sats = by_time.get(dt + one) or {}
        epoch = {}
        for s, v in sats.items():
            item = {"x": v["x"], "y": v["y"], "z": v["z"]}
            # velocidade por diferença finita: sem ela o tempo de propagação
            # (~70 ms, ~270 m de movimento do satélite) ficava fora do cálculo
            # da distância e gerava viés proporcional à linha de base
            a, b, span = prev_sats.get(s), next_sats.get(s), 2.0
            if a is None:
                a, span = v, 1.0
            if b is None:
                b, span = v, (1.0 if span == 2.0 else 0.0)
            if span > 0:
                for axis in ("x", "y", "z"):
                    item["v" + axis] = (b[axis] - a[axis]) / span
            epoch[s] = item
        out[_epoch_to_iso(dt)] = epoch
    return out


def _glonass_nav_files(nav_paths: list[str]) -> list[str]:
    out = []
    for p in nav_paths:
        name = Path(p).name.lower()
        if re.search(r"\.\d{2}g(\.gz)?$", name) or re.search(r"_(rn|mn)\.rnx(\.gz)?$", name):
            out.append(p)
    return out


def add_glonass_geometry(geometry: dict, nav_paths: list[str], start_utc: datetime,
                         end_utc: datetime) -> dict[str, int]:
    """Acrescenta posições GLONASS (1 s) ao mapa de geometria; devolve os canais."""
    from app.services.vrs_unified.glonass_nav import (
        glonass_channels,
        glonass_positions,
        merge_glonass_nav,
    )

    files = _glonass_nav_files(nav_paths)
    if not files:
        return {}
    records = merge_glonass_nav(files)
    channels = glonass_channels(records)
    if not channels:
        return {}
    start = start_utc.replace(tzinfo=None, microsecond=0)
    times = []
    t = start
    while t <= end_utc.replace(tzinfo=None):
        times.append(t)
        t += timedelta(seconds=1)
    positions = glonass_positions({s: r for s, r in records.items() if s in channels}, times)
    for t, sats in positions.items():
        if not sats:
            continue
        key = _epoch_to_iso(t.replace(tzinfo=timezone.utc))
        geometry.setdefault(key, {}).update(sats)
    return channels


def _wavelength_selector(channels: dict[str, int]):
    from app.services.vrs_advanced.advanced_observation_interpolation_service import (
        _gps_wavelength,
    )
    from app.services.vrs_unified.glonass_nav import glonass_wavelength

    def for_sat(sat: str):
        if sat.startswith("G"):
            return _gps_wavelength
        if sat.startswith("R") and sat in channels:
            k = channels[sat]
            return lambda code: glonass_wavelength(code, k)
        return None

    return for_sat


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


def _station_xyz_map(codes: list[str]) -> dict[str, tuple[float, float, float]]:
    from app.db import SessionLocal
    from app.models import GnssStation

    db = SessionLocal()
    try:
        rows = db.query(GnssStation).filter(GnssStation.station_code.in_(codes)).all()
        return {
            r.station_code.upper(): (r.x, r.y, r.z)
            for r in rows
            if r.x is not None and r.y is not None and r.z is not None
        }
    finally:
        db.close()


def _load_station_epochs(code: str, session_date: str, start_utc: datetime, end_utc: datetime):
    """Lê as observações reais da estação RBMC na janela (com margem)."""
    from app.services.hatanaka_service import convert_hatanaka_to_rinex
    from app.services.rbmc_observation_sync_service import ensure_rbmc_station_files

    state = ensure_rbmc_station_files(code, session_date) or {}
    obs = state.get("best_observation_file")
    if not obs:
        return None
    path = Path(convert_hatanaka_to_rinex(str(obs)))
    data = extract_rinex_observations_for_window(path, start_utc, end_utc)
    data["nav_path"] = state.get("best_navigation_file")
    data["nav_paths"] = station_navigation_files(state)
    return data


def station_navigation_files(state: dict) -> list[str]:
    """Navegação GPS do sincronizador + GLONASS (.yyg / _RN) da mesma pasta.

    O sincronizador RBMC só lista navegação GPS; os arquivos GLONASS vêm no
    mesmo pacote e ficam ao lado do .yyn.
    """
    paths = [str(x) for x in (state.get("navigation_files") or [])]
    folders = {Path(p).parent for p in paths}
    obs = state.get("best_observation_file")
    if obs:
        folders.add(Path(str(obs)).parent)
    for folder in folders:
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if f.is_file() and str(f) not in paths and _glonass_nav_files([str(f)]):
                paths.append(str(f))
    return paths


def _master_from_reader(data: dict, marker_xyz, code: str) -> dict:
    from app.services.vrs_advanced.advanced_observation_interpolation_service import (
        _apply_antenna_delta,
    )

    epochs = []
    for ep in data["epochs"]:
        dt = datetime.fromisoformat(ep["epoch_utc"].replace("Z", "+00:00"))
        obs_map = {}
        for item in ep["satellites"]:
            sat = (item["satellite"] or "").strip().upper().replace(" ", "0")
            if sat.isdigit():
                sat = f"G{int(sat):02d}"
            elif len(sat) == 2 and sat[0].isalpha():
                sat = f"{sat[0]}0{sat[1]}"
            obs_map[sat] = {k: v for k, v in item["observations"].items() if v}
        epochs.append({"epoch": dt, "epoch_iso": _epoch_to_iso(dt), "obs_map": obs_map})
    epochs.sort(key=lambda e: e["epoch"])
    xyz = _apply_antenna_delta(marker_xyz, data.get("antenna_delta_hen"))
    return {"station_code": code, "xyz": xyz, "epochs": epochs}


def _dual_phase_epoch_ratio(master: dict) -> float:
    from app.services.vrs_advanced.advanced_observation_interpolation_service import (
        _obs_has_dual_phase_gps,
    )

    eps = master.get("epochs") or []
    if not eps:
        return 0.0
    good = sum(
        1
        for ep in eps
        if sum(1 for s, r in ep["obs_map"].items() if s.startswith("G") and _obs_has_dual_phase_gps(r)) >= 4
    )
    return good / len(eps)


def build_master_transport_epochs(
    station_codes: list[str],
    session_date: str,
    target_xyz: tuple[float, float, float],
    start_utc: datetime,
    end_utc: datetime,
    step_seconds: int,
    min_quality_ratio: float = 0.8,
    use_glonass: bool = True,
) -> tuple[list[dict], int, dict]:
    """Transporte geométrico da base mais próxima com dados bons na janela.

    Física: obs_alvo(t) = [obs_mestra - (ρ + T)(mestra)](t interpolado) + (ρ + T)(alvo, t),
    com ρ corrigido de tempo de propagação e rotação da Terra, T troposfera
    padrão e a mestra no ARP (ANTENNA: DELTA H/E/N do cabeçalho).
    """
    coords = _station_xyz_map([c.upper() for c in station_codes])
    ranked = sorted(
        (math.dist(xyz, target_xyz), code) for code, xyz in coords.items()
    )
    margin = timedelta(minutes=PAD_MINUTES + 1)
    tried = []
    for dist_m, code in ranked:
        try:
            data = _load_station_epochs(code, session_date, start_utc - margin, end_utc + margin)
        except Exception as exc:  # noqa: BLE001 - estação sem arquivo no dia: tenta a próxima
            tried.append({"station": code, "dist_km": round(dist_m / 1000, 1),
                          "reason": f"sem arquivo RBMC no dia ({type(exc).__name__})"})
            continue
        if not data or not data.get("nav_path"):
            tried.append({"station": code, "dist_km": round(dist_m / 1000, 1), "reason": "sem dados"})
            continue
        master = _master_from_reader(data, coords[code], code)
        ratio = _dual_phase_epoch_ratio(master)
        if ratio < min_quality_ratio:
            tried.append({"station": code, "dist_km": round(dist_m / 1000, 1), "reason": f"qualidade {ratio:.2f}"})
            continue
        geometry = build_geometry_map(str(data["nav_path"]), start_utc - margin, end_utc + margin)
        glonass = {"satellites": 0}
        wavelength_for = None
        systems: tuple[str, ...] = ("G",)
        if use_glonass:
            channels = add_glonass_geometry(
                geometry, data.get("nav_paths") or [], start_utc - margin, end_utc + margin
            )
            if channels:
                systems = ("G", "R")
                wavelength_for = _wavelength_selector(channels)
                glonass = {"satellites": len(channels)}
        grid = build_target_grid(start_utc, end_utc, step_seconds)
        epochs, skipped = _build_output_epochs_phase_dense(
            master_base=master,
            geometry_epoch_map=geometry,
            target_xyz=target_xyz,
            time_grid=grid,
            max_gap_seconds=MAX_GAP_SECONDS,
            min_satellites_per_epoch=4,
            systems=systems,
            wavelength_for=wavelength_for,
        )
        glonass["observations"] = sum(
            1 for ep in epochs for o in ep["observations"] if o["satellite"].startswith("R")
        )
        info = {
            "stations_used": [code],
            "master_station": code,
            "master_distance_km": round(dist_m / 1000, 3),
            "master_quality_ratio": round(ratio, 3),
            "stations_skipped": tried,
            "nav_path": str(data["nav_path"]),
            "nav_paths": data.get("nav_paths") or [str(data["nav_path"])],
            "requested_epochs": len(grid),
            "systems": list(systems),
            "glonass": glonass,
        }
        return epochs, skipped, info
    detail = "; ".join(f"{t['station']} ({t['dist_km']} km): {t['reason']}" for t in tried)
    raise ValueError(f"Nenhuma estação RBMC selecionada tem dados bons nesse dia e horário. {detail}")


def unified_root(storage_root: Path, job_id: Any) -> Path:
    return storage_root / "processing" / f"job_{job_id}_unified"


def process_unified_vrs(
    job,
    storage_root: Path,
    step_seconds: int = 1,
    profile_config: dict | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    network_mode: str = "master",
    receiver_type: str | None = None,
    receiver_number: str | None = None,
    antenna_type: str | None = None,
    antenna_number: str | None = None,
    receiver_version: str | None = None,
) -> dict:
    """Gera a base VRS na janela exata pedida.

    receiver_type / receiver_number: o que vai em REC # / TYPE / VERS do RINEX;
    antenna_type / antenna_number: o que vai em ANT # / TYPE (escolhas do
    usuário). O COMMENT de estação virtual continua no cabeçalho.

    network_mode="master": transporte geométrico da base mais próxima (padrão).
    network_mode="basic": correção de rede do motor básico, adensada.
    """
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
    target_xyz = _geodetic_to_ecef(job.target_lat, job.target_lon, job.target_height)
    codes = json.loads(job.selected_station_codes_json or "[]")

    if network_mode == "master":
        _write_progress(results_dir, 10, "master", "Selecionando a base mestra mais próxima")
        epochs, skipped, info = build_master_transport_epochs(
            codes, job.session_date, target_xyz, start_utc, end_utc, step_seconds
        )
        return _finish_unified(
            job, results_dir, epochs, skipped, target_xyz, start_local, end_local,
            start_utc, end_utc, step_seconds, {"network_mode": "master", **info},
            receiver=receiver_header(
                receiver_type, receiver_number, antenna_type, antenna_number, receiver_version
            ),
        )

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
    nav_path = _first_nav_file(codes, job.session_date)
    if not nav_path:
        raise ValueError("Nenhum arquivo de navegação disponível nas bases selecionadas.")

    geom_start = start_utc - timedelta(minutes=PAD_MINUTES + 1)
    geom_end = end_utc + timedelta(minutes=PAD_MINUTES + 1)
    geometry_map = build_geometry_map(nav_path, geom_start, geom_end)

    _write_progress(results_dir, 80, "densify", f"Gerando épocas a {step_seconds} s")
    grid = build_target_grid(start_utc, end_utc, step_seconds)
    epochs, skipped = densify_basic_output(basic_rinex, target_xyz, geometry_map, grid)

    basic_root = Path(basic_result.get("job_root") or "")
    results_dir.mkdir(parents=True, exist_ok=True)
    if basic_rinex.exists():
        shutil.copy2(basic_rinex, results_dir / "network_15s.obs")
    if basic_root.exists():
        shutil.rmtree(basic_root, ignore_errors=True)

    return _finish_unified(
        job, results_dir, epochs, skipped, target_xyz, start_local, end_local,
        start_utc, end_utc, step_seconds,
        receiver=receiver_header(
                receiver_type, receiver_number, antenna_type, antenna_number, receiver_version
            ),
        extra={
            "network_mode": "basic",
            "requested_epochs": len(grid),
            "basic_fallback_to_reference_base": "reference_base_fallback" in basic_summary,
            "nav_path": nav_path,
        },
    )


_NAV_LETTERS = {"n": "GPS", "g": "GLONASS", "l": "Galileo", "f": "BeiDou", "p": "misto"}


def nav_kind(path: str) -> str | None:
    """Letra RINEX 2 da navegação (n, g, l, f, p) a partir do nome do arquivo."""
    name = Path(path).name.lower()
    for suffix in (".gz", ".z", ".zip"):
        if name.endswith(suffix):
            return None
    if name.endswith("_gn.rnx"):
        return "n"
    if name.endswith("_rn.rnx"):
        return "g"
    if name.endswith("_mn.rnx"):
        return "p"
    last = name[-1:]
    if len(name) > 4 and name[-4] == "." and name[-3:-1].isdigit() and last in _NAV_LETTERS:
        return last
    return None


def _copy_navigation_files(results_dir: Path, rinex_name: str, nav_paths: list) -> list[str]:
    """Copia as efemérides transmitidas reais da RBMC do dia com o nome da base.

    São os arquivos de navegação originais (não alterados) da estação usada,
    renomeados para acompanhar o RINEX de observação (ex.: BASE2320.26n).
    """
    out = []
    seen = set()
    stem = rinex_name[:-1]
    for raw in nav_paths:
        if not raw:
            continue
        src = Path(raw)
        kind = nav_kind(str(src))
        if not src.is_file() or kind is None or kind in seen:
            continue
        dst = results_dir / f"{stem}{kind}"
        shutil.copy2(src, dst)
        seen.add(kind)
        out.append(str(dst))
    return out


IGS_RADOMES = {"NONE", "SCIS", "SCIT", "TZGD", "LEIT", "LEIS", "TCWD", "DOME", "UNAV"}


def _rinex_text(value: str | None, size: int = 20) -> str:
    """ASCII maiúsculo, espaços simples, cortado no tamanho do campo RINEX."""
    text = "".join(ch for ch in str(value or "") if 32 <= ord(ch) < 127)
    return " ".join(text.upper().split())[:size]


def antenna_type_field(antenna_type: str | None) -> str:
    """Campo TYPE de ANT # / TYPE no padrão IGS: modelo em 16 colunas + radome em 4.

    "TPSHIPER_SR" -> "TPSHIPER_SR     NONE"; "LEIGS18 LEIT" -> "LEIGS18         LEIT".
    """
    text = _rinex_text(antenna_type, 40)
    if not text:
        return ""
    parts = text.split(" ")
    radome = "NONE"
    if len(parts) > 1 and parts[-1] in IGS_RADOMES:
        radome = parts.pop()
    model = "_".join(parts)[:16]
    return f"{model:<16}{radome}"


def receiver_header(
    receiver_type: str | None,
    receiver_number: str | None,
    antenna_type: str | None = None,
    antenna_number: str | None = None,
    receiver_version: str | None = None,
) -> dict:
    """Campos REC # / TYPE / VERS e ANT # / TYPE (RINEX: 20 colunas ASCII cada).

    O que o usuário informar vai para o cabeçalho; o que ficar em branco
    mantém o padrão da VRS (número "VRS", tipo "ORBITBASE VRS", antena "VIRTUAL").
    REC # recebe o ID interno da placa (rcv/id do .tps, ex.: O0IZ4RS5R82), que é
    o que o Topcon Tools usa para identificar o receptor; ele é gravado mesmo
    quando o modelo do receptor fica no padrão.
    """
    rtype = _rinex_text(receiver_type)
    rnumber = _rinex_text(receiver_number)
    rversion = _rinex_text(receiver_version)
    atype = antenna_type_field(antenna_type)
    anumber = _rinex_text(antenna_number)
    return {
        "number": rnumber or "VRS",
        "type": rtype or "ORBITBASE VRS",
        "version": rversion or ("ORBITBASE 1.0" if rtype else "1.0"),
        "antenna_number": anumber or "VRS",
        "antenna_type": atype or "VIRTUAL",
    }


def _finish_unified(
    job, results_dir: Path, epochs, skipped, target_xyz, start_local, end_local,
    start_utc, end_utc, step_seconds, extra: dict, receiver: dict | None = None,
) -> dict:
    receiver = receiver or receiver_header(None, None)
    if not epochs:
        raise ValueError("Nenhuma época válida na janela pedida.")
    _write_progress(results_dir, 90, "write", "Gravando RINEX")
    marker = (getattr(job, "output_marker_name", None) or "BASE")[:60]
    code4 = ("".join(ch for ch in marker if ch.isalnum()).upper()[:4] or "BASE").ljust(4, "0")
    doy = start_utc.timetuple().tm_yday
    rinex_name = f"{code4}{doy:03d}0.{start_utc.year % 100:02d}o"
    output_path = write_advanced_rinex_from_seed(
        output_path=str(results_dir / rinex_name),
        advanced_seed={"epochs": epochs},
        marker_name=marker,
        receiver_brand="ORBITBASE",
        receiver_number=receiver["number"],
        receiver_model=receiver["type"],
        receiver_version=receiver["version"],
        antenna_model=receiver["antenna_type"],
        antenna_number=receiver["antenna_number"],
        approx_position_xyz=target_xyz,
        antenna_delta_hen=(0.0, 0.0, 0.0),
        interval_seconds=float(step_seconds),
        require_phase_observables=True,
        output_systems=["G", "R"],
        phase_systems=("G", "R"),
        forced_obs_types=["C1", "P1", "L1", "C2", "P2", "L2"],
    )
    nav_outputs = _copy_navigation_files(
        results_dir, rinex_name, extra.get("nav_paths") or [extra.get("nav_path")]
    )
    summary = {
        "engine": "unified_v1",
        "receiver": receiver,
        "navigation_files": nav_outputs,
        "step_seconds": step_seconds,
        "window_local": [start_local.isoformat(), end_local.isoformat()],
        "window_utc": [_epoch_to_iso(start_utc), _epoch_to_iso(end_utc)],
        "epochs_written": len(epochs),
        "epochs_skipped": skipped,
        "rinex_path": output_path,
        **extra,
    }
    (results_dir / "unified_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    _write_progress(results_dir, 100, "done", "Base gerada", status="completed")
    return summary
