from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import cos, radians, sin, sqrt
from pathlib import Path
import json

from app.services.vrs_processing_service import (
    _extract_station_meta,
    _load_selected_station_codes_from_job,
    _prepare_candidate_files,
    _resolve_candidate_base_paths,
)

from .advanced_config import normalize_advanced_config
from .advanced_debug_report_service import write_advanced_debug_report
from .advanced_ephemeris_service import (
    ensure_broadcast_navigation,
    ensure_precise_ephemeris_products,
)
from .advanced_geometry_service import build_precise_geometry_cache
from .advanced_json_io import dump_json_file, max_rss_mb, debug_seed_dumps_enabled
from .advanced_nav_service import resolve_advanced_navigation_files
from .advanced_observation_interpolation_service import (
    interpolate_advanced_observations,
)
from .advanced_ppp_filter_service import (
    harden_advanced_seed_for_ppp,
    write_advanced_ppp_filter_diagnostics,
)
from .advanced_progress import write_advanced_progress
from .advanced_rinex_observation_reader_service import (
    build_base_numeric_data_from_candidates,
)
from .advanced_rinex_writer_service import (
    write_advanced_debug_seed,
    write_advanced_rinex_from_seed,
)
from .advanced_validation_service import validate_advanced_seed
from .advanced_window_service import (
    build_advanced_requested_window,
    build_target_time_grid,
)


def _safe_float_or_none(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _geometry_summary(geometry_cache: dict) -> dict:
    return {k: v for k, v in (geometry_cache or {}).items() if k != "epochs"}


def _write_json(path: Path, payload) -> None:
    dump_json_file(path, payload)


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


# Margem lida das bases além da janela pedida. As bases RBMC têm épocas a cada
# 15/30 s; sem a margem, um início como 07:05:22 não tem época anterior para
# interpolar e a grade era cortada para 07:05:30.
BASE_WINDOW_PADDING_SECONDS = 120


def _base_extraction_window(window: dict) -> tuple[datetime, datetime]:
    pad = timedelta(seconds=BASE_WINDOW_PADDING_SECONDS)
    return window["start_utc"] - pad, window["end_utc"] + pad


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
        "requested_start": time_grid[0] if time_grid else None,
        "requested_end": time_grid[-1] if time_grid else None,
        "requested_time_grid_count": len(time_grid),
        "effective_time_grid_count": len(effective_grid),
        "common_start": common_start,
        "common_end": common_end,
        "valid_bases": len(valid_bases),
        "mode": "preserve_requested_window",
    }

    return effective_grid, summary


