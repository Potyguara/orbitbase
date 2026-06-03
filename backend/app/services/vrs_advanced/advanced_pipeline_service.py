from __future__ import annotations

from pathlib import Path
import json
from datetime import timezone, datetime
from math import sin, cos, sqrt, radians

from app.services.vrs_processing_service import (
    _load_selected_station_codes_from_job,
    _resolve_candidate_base_paths,
    _prepare_candidate_files,
    _extract_station_meta,
)

from .advanced_config import normalize_advanced_config
from .advanced_progress import write_advanced_progress
from .advanced_window_service import (
    build_advanced_requested_window,
    build_target_time_grid,
)
from .advanced_ephemeris_service import ensure_precise_ephemeris_products
from .advanced_nav_service import resolve_advanced_navigation_files
from .advanced_geometry_service import build_precise_geometry_cache
from .advanced_observation_interpolation_service import (
    interpolate_advanced_observations,
)
from .advanced_validation_service import validate_advanced_seed
from .advanced_rinex_writer_service import (
    write_advanced_debug_seed,
    write_advanced_rinex_from_seed,
)
from .advanced_debug_report_service import write_advanced_debug_report
from .advanced_rinex_observation_reader_service import (
    build_base_numeric_data_from_candidates,
)
from .advanced_ppp_filter_service import (
    harden_advanced_seed_for_ppp,
    write_advanced_ppp_filter_diagnostics,
)


def _safe_float_or_none(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _extract_xyz_from_meta(
    meta: dict,
) -> tuple[float | None, float | None, float | None]:
    candidates = [
        ("approx_x", "approx_y", "approx_z"),
        ("x", "y", "z"),
        ("position_x", "position_y", "position_z"),
        ("ecef_x", "ecef_y", "ecef_z"),
        ("antenna_x", "antenna_y", "antenna_z"),
        ("marker_x", "marker_y", "marker_z"),
    ]

    for kx, ky, kz in candidates:
        x = _safe_float_or_none(meta.get(kx))
        y = _safe_float_or_none(meta.get(ky))
        z = _safe_float_or_none(meta.get(kz))
        if x is not None and y is not None and z is not None:
            return x, y, z

    xyz = meta.get("xyz")
    if isinstance(xyz, dict):
        x = _safe_float_or_none(xyz.get("x"))
        y = _safe_float_or_none(xyz.get("y"))
        z = _safe_float_or_none(xyz.get("z"))
        if x is not None and y is not None and z is not None:
            return x, y, z

    approx = meta.get("approx_position_xyz")
    if isinstance(approx, (list, tuple)) and len(approx) >= 3:
        x = _safe_float_or_none(approx[0])
        y = _safe_float_or_none(approx[1])
        z = _safe_float_or_none(approx[2])
        if x is not None and y is not None and z is not None:
            return x, y, z

    position_xyz = meta.get("position_xyz")
    if isinstance(position_xyz, (list, tuple)) and len(position_xyz) >= 3:
        x = _safe_float_or_none(position_xyz[0])
        y = _safe_float_or_none(position_xyz[1])
        z = _safe_float_or_none(position_xyz[2])
        if x is not None and y is not None and z is not None:
            return x, y, z

    return None, None, None


def _enrich_base_numeric_data_with_station_xyz(
    base_numeric_data: list[dict],
    candidate_metas: list[dict],
) -> list[dict]:
    meta_by_station = {}

    for meta in candidate_metas or []:
        station = (meta.get("station_code") or "").strip().upper()
        if not station:
            continue
        meta_by_station[station] = meta

    enriched = []

    for base in base_numeric_data or []:
        item = dict(base)
        station = (item.get("station_code") or "").strip().upper()

        meta = meta_by_station.get(station, {})
        x, y, z = _extract_xyz_from_meta(meta)

        item["approx_x"] = x
        item["approx_y"] = y
        item["approx_z"] = z

        enriched.append(item)

    return enriched


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            default=_json_default,
        ),
        encoding="utf-8",
    )


def _ensure_utc_datetime(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        text = text.replace("Z", "+00:00")

        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt = datetime.strptime(text, "%y %m %d %H %M %S.%f")
                except Exception:
                    try:
                        dt = datetime.strptime(text, "%Y %m %d %H %M %S.%f")
                    except Exception:
                        return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    return None


def _normalize_base_numeric_data(base_numeric_data: list[dict]) -> list[dict]:
    normalized = []

    for base in base_numeric_data or []:
        item = dict(base)

        epochs = item.get("epochs") or []
        new_epochs = []

        for ep in epochs:
            ep_item = dict(ep)

            raw_epoch = ep_item.get("epoch")
            if raw_epoch is None:
                raw_epoch = ep_item.get("epoch_utc")

            ep_dt = _ensure_utc_datetime(raw_epoch)
            if ep_dt is None:
                continue

            ep_item["epoch"] = ep_dt
            ep_item["epoch_utc"] = ep_dt.isoformat().replace("+00:00", "Z")
            new_epochs.append(ep_item)

        new_epochs.sort(key=lambda x: x["epoch"])
        item["epochs"] = new_epochs

        if new_epochs:
            item["first_epoch"] = new_epochs[0]["epoch"]
            item["last_epoch"] = new_epochs[-1]["epoch"]
            item["epoch_count"] = len(new_epochs)
        else:
            item["first_epoch"] = None
            item["last_epoch"] = None
            item["epoch_count"] = 0

        normalized.append(item)

    return normalized


def _summarize_base_numeric_data(base_numeric_data: list[dict]) -> dict:
    bases = []

    for base in base_numeric_data or []:
        first_epoch = base.get("first_epoch")
        last_epoch = base.get("last_epoch")

        bases.append(
            {
                "station_code": base.get("station_code"),
                "label": base.get("label"),
                "epoch_count": base.get("epoch_count", 0),
                "first_epoch": (
                    first_epoch.isoformat().replace("+00:00", "Z")
                    if isinstance(first_epoch, datetime)
                    else None
                ),
                "last_epoch": (
                    last_epoch.isoformat().replace("+00:00", "Z")
                    if isinstance(last_epoch, datetime)
                    else None
                ),
            }
        )

    return {
        "base_count": len(bases),
        "bases": bases,
    }


def _build_effective_time_grid(
    time_grid: list[datetime],
    base_numeric_data: list[dict],
) -> tuple[list[datetime], dict]:
    valid_bases = [
        b
        for b in (base_numeric_data or [])
        if b.get("epoch_count", 0) > 0
        and b.get("first_epoch") is not None
        and b.get("last_epoch") is not None
    ]

    if not valid_bases:
        raise ValueError(
            "Nenhuma base possui épocas válidas após a normalização do base_numeric_data."
        )

    common_start = max(b["first_epoch"] for b in valid_bases)
    common_end = min(b["last_epoch"] for b in valid_bases)

    if common_end <= common_start:
        raise ValueError(
            "As bases não possuem interseção temporal comum após a normalização."
        )

    effective_grid = [t for t in time_grid if common_start <= t <= common_end]

    summary = {
        "common_start": common_start,
        "common_end": common_end,
        "input_time_grid_count": len(time_grid),
        "effective_time_grid_count": len(effective_grid),
        "valid_bases": len(valid_bases),
    }

    return effective_grid, summary


def _geodetic_to_ecef(
    lat_deg: float, lon_deg: float, h_m: float
) -> tuple[float, float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)

    lat = radians(lat_deg)
    lon = radians(lon_deg)

    sin_lat = sin(lat)
    cos_lat = cos(lat)
    sin_lon = sin(lon)
    cos_lon = cos(lon)

    N = a / sqrt(1 - e2 * sin_lat * sin_lat)

    x = (N + h_m) * cos_lat * cos_lon
    y = (N + h_m) * cos_lat * sin_lon
    z = (N * (1 - e2) + h_m) * sin_lat

    return x, y, z


def _count_epochs_with_satellites(seed: dict) -> int:
    epochs = seed.get("epochs") or []
    count = 0

    for epoch in epochs:
        if (epoch.get("satellite_count") or 0) > 0:
            count += 1
            continue

        observations = epoch.get("observations") or []
        if observations:
            count += 1

    return count


def process_real_vrs_advanced(
    job,
    profile_config: dict | None = None,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
) -> dict:
    storage_root = Path(__file__).resolve().parents[3] / "storage"
    job_root = storage_root / "processing" / f"job_{job.id}_advanced"
    input_root = job_root / "inputs"
    result_root = job_root / "results"
    metadata_root = job_root / "metadata"
    log_root = job_root / "logs"

    print(f"[ADV-PIPE] start job={job.id}", flush=True)

    input_root.mkdir(parents=True, exist_ok=True)
    result_root.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)

    print(f"[ADV-PIPE] job_root={job_root}", flush=True)

    cfg = normalize_advanced_config(profile_config)
    print(f"[ADV-PIPE] cfg={cfg}", flush=True)

    _write_json(metadata_root / "advanced_config_used.json", cfg)

    write_advanced_progress(
        job_root,
        2,
        "start",
        "Iniciando processamento avançado",
        f"job={job.id}",
    )

    window = build_advanced_requested_window(job)
    _write_json(
        metadata_root / "advanced_requested_window.json",
        {
            "start_local": window["start_local"].isoformat(sep=" "),
            "end_local": window["end_local"].isoformat(sep=" "),
            "start_utc": window["start_utc"].isoformat(),
            "end_utc": window["end_utc"].isoformat(),
            "duration_seconds": window["duration_seconds"],
        },
    )

    print(
        f"[ADV-PIPE] window={window['start_utc'].isoformat()} -> {window['end_utc'].isoformat()}",
        flush=True,
    )

    write_advanced_progress(
        job_root,
        8,
        "window",
        "Janela temporal avançada validada",
        f"{window['start_utc'].isoformat()} -> {window['end_utc'].isoformat()}",
    )

    selected_station_codes = _load_selected_station_codes_from_job(job)
    if len(selected_station_codes) < 3:
        raise ValueError("Selecione ao menos 3 estações RBMC para o modo avançado.")

    print(f"[ADV-PIPE] selected_station_codes={selected_station_codes}", flush=True)

    _write_json(
        metadata_root / "advanced_selected_station_codes.json",
        {"selected_station_codes": selected_station_codes},
    )

    write_advanced_progress(
        job_root,
        12,
        "candidate_bases_start",
        "Resolvendo bases candidatas",
        f"{len(selected_station_codes)} estações selecionadas",
    )

    candidate_paths = _resolve_candidate_base_paths(job)
    print(f"[ADV-PIPE] candidate_paths={candidate_paths}", flush=True)

    _write_json(
        metadata_root / "advanced_candidate_paths.json",
        {"candidate_paths": candidate_paths},
    )

    write_advanced_progress(
        job_root,
        16,
        "candidate_bases_done",
        "Bases candidatas resolvidas",
        f"{len(candidate_paths)} candidatas",
    )

    prepared_candidates = _prepare_candidate_files(candidate_paths, input_root)
    print(f"[ADV-PIPE] prepared_candidates={len(prepared_candidates)}", flush=True)

    prepared_candidates_json = []
    for item in prepared_candidates:
        prepared_candidates_json.append(
            {
                "station_code": item.get("station_code"),
                "label": item.get("label"),
                "source_type": item.get("source_type"),
                "original_path": str(item.get("original_path")),
                "copied_path": str(item.get("copied_path")),
                "rinex_path": str(item.get("rinex_path")),
            }
        )

    _write_json(
        metadata_root / "advanced_prepared_candidates.json",
        {"prepared_candidates": prepared_candidates_json},
    )

    write_advanced_progress(
        job_root,
        24,
        "hatanaka_done",
        "Arquivos preparados e convertidos",
        f"{len(prepared_candidates)} bases preparadas",
    )

    candidate_metas = []
    for item in prepared_candidates:
        meta = _extract_station_meta(
            item["copied_path"],
            label=item["label"],
            station_code=item.get("station_code"),
        )
        meta["source_type"] = item.get("source_type")
        meta["copied_path"] = str(item["copied_path"])
        meta["original_path"] = str(item["original_path"])
        meta["rinex_path"] = str(item["rinex_path"])
        candidate_metas.append(meta)

    _write_json(
        metadata_root / "advanced_candidate_metas.json",
        {"candidate_metas": candidate_metas},
    )

    print(f"[ADV-PIPE] candidate_metas={len(candidate_metas)}", flush=True)
    print(
        f"[ADV-PIPE] candidate_metas_preview={json.dumps(candidate_metas[:2], ensure_ascii=False, default=_json_default)}",
        flush=True,
    )

    write_advanced_progress(
        job_root,
        30,
        "metadata_done",
        "Metadados das bases extraídos",
        f"{len(candidate_metas)} bases válidas",
    )

    write_advanced_progress(
        job_root,
        32,
        "base_obs_start",
        "Extraindo observáveis reais das bases",
        f"janela={window['start_utc'].isoformat()} -> {window['end_utc'].isoformat()}",
    )

    base_numeric_data = build_base_numeric_data_from_candidates(
        candidate_metas=candidate_metas,
        start_utc=window["start_utc"],
        end_utc=window["end_utc"],
        step_seconds=int(cfg["target_step_seconds"]),
    )

    _write_json(
        metadata_root / "advanced_base_numeric_data_raw.json",
        {"base_numeric_data": base_numeric_data},
    )

    print(
        f"[ADV-PIPE] base_numeric_data={len(base_numeric_data)} bases extraídas",
        flush=True,
    )

    base_numeric_data = _normalize_base_numeric_data(base_numeric_data)
    base_numeric_data = _enrich_base_numeric_data_with_station_xyz(
        base_numeric_data=base_numeric_data,
        candidate_metas=candidate_metas,
    )

    _write_json(
        metadata_root / "advanced_base_numeric_data_normalized.json",
        {"base_numeric_data": base_numeric_data},
    )

    print(
        f"[ADV-PIPE] base_xyz_check={json.dumps([{
            'station_code': b.get('station_code'),
            'approx_x': b.get('approx_x'),
            'approx_y': b.get('approx_y'),
            'approx_z': b.get('approx_z'),
        } for b in base_numeric_data], ensure_ascii=False, default=_json_default)}",
        flush=True,
    )

    base_numeric_summary = _summarize_base_numeric_data(base_numeric_data)

    _write_json(
        metadata_root / "advanced_base_numeric_data_summary.json",
        base_numeric_summary,
    )

    print(
        f"[ADV-PIPE] base_numeric_summary={json.dumps(base_numeric_summary, default=_json_default, ensure_ascii=False)}",
        flush=True,
    )

    write_advanced_progress(
        job_root,
        34,
        "base_obs_done",
        "Observáveis reais extraídos",
        f"{len(base_numeric_data)} bases com dados na janela",
    )

    nav_context = resolve_advanced_navigation_files(candidate_paths)

    _write_json(
        metadata_root / "advanced_navigation_context.json",
        {"navigation_context": nav_context},
    )

    print(f"[ADV-PIPE] nav_context={len(nav_context)}", flush=True)

    write_advanced_progress(
        job_root,
        36,
        "nav_done",
        "Identificando arquivos de navegação",
        f"{len(nav_context)} itens de contexto",
    )

    use_precise_ephemeris = bool(cfg.get("use_precise_ephemeris", True))

    if use_precise_ephemeris and (not earthdata_username or not earthdata_password):
        write_advanced_progress(
            job_root,
            38,
            "precise_products_failed",
            "Credenciais Earthdata ausentes",
            "As efemérides precisas foram solicitadas, mas o pipeline não recebeu usuário/senha Earthdata.",
            status="failed",
        )
        raise ValueError(
            "Credenciais Earthdata / CDDIS não informadas para o processamento avançado."
        )

    print("[ADV-PIPE] chamando ensure_precise_ephemeris_products...", flush=True)

    write_advanced_progress(
        job_root,
        40,
        "precise_products_start",
        "Baixando efemérides precisas do dia",
        f"data={job.session_date}",
    )

    precise_products = ensure_precise_ephemeris_products(
        session_date=job.session_date,
        storage_root=storage_root,
        earthdata_username=earthdata_username,
        earthdata_password=earthdata_password,
        timeout_seconds=60,
        force_refresh=False,
    )

    _write_json(
        metadata_root / "advanced_precise_products.json",
        precise_products,
    )

    print(f"[ADV-PIPE] precise_products={precise_products}", flush=True)

    write_advanced_progress(
        job_root,
        46,
        "precise_products_done",
        "Efemérides precisas verificadas",
        json.dumps(precise_products, ensure_ascii=False, default=_json_default),
        status="running" if precise_products.get("sp3_available") else "failed",
    )

    if use_precise_ephemeris and not precise_products.get("sp3_available"):
        raise ValueError(
            "Não foi possível obter arquivo SP3 válido para a data do job no modo avançado."
        )

    time_grid = build_target_time_grid(
        start_utc=window["start_utc"],
        end_utc=window["end_utc"],
        step_seconds=int(cfg["target_step_seconds"]),
    )

    effective_time_grid, effective_grid_summary = _build_effective_time_grid(
        time_grid=time_grid,
        base_numeric_data=base_numeric_data,
    )

    _write_json(
        metadata_root / "advanced_effective_time_grid_summary.json",
        effective_grid_summary,
    )

    print(
        f"[ADV-PIPE] effective_grid_summary={json.dumps(effective_grid_summary, default=_json_default, ensure_ascii=False)}",
        flush=True,
    )

    _write_json(
        metadata_root / "advanced_time_grid_summary.json",
        {
            "count": len(time_grid),
            "first_epoch": time_grid[0].isoformat() if time_grid else None,
            "last_epoch": time_grid[-1].isoformat() if time_grid else None,
            "step_seconds": int(cfg["target_step_seconds"]),
            "effective_count": len(effective_time_grid),
        },
    )

    write_advanced_progress(
        job_root,
        52,
        "time_grid_done",
        "Grade temporal efetiva construída",
        f"input={len(time_grid)} | effective={len(effective_time_grid)}",
    )

    write_advanced_progress(
        job_root,
        56,
        "geometry_start",
        "Montando cache orbital",
        "Preparando geometria com efemérides precisas",
    )

    geometry_cache = build_precise_geometry_cache(
        time_grid=effective_time_grid,
        precise_products=precise_products,
        navigation_context=nav_context,
    )

    _write_json(
        metadata_root / "advanced_geometry_cache.json",
        geometry_cache,
    )

    write_advanced_progress(
        job_root,
        62,
        "geometry_done",
        "Cache orbital montado",
        json.dumps(geometry_cache, ensure_ascii=False, default=_json_default),
    )

    target_lat = getattr(job, "target_lat", None)
    target_lon = getattr(job, "target_lon", None)
    target_h = getattr(job, "target_height", None)

    approx_position_xyz = None
    if target_lat is not None and target_lon is not None:
        x, y, z = _geodetic_to_ecef(
            float(target_lat),
            float(target_lon),
            float(target_h or 0.0),
        )
        approx_position_xyz = (x, y, z)

        cfg["target_x"] = x
        cfg["target_y"] = y
        cfg["target_z"] = z

        print(f"[ADV-PIPE] target_ecef=({x:.4f}, {y:.4f}, {z:.4f})", flush=True)
    else:
        print(
            "[ADV-PIPE] target_lat/target_lon ausentes no job; APPROX POSITION XYZ permanecerá zerado.",
            flush=True,
        )

    write_advanced_progress(
        job_root,
        70,
        "interpolation_start",
        "Calculando interpolação das observáveis",
        (
            f"code={bool(cfg.get('interpolate_code_observables'))} | "
            f"phase={bool(cfg.get('interpolate_phase_observables'))}"
        ),
    )

    advanced_seed_pre_ppp = interpolate_advanced_observations(
        time_grid=effective_time_grid,
        base_numeric_data=base_numeric_data,
        geometry_cache=geometry_cache,
        config=cfg,
    )

    print(
        f"[ADV-PIPE] advanced_seed_pre_ppp_type={type(advanced_seed_pre_ppp).__name__}",
        flush=True,
    )

    if not isinstance(advanced_seed_pre_ppp, dict):
        raise ValueError("A interpolação avançada retornou estrutura inválida.")

    if approx_position_xyz is not None:
        advanced_seed_pre_ppp["approx_x"] = approx_position_xyz[0]
        advanced_seed_pre_ppp["approx_y"] = approx_position_xyz[1]
        advanced_seed_pre_ppp["approx_z"] = approx_position_xyz[2]

    pre_ppp_seed_json_path = write_advanced_debug_seed(
        str(result_root / "advanced_seed_pre_ppp.json"),
        advanced_seed_pre_ppp,
    )

    _write_json(
        metadata_root / "advanced_seed_pre_ppp_summary.json",
        {
            "status": advanced_seed_pre_ppp.get("status"),
            "message": advanced_seed_pre_ppp.get("message"),
            "epochs_generated": advanced_seed_pre_ppp.get("epochs_generated"),
            "epochs_skipped": advanced_seed_pre_ppp.get("epochs_skipped"),
            "epochs_with_satellites": _count_epochs_with_satellites(
                advanced_seed_pre_ppp
            ),
            "summary": advanced_seed_pre_ppp.get("summary"),
            "seed_json_path": pre_ppp_seed_json_path,
        },
    )

    print(
        f"[ADV-PIPE] advanced_seed_pre_ppp_epochs_generated={advanced_seed_pre_ppp.get('epochs_generated')}",
        flush=True,
    )
    print(
        f"[ADV-PIPE] advanced_seed_pre_ppp_epochs_with_satellites={_count_epochs_with_satellites(advanced_seed_pre_ppp)}",
        flush=True,
    )

    if advanced_seed_pre_ppp.get("epochs_generated", 0) <= 0:
        _write_json(
            metadata_root / "advanced_seed_zero_debug.json",
            {
                "advanced_seed_pre_ppp": advanced_seed_pre_ppp,
                "effective_grid_summary": effective_grid_summary,
                "base_numeric_summary": base_numeric_summary,
            },
        )
        raise ValueError(
            "A interpolação avançada não gerou épocas observacionais. "
            "Arquivo debug salvo em metadata/advanced_seed_zero_debug.json."
        )

    pre_ppp_epochs_with_sat = _count_epochs_with_satellites(advanced_seed_pre_ppp)
    if pre_ppp_epochs_with_sat <= 0:
        _write_json(
            metadata_root / "advanced_seed_empty_observations_debug.json",
            {
                "advanced_seed_pre_ppp": advanced_seed_pre_ppp,
                "effective_grid_summary": effective_grid_summary,
                "base_numeric_summary": base_numeric_summary,
                "geometry_cache_summary": {
                    "epochs_with_geometry": geometry_cache.get("epochs_with_geometry"),
                    "satellite_count": geometry_cache.get("satellite_count"),
                    "sp3_epoch_count": geometry_cache.get("sp3_epoch_count"),
                    "status": geometry_cache.get("status"),
                    "source": geometry_cache.get("source"),
                },
                "config": cfg,
            },
        )
        raise ValueError(
            "A interpolação avançada concluiu, porém não produziu satélites/observáveis utilizáveis "
            "em nenhuma época. O erro real está antes do PPP filter. "
            "Verifique result/advanced_seed_pre_ppp.json e "
            "metadata/advanced_seed_empty_observations_debug.json."
        )

    write_advanced_progress(
        job_root,
        78,
        "interpolation_done",
        "Interpolação avançada executada",
        (
            f"epochs_generated={advanced_seed_pre_ppp.get('epochs_generated', 0)} | "
            f"epochs_with_satellites={pre_ppp_epochs_with_sat}"
        ),
    )

    write_advanced_progress(
        job_root,
        80,
        "ppp_hardening_start",
        "Aplicando endurecimento PPP nas observáveis",
        "Seleção de satélites e recorte do melhor bloco contínuo",
    )

    advanced_seed_post_ppp, ppp_filter_diagnostics = harden_advanced_seed_for_ppp(
        seed=advanced_seed_pre_ppp,
        max_satellites=12,
        min_presence_ratio=0.30,
        min_dual_freq_ratio=0.25,
        max_gap_epochs=45,
        min_satellites_per_epoch=4,
        require_dual_frequency=True,
        prefer_phase_but_do_not_require=True,
        priority_obs=["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"],
    )

    ppp_filter_diag_path = write_advanced_ppp_filter_diagnostics(
        result_root / "advanced_ppp_filter_diagnostics.json",
        ppp_filter_diagnostics,
    )

    _write_json(
        metadata_root / "advanced_ppp_filter_summary.json",
        ppp_filter_diagnostics,
    )

    post_ppp_seed_json_path = write_advanced_debug_seed(
        str(result_root / "advanced_seed_post_ppp.json"),
        advanced_seed_post_ppp,
    )

    post_ppp_epochs_with_sat = _count_epochs_with_satellites(advanced_seed_post_ppp)

    _write_json(
        metadata_root / "advanced_seed_post_ppp_summary.json",
        {
            "status": advanced_seed_post_ppp.get("status"),
            "message": advanced_seed_post_ppp.get("message"),
            "epochs_generated": advanced_seed_post_ppp.get("epochs_generated"),
            "epochs_skipped": advanced_seed_post_ppp.get("epochs_skipped"),
            "epochs_with_satellites": post_ppp_epochs_with_sat,
            "seed_json_path": post_ppp_seed_json_path,
        },
    )

    write_advanced_progress(
        job_root,
        84,
        "ppp_hardening_done",
        "Endurecimento PPP aplicado",
        (
            f"diag={ppp_filter_diag_path} | "
            f"epochs_with_satellites={post_ppp_epochs_with_sat}"
        ),
    )

    if post_ppp_epochs_with_sat <= 0:
        raise ValueError(
            "Após o endurecimento PPP, nenhuma época permaneceu com satélites utilizáveis. "
            "Compare result/advanced_seed_pre_ppp.json com result/advanced_seed_post_ppp.json."
        )

    validation = validate_advanced_seed(advanced_seed_post_ppp, cfg)

    write_advanced_progress(
        job_root,
        86,
        "validation_done",
        "Validação do modo avançado concluída",
        validation.get("message"),
        status="running" if validation["status"] == "completed" else "failed",
    )

    reference_rinex_path = prepared_candidates[0]["rinex_path"]

    output_path = write_advanced_rinex_from_seed(
        reference_rinex_path=reference_rinex_path,
        output_path=str(result_root / "ADVANCED_BASE.obs"),
        advanced_seed=advanced_seed_post_ppp,
        marker_name=(getattr(job, "output_marker_name", None) or "ADVBASE"),
        receiver_brand=cfg["receiver_brand"],
        receiver_model=cfg["receiver_model"],
        receiver_version=cfg["receiver_version"],
        antenna_model=cfg["antenna_model"],
        approx_position_xyz=approx_position_xyz,
        antenna_delta_hen=(0.0, 0.0, 0.0),
        interval_seconds=float(
            advanced_seed_post_ppp.get("interval_seconds", cfg["target_step_seconds"])
        ),
        require_phase_observables=bool(cfg.get("interpolate_phase_observables", True)),
    )
    debug_report = {
        "job_id": job.id,
        "processed_at": datetime.utcnow().isoformat() + "Z",
        "config": cfg,
        "window": {
            "start_local": window["start_local"].isoformat(sep=" "),
            "end_local": window["end_local"].isoformat(sep=" "),
            "start_utc": window["start_utc"].isoformat(),
            "end_utc": window["end_utc"].isoformat(),
            "duration_seconds": window["duration_seconds"],
        },
        "selected_station_codes": selected_station_codes,
        "candidate_bases_count": len(candidate_paths),
        "prepared_candidates_count": len(prepared_candidates),
        "candidate_metas_count": len(candidate_metas),
        "base_numeric_data_count": len(base_numeric_data),
        "base_numeric_epoch_counts": [
            {
                "station_code": item.get("station_code"),
                "label": item.get("label"),
                "epoch_count": item.get("epoch_count", 0),
            }
            for item in base_numeric_data
        ],
        "precise_products": precise_products,
        "time_grid_count": len(time_grid),
        "effective_time_grid_count": len(effective_time_grid),
        "geometry_cache": geometry_cache,
        "validation": validation,
        "pre_ppp_seed_json_path": pre_ppp_seed_json_path,
        "post_ppp_seed_json_path": post_ppp_seed_json_path,
        "output_path": output_path,
        "ppp_filter_diagnostics": ppp_filter_diagnostics,
    }

    debug_report_path = write_advanced_debug_report(
        str(result_root / "advanced_debug_report.json"),
        debug_report,
    )

    final_status = "completed" if validation["status"] == "completed" else "failed"

    write_advanced_progress(
        job_root,
        100,
        "done",
        "Processamento avançado concluído",
        debug_report_path,
        status=final_status,
    )

    return {
        "ok": validation["status"] == "completed",
        "job_id": job.id,
        "mode": "advanced_experimental",
        "output_path": output_path,
        "debug_report_path": debug_report_path,
        "pre_ppp_seed_json_path": pre_ppp_seed_json_path,
        "post_ppp_seed_json_path": post_ppp_seed_json_path,
        "validation": validation,
        "precise_products": precise_products,
        "ppp_filter_diagnostics_path": ppp_filter_diag_path,
    }