def _geodetic_to_ecef(
    lat_deg: float,
    lon_deg: float,
    h_m: float,
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

    n = a / sqrt(1 - e2 * sin_lat * sin_lat)

    x = (n + h_m) * cos_lat * cos_lon
    y = (n + h_m) * cos_lat * sin_lon
    z = (n * (1 - e2) + h_m) * sin_lat

    return x, y, z


def _count_epochs_with_satellites(seed: dict) -> int:
    epochs = seed.get("epochs") or []
    count = 0

    for epoch in epochs:
        if int(epoch.get("satellite_count") or 0) > 0:
            count += 1
            continue

        if epoch.get("observations"):
            count += 1

    return count


def _count_total_satellite_instances(seed: dict) -> int:
    total = 0
    for epoch in seed.get("epochs") or []:
        obs = epoch.get("observations") or []
        total += len(obs)
    return total


def _extract_interval_seconds(seed: dict, fallback: float) -> float:
    try:
        value = seed.get("interval_seconds")
        if value is None or value == "":
            return float(fallback)
        return float(value)
    except Exception:
        return float(fallback)


def _choose_final_seed(
    pre_ppp_seed: dict,
    post_ppp_seed: dict | None,
    ppp_filter_diagnostics: dict | None,
) -> tuple[dict, dict]:
    pre_epochs = int(pre_ppp_seed.get("epochs_generated", 0) or 0)
    pre_epochs_with_sat = _count_epochs_with_satellites(pre_ppp_seed)
    pre_sat_instances = _count_total_satellite_instances(pre_ppp_seed)

    if not isinstance(post_ppp_seed, dict):
        return pre_ppp_seed, {
            "selected_source": "pre_ppp",
            "reason": "post_ppp_seed_invalid",
            "pre_epochs": pre_epochs,
            "pre_epochs_with_satellites": pre_epochs_with_sat,
            "pre_satellite_instances": pre_sat_instances,
        }

    post_epochs = int(post_ppp_seed.get("epochs_generated", 0) or 0)
    post_epochs_with_sat = _count_epochs_with_satellites(post_ppp_seed)
    post_sat_instances = _count_total_satellite_instances(post_ppp_seed)

    if post_epochs_with_sat <= 0 or post_sat_instances <= 0:
        return pre_ppp_seed, {
            "selected_source": "pre_ppp",
            "reason": "post_ppp_empty",
            "pre_epochs": pre_epochs,
            "pre_epochs_with_satellites": pre_epochs_with_sat,
            "pre_satellite_instances": pre_sat_instances,
            "post_epochs": post_epochs,
            "post_epochs_with_satellites": post_epochs_with_sat,
            "post_satellite_instances": post_sat_instances,
        }

    if pre_epochs_with_sat > 0:
        epoch_ratio = post_epochs_with_sat / max(1, pre_epochs_with_sat)
        sat_ratio = post_sat_instances / max(1, pre_sat_instances)

        if epoch_ratio < 0.60 or sat_ratio < 0.45:
            return pre_ppp_seed, {
                "selected_source": "pre_ppp",
                "reason": "post_ppp_overfiltered",
                "epoch_ratio": epoch_ratio,
                "sat_ratio": sat_ratio,
                "pre_epochs": pre_epochs,
                "pre_epochs_with_satellites": pre_epochs_with_sat,
                "pre_satellite_instances": pre_sat_instances,
                "post_epochs": post_epochs,
                "post_epochs_with_satellites": post_epochs_with_sat,
                "post_satellite_instances": post_sat_instances,
            }

    return post_ppp_seed, {
        "selected_source": "post_ppp",
        "reason": "post_ppp_accepted",
        "pre_epochs": pre_epochs,
        "pre_epochs_with_satellites": pre_epochs_with_sat,
        "pre_satellite_instances": pre_sat_instances,
        "post_epochs": post_epochs,
        "post_epochs_with_satellites": post_epochs_with_sat,
        "post_satellite_instances": post_sat_instances,
        "ppp_filter_stage": (
            ppp_filter_diagnostics.get("stage")
            if isinstance(ppp_filter_diagnostics, dict)
            else None
        ),
    }


def _decide_advanced_output_profile(cfg: dict) -> dict:
    use_phase = bool(cfg.get("interpolate_phase_observables", False))

    if use_phase:
        return {
            "profile_name": "ppp_gps_strict",
            "allowed_systems": ["G"],
            "writer_require_phase_observables": True,
            "writer_obs_types": ["C1", "L1", "P2", "L2"],
            "ppp_require_dual_frequency": True,
            "ppp_require_phase_for_dual_frequency": True,
            "ppp_prefer_phase_but_do_not_require": False,
            "max_satellites": 8,
            "min_presence_ratio": 0.45,
            "min_dual_freq_ratio": 0.40,
            "max_gap_epochs": 20,
            "min_satellites_per_epoch": max(
                4, int(cfg.get("min_satellites_per_epoch", 4) or 4)
            ),
        }

    return {
        "profile_name": "mixed_code_dense",
        "allowed_systems": ["G", "R"],
        "writer_require_phase_observables": False,
        "writer_obs_types": ["C1", "P2"],
        "ppp_require_dual_frequency": False,
        "ppp_require_phase_for_dual_frequency": False,
        "ppp_prefer_phase_but_do_not_require": True,
        "max_satellites": 10,
        "min_presence_ratio": 0.25,
        "min_dual_freq_ratio": 0.00,
        "max_gap_epochs": 40,
        "min_satellites_per_epoch": max(
            4, int(cfg.get("min_satellites_per_epoch", 4) or 4)
        ),
    }


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
    output_profile = _decide_advanced_output_profile(cfg)
    cfg["output_profile"] = output_profile

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

    base_start_utc, base_end_utc = _base_extraction_window(window)

    base_numeric_data = build_base_numeric_data_from_candidates(
        candidate_metas=candidate_metas,
        start_utc=base_start_utc,
        end_utc=base_end_utc,
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

    nav_context_input = []

    for item in candidate_paths or []:
        nav_context_input.append(
            {
                "station_code": item.get("station_code"),
                "label": item.get("label"),
                "path": str(item.get("path")) if item.get("path") else None,
                "nav_path": item.get("nav_path"),
                "nav_paths": list(item.get("nav_paths") or []),
                "source": "candidate_paths",
            }
        )

    for item in prepared_candidates or []:
        nav_context_input.append(
            {
                "station_code": item.get("station_code"),
                "label": item.get("label"),
                "obs_path": (
                    str(item.get("copied_path")) if item.get("copied_path") else None
                ),
                "rinex_path": (
                    str(item.get("rinex_path")) if item.get("rinex_path") else None
                ),
                "copied_path": (
                    str(item.get("copied_path")) if item.get("copied_path") else None
                ),
                "original_path": (
                    str(item.get("original_path"))
                    if item.get("original_path")
                    else None
                ),
                "source": "prepared_candidates",
            }
        )

    for meta in candidate_metas or []:
        nav_context_input.append(
            {
                "station_code": meta.get("station_code"),
                "label": meta.get("label"),
                "obs_path": meta.get("file_path"),
                "rinex_path": meta.get("rinex_path"),
                "copied_path": meta.get("copied_path"),
                "original_path": meta.get("original_path"),
                "source": "candidate_metas",
            }
        )

    print(
        f"[ADV-PIPE] nav_context_input_preview={json.dumps(nav_context_input[:8], ensure_ascii=False, default=_json_default)}",
        flush=True,
    )

    nav_context = resolve_advanced_navigation_files(
        nav_context_input,
        session_date=job.session_date,
    )
    local_nav_count = sum(1 for item in nav_context if item.get("nav_path"))

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
        f"{local_nav_count}/{len(nav_context)} estações com NAV local",
    )

    use_precise_ephemeris = bool(cfg.get("use_precise_ephemeris", True))

    if use_precise_ephemeris and (not earthdata_username or not earthdata_password):
        # Sem Earthdata ainda tentamos os espelhos anônimos (BKG/IGN) e,
        # em último caso, a navegação broadcast. Não é mais um erro fatal.
        print(
            "[ADV-PIPE] Earthdata ausente: usando apenas espelhos SP3 anônimos + NAV.",
            flush=True,
        )
        write_advanced_progress(
            job_root,
            38,
            "precise_products_no_credentials",
            "Credenciais Earthdata ausentes",
            "Tentando espelhos SP3 anônimos (BKG/IGN) e navegação broadcast.",
        )

    print("[ADV-PIPE] chamando ensure_precise_ephemeris_products...", flush=True)

    write_advanced_progress(
        job_root,
        40,
        "precise_products_start",
        "Baixando efemérides precisas do dia",
        f"data={job.session_date}",
    )

    if use_precise_ephemeris:
        precise_products = ensure_precise_ephemeris_products(
            session_date=job.session_date,
            storage_root=storage_root,
            earthdata_username=earthdata_username,
            earthdata_password=earthdata_password,
            timeout_seconds=30,
            force_refresh=False,
        )
    else:
        precise_products = {
            "session_date": job.session_date,
            "sp3_available": False,
            "sp3_path": None,
            "status": "disabled_by_config",
            "download_errors": [],
        }

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
        status="running",
    )

    if use_precise_ephemeris and not precise_products.get("sp3_available"):
        print(
            "[ADV-PIPE] SP3 indisponível; seguindo com fallback para geometria broadcast/nav.",
            flush=True,
        )
        write_advanced_progress(
            job_root,
            46,
            "precise_products_fallback",
            "SP3 indisponível; seguindo com fallback",
            "O modo avançado continuará com geometria derivada da navegação broadcast.",
            status="running",
        )

    if not precise_products.get("sp3_available") and local_nav_count == 0:
        print(
            "[ADV-PIPE] sem SP3 e sem NAV local: tentando BRDC diário do IGS.",
            flush=True,
        )
        brdc_result = ensure_broadcast_navigation(
            session_date=job.session_date,
            storage_root=storage_root,
            earthdata_username=earthdata_username,
            earthdata_password=earthdata_password,
            timeout_seconds=30,
        )
        _write_json(metadata_root / "advanced_brdc_download.json", brdc_result)
        precise_products.setdefault("download_errors", [])
        precise_products["brdc_download"] = {
            "nav_available": brdc_result.get("nav_available"),
            "nav_path": brdc_result.get("nav_path"),
            "errors": brdc_result.get("download_errors", [])[:10],
        }
        if brdc_result.get("nav_path"):
            nav_context.append(
                {
                    "station_code": "IGS_BRDC",
                    "obs_path": None,
                    "nav_path": brdc_result["nav_path"],
                    "nav_paths": [brdc_result["nav_path"]],
                    "status": "downloaded_brdc",
                }
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

    # Só o resumo: as épocas completas (passo de 1 s x horas x satélites) não
    # cabem em memória quando serializadas.
    _write_json(
        metadata_root / "advanced_geometry_cache.json",
        _geometry_summary(geometry_cache),
    )
    print(f"[ADV-MEM] after_geometry max_rss_mb={max_rss_mb():.0f}", flush=True)

    if int(geometry_cache.get("epochs_with_geometry", 0) or 0) <= 0:
        message = geometry_cache.get("message") or (
            "Geometria orbital indisponível "
            f"(status={geometry_cache.get('status')})."
        )
        write_advanced_progress(
            job_root,
            62,
            "geometry_failed",
            "Geometria orbital indisponível",
            message,
            status="failed",
        )
        raise ValueError(message)

    write_advanced_progress(
        job_root,
        62,
        "geometry_done",
        "Cache orbital montado",
        (
            f"source={geometry_cache.get('source')} | "
            f"epochs={geometry_cache.get('epochs_with_geometry')}/"
            f"{geometry_cache.get('epochs_requested')} | "
            f"satellites={geometry_cache.get('satellite_count')}"
        ),
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
            f"phase={bool(cfg.get('interpolate_phase_observables'))} | "
            f"profile={output_profile['profile_name']}"
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
    # Libera as épocas da geometria: daqui em diante só o resumo é usado.
    geometry_cache = _geometry_summary(geometry_cache)
    print(f"[ADV-MEM] after_interpolation max_rss_mb={max_rss_mb():.0f}", flush=True)

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

    pre_ppp_epochs_generated = int(
        advanced_seed_pre_ppp.get("epochs_generated", 0) or 0
    )
    pre_ppp_epochs_with_sat = _count_epochs_with_satellites(advanced_seed_pre_ppp)

    _write_json(
        metadata_root / "advanced_seed_pre_ppp_summary.json",
        {
            "status": advanced_seed_pre_ppp.get("status"),
            "message": advanced_seed_pre_ppp.get("message"),
            "epochs_generated": pre_ppp_epochs_generated,
            "epochs_skipped": advanced_seed_pre_ppp.get("epochs_skipped"),
            "epochs_with_satellites": pre_ppp_epochs_with_sat,
            "satellite_instances": _count_total_satellite_instances(
                advanced_seed_pre_ppp
            ),
            "summary": advanced_seed_pre_ppp.get("summary"),
            "seed_json_path": pre_ppp_seed_json_path,
        },
    )

    print(
        f"[ADV-PIPE] advanced_seed_pre_ppp_epochs_generated={pre_ppp_epochs_generated}",
        flush=True,
    )
    print(
        f"[ADV-PIPE] advanced_seed_pre_ppp_epochs_with_satellites={pre_ppp_epochs_with_sat}",
        flush=True,
    )

    if pre_ppp_epochs_generated <= 0:
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
            f"epochs_generated={pre_ppp_epochs_generated} | "
            f"epochs_with_satellites={pre_ppp_epochs_with_sat}"
        ),
    )

    write_advanced_progress(
        job_root,
        80,
        "ppp_hardening_start",
        "Aplicando endurecimento PPP nas observáveis",
        (
            f"profile={output_profile['profile_name']} | "
            f"systems={','.join(output_profile['allowed_systems'])}"
        ),
    )

    advanced_seed_post_ppp, ppp_filter_diagnostics = harden_advanced_seed_for_ppp(
        seed=advanced_seed_pre_ppp,
        max_satellites=output_profile["max_satellites"],
        min_presence_ratio=output_profile["min_presence_ratio"],
        min_dual_freq_ratio=output_profile["min_dual_freq_ratio"],
        max_gap_epochs=output_profile["max_gap_epochs"],
        min_satellites_per_epoch=output_profile["min_satellites_per_epoch"],
        require_dual_frequency=output_profile["ppp_require_dual_frequency"],
        require_phase_for_dual_frequency=output_profile[
            "ppp_require_phase_for_dual_frequency"
        ],
        prefer_phase_but_do_not_require=output_profile[
            "ppp_prefer_phase_but_do_not_require"
        ],
        priority_obs=["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"],
        allowed_systems=output_profile["allowed_systems"],
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
            "satellite_instances": _count_total_satellite_instances(
                advanced_seed_post_ppp
            ),
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

    final_seed, final_seed_selection = _choose_final_seed(
        pre_ppp_seed=advanced_seed_pre_ppp,
        post_ppp_seed=advanced_seed_post_ppp,
        ppp_filter_diagnostics=ppp_filter_diagnostics,
    )

    # Mantém só o seed escolhido em memória.
    advanced_seed_pre_ppp = None
    advanced_seed_post_ppp = None
    print(f"[ADV-MEM] after_ppp max_rss_mb={max_rss_mb():.0f}", flush=True)

    final_seed_json_path = write_advanced_debug_seed(
        str(result_root / "advanced_seed_final.json"),
        final_seed,
    )

    _write_json(
        metadata_root / "advanced_seed_final_selection.json",
        {
            **final_seed_selection,
            "final_seed_json_path": final_seed_json_path,
        },
    )

    final_epochs_with_sat = _count_epochs_with_satellites(final_seed)

    print(
        f"[ADV-PIPE] final_seed_source={final_seed_selection.get('selected_source')}",
        flush=True,
    )
    print(
        f"[ADV-PIPE] final_epochs_with_satellites={final_epochs_with_sat}",
        flush=True,
    )

    if final_epochs_with_sat <= 0:
        raise ValueError(
            "Nenhuma época utilizável permaneceu após a seleção final do seed avançado."
        )

    validation = validate_advanced_seed(final_seed, cfg)

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
        advanced_seed=final_seed,
        marker_name=(getattr(job, "output_marker_name", None) or "ADVBASE"),
        receiver_brand=cfg["receiver_brand"],
        receiver_model=cfg["receiver_model"],
        receiver_version=cfg["receiver_version"],
        antenna_model=cfg["antenna_model"],
        approx_position_xyz=approx_position_xyz,
        antenna_delta_hen=(0.0, 0.0, 0.0),
        interval_seconds=_extract_interval_seconds(
            final_seed,
            float(cfg.get("target_step_seconds", 1) or 1),
        ),
        require_phase_observables=output_profile["writer_require_phase_observables"],
        output_systems=output_profile["allowed_systems"],
        forced_obs_types=["C1", "P1", "L1", "C2", "P2", "L2"],
    )

    debug_report = {
        "job_id": job.id,
        "processed_at": datetime.utcnow().isoformat() + "Z",
        "config": cfg,
        "output_profile": output_profile,
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
        "geometry_cache": _geometry_summary(geometry_cache),
        "validation": validation,
        "pre_ppp_seed_json_path": pre_ppp_seed_json_path,
        "post_ppp_seed_json_path": post_ppp_seed_json_path,
        "final_seed_json_path": final_seed_json_path,
        "final_seed_selection": final_seed_selection,
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
        "output_profile": output_profile,
        "output_path": output_path,
        "debug_report_path": debug_report_path,
        "pre_ppp_seed_json_path": pre_ppp_seed_json_path,
        "post_ppp_seed_json_path": post_ppp_seed_json_path,
        "final_seed_json_path": final_seed_json_path,
        "final_seed_selection": final_seed_selection,
        "validation": validation,
        "precise_products": precise_products,
        "ppp_filter_diagnostics_path": ppp_filter_diag_path,
    }
