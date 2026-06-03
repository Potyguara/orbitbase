from __future__ import annotations
import math

from pathlib import Path
from shutil import copy2
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import time
import traceback
import re

from app.services.hatanaka_service import convert_hatanaka_to_rinex
from app.services.rinex_service import parse_rinex_observation_metadata
from app.services.coordinates_service import (
    parse_approx_xyz,
    ecef_to_geodetic,
    geodetic_distance,
)
from app.services.vrs_window_service import (
    get_common_time_window,
    validate_common_window,
    format_dt,
)
from app.services.rinex_epochs_service import summarize_epochs
from app.services.rinex_observation_blocks_service import (
    build_network_only_synchronized_observation_blocks,
)
from app.services.rinex_numeric_service import (
    extract_numeric_observations_in_window,
    summarize_numeric_observations,
    build_network_only_virtual_station_seed,
)
from app.services.validate_virtual_rinex_service import (
    validate_virtual_observations,
    write_virtual_validation_report,
)
from app.services.sanitize_virtual_rinex_service import (
    sanitize_virtual_observations,
    summarize_sanitized_virtual_observations,
    write_sanitized_virtual_observations_json,
)

from app.services.vrs_generation_service import (
    build_virtual_observation_seed,
    build_geometry_based_network_vrs_seed,
    summarize_virtual_observation_seed,
    filter_seed_by_selected_epochs,
    write_virtual_observations_json,
    write_virtual_rinex_v2_obs,
    write_virtual_rinex_v3_obs,
    harden_ppp_seed_for_ibge,
    _geodetic_to_ecef,
    physically_filter_ppp_seed,
)

from app.services.vrs_ppp_block_selector_service import (
    select_best_continuous_ppp_block,
)
from app.services.vrs_offset_interpolation_service import (
    compute_station_coordinate_offset,
    interpolate_vrs_offset,
    apply_interpolated_offset_to_target_xyz,
)
from app.services.vrs_profile_service import normalize_vrs_config
from app.services.rinex_ppp_safe_service import build_ppp_safe_rinex_from_reference_base
from itertools import combinations


from app.services.vrs_ppp_filter_service import (
    generate_ppp_observation_diagnostics,
    write_ppp_observation_diagnostics,
    filter_ppp_seed_by_quality,
)

from app.services.vrs_relative_correction_service import (
    build_relative_correction_vrs_seed,
    build_satellite_xyz_cache_from_rinex_nav,
)

from app.services.vrs_physical_geometry_service import (
    build_physical_geometry_vrs_seed_v4,
)

from app.services.rinex_nav_satpos_service import (
    detect_navigation_file_near_observation,
    build_satellite_position_cache_from_nav,
)

from app.services.rbmc_observation_sync_service import ensure_rbmc_station_files

LOCAL_TIMEZONE = "America/Belem"


def _debug(step: str, message: str) -> None:
    print(f"[VRS][{step}] {message}", flush=True)


def _timed(step: str):
    start = time.perf_counter()

    def done(extra: str = ""):
        elapsed = time.perf_counter() - start
        suffix = f" | {extra}" if extra else ""
        print(f"[VRS][{step}] concluído em {elapsed:.2f}s{suffix}", flush=True)

    return done


def _extract_candidate_path(value) -> str:
    if value is None:
        return ""

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        return (
            value.get("file_path")
            or value.get("source_file")
            or value.get("path")
            or value.get("stored_path")
            or ""
        )

    return ""


def safe_file_name(value) -> str:
    candidate = _extract_candidate_path(value)

    if candidate:
        return Path(candidate).name

    if isinstance(value, dict):
        return value.get("file_name") or ""

    return ""


def safe_path_str(value) -> str:
    candidate = _extract_candidate_path(value)
    return candidate or ""


def _copy_if_different(src: str | Path, dst: str | Path) -> Path:
    src_path = Path(src)
    dst_path = Path(dst)

    try:
        if src_path.exists() and dst_path.exists() and src_path.samefile(dst_path):
            return dst_path
    except Exception:
        pass

    try:
        if str(src_path.resolve()).lower() == str(dst_path.resolve()).lower():
            return dst_path
    except Exception:
        pass

    copy2(src_path, dst_path)
    return dst_path


def _safe_rinex_output_name(value: str | None, default: str = "BASE") -> str:
    if not value:
        return default

    cleaned = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in value.strip()
    ).strip("_")

    return cleaned or default


def _safe_marker_name(value: str | None, default: str) -> str:
    if not value:
        return default

    cleaned = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in value.strip().upper()
    ).strip("_")

    return (cleaned or default)[:20]


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in value)


def _find_navigation_files_for_base(original_path: Path) -> list[dict]:
    result: list[dict] = []

    day_root = (
        original_path.parent.parent
        if original_path.parent.name in {"converted", "extracted", "raw"}
        else original_path.parent
    )

    search_roots = [
        original_path.parent,
        day_root,
        day_root / "converted",
        day_root / "extracted",
        day_root / "raw",
    ]

    seen_roots = set()
    unique_roots = []
    for root in search_roots:
        try:
            key = str(root.resolve())
        except Exception:
            key = str(root)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        unique_roots.append(root)

    for root in unique_roots:
        if not root.exists():
            continue

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            lower = path.name.lower()

            if (
                lower.endswith(".nav")
                or re.search(r"\.\d{2}[ngl]$", lower)
                or "brdc" in lower
            ):
                result.append(
                    {
                        "file_name": path.name,
                        "file_path": str(path),
                        "source": original_path.name,
                    }
                )

    unique = []
    seen = set()
    for item in result:
        key = item["file_path"]
        if key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(
        key=lambda item: (
            (
                0
                if re.search(r"\.\d{2}n$", item["file_name"].lower())
                else (
                    1
                    if re.search(r"\.\d{2}g$", item["file_name"].lower())
                    else 2 if re.search(r"\.\d{2}l$", item["file_name"].lower()) else 3
                )
            ),
            item["file_name"].lower(),
        )
    )

    return unique


def detect_navigation_file_for_base(base_meta: dict) -> str | None:
    """
    Localiza o arquivo NAV mais provável para uma base,
    priorizando os diretórios originais da RBMC e aceitando
    padrões comuns de RINEX de navegação.
    """
    candidate_dirs = []

    original_path = base_meta.get("original_path")
    rinex_path = base_meta.get("rinex_path")
    copied_path = base_meta.get("copied_path")

    for p in [original_path, rinex_path, copied_path]:
        if not p:
            continue
        try:
            parent = Path(p).resolve().parent
            if parent.exists() and parent.is_dir():
                candidate_dirs.append(parent)
        except Exception:
            continue

    unique_dirs = []
    seen = set()
    for d in candidate_dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            unique_dirs.append(d)

    if not unique_dirs:
        return None

    station_code = (base_meta.get("station_code") or "").strip().lower()
    file_name = (base_meta.get("file_name") or "").strip().lower()
    stem = Path(file_name).stem.lower() if file_name else ""

    patterns = []

    if station_code:
        patterns.extend(
            [
                f"{station_code}*.nav",
                f"{station_code}*.NAV",
                f"{station_code}*.rnx",
                f"{station_code}*.RNX",
                f"{station_code}*.??n",
                f"{station_code}*.??N",
                f"{station_code}*.??g",
                f"{station_code}*.??G",
                f"{station_code}*.??l",
                f"{station_code}*.??L",
            ]
        )

    if stem:
        patterns.extend(
            [
                f"{stem}*.nav",
                f"{stem}*.NAV",
                f"{stem}*.rnx",
                f"{stem}*.RNX",
                f"{stem}*.??n",
                f"{stem}*.??N",
                f"{stem}*.??g",
                f"{stem}*.??G",
                f"{stem}*.??l",
                f"{stem}*.??L",
            ]
        )

    patterns.extend(
        [
            "*BRDC*MN*.rnx",
            "*brdc*mn*.rnx",
            "*BRDC*.rnx",
            "*brdc*.rnx",
            "*.nav",
            "*.NAV",
            "*.rnx",
            "*.RNX",
            "*.??n",
            "*.??N",
            "*.??g",
            "*.??G",
            "*.??l",
            "*.??L",
        ]
    )

    for base_dir in unique_dirs:
        for pattern in patterns:
            matches = sorted(base_dir.glob(pattern))
            for m in matches:
                if not m.is_file():
                    continue

                name_lower = m.name.lower()
                suffix_lower = m.suffix.lower()

                if suffix_lower in {".o", ".obs"}:
                    continue

                if (
                    suffix_lower in {".nav", ".rnx"}
                    or name_lower.endswith("n")
                    or ".26n" in name_lower
                    or ".25n" in name_lower
                    or ".24n" in name_lower
                    or ".23n" in name_lower
                    or ".26g" in name_lower
                    or ".25g" in name_lower
                    or ".24g" in name_lower
                    or ".23g" in name_lower
                    or ".26l" in name_lower
                    or ".25l" in name_lower
                    or ".24l" in name_lower
                    or ".23l" in name_lower
                    or "brdc" in name_lower
                ):
                    return str(m)

    return None


def _extract_station_meta(
    file_path: Path, label: str, station_code: str | None = None
) -> dict:
    metadata = parse_rinex_observation_metadata(str(file_path))

    xyz_text = metadata.get("approx_position_text")
    x, y, z = parse_approx_xyz(xyz_text)
    lat, lon, h = ecef_to_geodetic(x, y, z)

    return {
        "label": label,
        "station_code": station_code,
        "file_path": str(file_path),
        "file_name": file_path.name,
        "marker_name": metadata.get("marker_name"),
        "rinex_version": metadata.get("rinex_version"),
        "approx_position_text": xyz_text,
        "xyz": {"x": x, "y": y, "z": z},
        "geodetic": {"lat": lat, "lon": lon, "height": h},
        "interval_seconds": metadata.get("interval_seconds"),
        "first_epoch_text": metadata.get("first_epoch_text"),
        "last_epoch_text": metadata.get("last_epoch_text"),
        "line_count": metadata.get("line_count"),
        "data_line_count": metadata.get("data_line_count"),
    }


def _select_reference_base(
    bases_meta: list[dict], target_lat: float, target_lon: float
) -> dict:
    enriched = []

    for base in bases_meta:
        distance_m = geodetic_distance(
            target_lat,
            target_lon,
            base["geodetic"]["lat"],
            base["geodetic"]["lon"],
        )
        clone = dict(base)
        clone["distance_to_target_m"] = distance_m
        enriched.append(clone)

    if not enriched:
        raise ValueError(
            "Nenhuma base válida foi encontrada para seleção da base de referência."
        )

    enriched.sort(key=lambda item: item.get("distance_to_target_m", float("inf")))
    return enriched[0]


def _build_requested_window_utc(job) -> tuple[datetime, datetime, datetime, datetime]:
    if not job.session_date or not job.start_time or not job.end_time:
        raise ValueError(
            "Data da sessão, hora inicial e hora final são obrigatórias para o processamento."
        )

    tz = ZoneInfo(LOCAL_TIMEZONE)

    start_local = datetime.fromisoformat(f"{job.session_date}T{job.start_time}:00")
    end_local = datetime.fromisoformat(f"{job.session_date}T{job.end_time}:00")

    if end_local <= start_local:
        raise ValueError("A hora final deve ser maior que a hora inicial.")

    start_local = start_local.replace(tzinfo=tz)
    end_local = end_local.replace(tzinfo=tz)

    requested_duration_min = int((end_local - start_local).total_seconds() / 60)

    if job.occupation_minutes is not None and requested_duration_min < int(
        job.occupation_minutes
    ):
        raise ValueError(
            "A janela informada é menor que o tempo de ocupação solicitado."
        )

    if job.requested_minutes and requested_duration_min < int(job.requested_minutes):
        raise ValueError("A janela informada é menor que os minutos solicitados.")

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    return (
        start_local.replace(tzinfo=None),
        end_local.replace(tzinfo=None),
        start_utc,
        end_utc,
    )


def _load_selected_station_codes_from_job(job) -> list[str]:
    raw = getattr(job, "selected_station_codes_json", None)
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    cleaned = []
    seen = set()
    for item in parsed:
        if item is None:
            continue
        code = str(item).strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned.append(code)

    return cleaned


def _resolve_rbmc_station_observation_path(
    station_code: str,
    session_date: str,
) -> Path | None:
    try:
        state = ensure_rbmc_station_files(station_code, session_date)
        best_obs = state.get("best_observation_file")
        if best_obs:
            return Path(best_obs)
        return None
    except Exception as exc:
        print(
            f"[RBMC-RESOLVE] falha ao garantir arquivos de {station_code}/{session_date}: {exc}",
            flush=True,
        )
        return None


def _resolve_candidate_base_paths(job) -> list[dict]:
    selected_station_codes = _load_selected_station_codes_from_job(job)
    candidates: list[dict] = []

    for code in selected_station_codes:
        path = _resolve_rbmc_station_observation_path(code, job.session_date)
        if path is None:
            continue

        candidates.append(
            {
                "station_code": code,
                "label": f"rbmc_{code}",
                "path": path,
                "source_type": "rbmc_map_selection",
            }
        )

    return candidates


def _prepare_candidate_files(
    candidate_paths: list[dict], input_root: Path
) -> list[dict]:
    prepared = []

    for idx, item in enumerate(candidate_paths, start=1):
        original_path = Path(item["path"])
        rinex_path = Path(convert_hatanaka_to_rinex(str(original_path)))

        copied_path = input_root / f"cand{idx}_{_safe_name(rinex_path.name)}"
        _copy_if_different(rinex_path, copied_path)

        prepared.append(
            {
                **item,
                "original_path": original_path,
                "rinex_path": rinex_path,
                "copied_path": copied_path,
            }
        )

    return prepared


def _score_candidate_base(
    base_meta: dict,
    target: dict,
    requested_start_utc: datetime,
    requested_end_utc: datetime,
) -> dict:
    distance_m = geodetic_distance(
        target["lat"],
        target["lon"],
        base_meta["geodetic"]["lat"],
        base_meta["geodetic"]["lon"],
    )

    first_text = base_meta.get("first_epoch_text")
    last_text = base_meta.get("last_epoch_text")

    coverage_ok = True
    try:
        first_dt = (
            datetime.fromisoformat(str(first_text).replace("Z", "").strip())
            if first_text
            else None
        )
        last_dt = (
            datetime.fromisoformat(str(last_text).replace("Z", "").strip())
            if last_text
            else None
        )
        if first_dt and requested_start_utc < first_dt:
            coverage_ok = False
        if last_dt and requested_end_utc > last_dt:
            coverage_ok = False
    except Exception:
        coverage_ok = True

    interval = base_meta.get("interval_seconds") or 999999

    score = 0.0
    score += 1000.0 if coverage_ok else 0.0
    score += max(0.0, 500000.0 / max(distance_m, 1.0))
    score += max(0.0, 100.0 / max(interval, 1.0))

    return {
        "distance_m": distance_m,
        "coverage_ok": coverage_ok,
        "interval_seconds": interval,
        "score": score,
    }


def _local_ne_m(
    ref_lat_deg: float,
    ref_lon_deg: float,
    lat_deg: float,
    lon_deg: float,
) -> tuple[float, float]:
    """
    Projeção local simples em metros no plano Norte-Leste, suficiente
    para seleção geométrica do triângulo em torno do alvo.
    """
    r = 6378137.0
    ref_lat = math.radians(ref_lat_deg)

    dlat = math.radians(lat_deg - ref_lat_deg)
    dlon = math.radians(lon_deg - ref_lon_deg)

    north = dlat * r
    east = dlon * r * math.cos(ref_lat)

    return north, east


def _triangle_area2(p1, p2, p3) -> float:
    return abs((p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0]))


def _point_in_triangle_barycentric(
    pt, a, b, c
) -> tuple[bool, tuple[float, float, float]]:
    area_abc = _triangle_area2(a, b, c)
    if area_abc <= 1e-9:
        return False, (0.0, 0.0, 0.0)

    area_pbc = _triangle_area2(pt, b, c)
    area_apc = _triangle_area2(a, pt, c)
    area_abp = _triangle_area2(a, b, pt)

    l1 = area_pbc / area_abc
    l2 = area_apc / area_abc
    l3 = area_abp / area_abc

    s = l1 + l2 + l3
    if s <= 0:
        return False, (0.0, 0.0, 0.0)

    l1 /= s
    l2 /= s
    l3 /= s

    inside = (
        -1e-6 <= l1 <= 1.000001 and -1e-6 <= l2 <= 1.000001 and -1e-6 <= l3 <= 1.000001
    )

    return inside, (l1, l2, l3)


def _select_best_base_triplet(
    candidate_metas: list[dict],
    target: dict,
    requested_start_utc: datetime,
    requested_end_utc: datetime,
) -> list[dict]:
    """
    Seleção inteligente:
    1) prioriza triângulo que contenha o alvo;
    2) evita bases quase colineares;
    3) entre opções válidas, escolhe menor distância média ao alvo.
    """
    if len(candidate_metas) < 3:
        raise ValueError(
            "Não há bases candidatas suficientes para formar a rede mínima. "
            "Selecione no mínimo 3 estações válidas."
        )

    scored = []
    for meta in candidate_metas:
        metrics = _score_candidate_base(
            meta,
            target,
            requested_start_utc,
            requested_end_utc,
        )
        enriched = dict(meta)
        enriched.update(metrics)
        scored.append(enriched)

    valid = [m for m in scored if m.get("coverage_ok")]
    if len(valid) < 3:
        valid = scored[:]

    target_pt = (0.0, 0.0)

    best_triplet = None
    best_key = None

    for triplet in combinations(valid, 3):
        b1, b2, b3 = triplet

        p1 = _local_ne_m(
            target["lat"], target["lon"], b1["geodetic"]["lat"], b1["geodetic"]["lon"]
        )
        p2 = _local_ne_m(
            target["lat"], target["lon"], b2["geodetic"]["lat"], b2["geodetic"]["lon"]
        )
        p3 = _local_ne_m(
            target["lat"], target["lon"], b3["geodetic"]["lat"], b3["geodetic"]["lon"]
        )

        area2 = _triangle_area2(p1, p2, p3)
        if area2 < 10.0:
            # triângulo degenerado / quase colinear
            continue

        inside, bary = _point_in_triangle_barycentric(target_pt, p1, p2, p3)

        mean_distance = (b1["distance_m"] + b2["distance_m"] + b3["distance_m"]) / 3.0

        max_distance = max(b1["distance_m"], b2["distance_m"], b3["distance_m"])

        # maior área é melhor; menor distância média é melhor
        # inside=True ganha prioridade máxima
        key = (
            0 if inside else 1,
            mean_distance,
            max_distance,
            -area2,
        )

        if best_key is None or key < best_key:
            best_key = key
            best_triplet = [dict(b1), dict(b2), dict(b3)]
            best_triplet[0]["triangle_ne"] = p1
            best_triplet[1]["triangle_ne"] = p2
            best_triplet[2]["triangle_ne"] = p3
            best_triplet[0]["triangle_contains_target"] = inside
            best_triplet[0]["triangle_barycentric"] = bary
            best_triplet[0]["triangle_area2"] = area2

    if best_triplet is None:
        valid.sort(
            key=lambda item: (
                not item["coverage_ok"],
                item["distance_m"],
                item["interval_seconds"],
            )
        )
        best_triplet = valid[:3]

    # reordena: a mais próxima vira mestre
    best_triplet.sort(key=lambda item: item.get("distance_m", float("inf")))
    return best_triplet


def _write_progress_file(
    job_root: Path,
    percent: int,
    step: str,
    message: str,
    detail: str | None = None,
    extra: dict | None = None,
) -> None:
    payload = {
        "percent": max(0, min(100, int(percent))),
        "step": step,
        "message": message,
        "detail": detail or "",
        "extra": extra or {},
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    progress_path = job_root / "progress.json"
    progress_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _progress(
    job_root: Path, percent: int, step: str, message: str, detail: str | None = None
):
    progress_path = Path(job_root) / "progress.json"
    payload = {
        "percent": int(percent),
        "step": step,
        "message": message,
        "detail": detail,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "status": (
            "running"
            if step not in {"done", "error"}
            else ("completed" if step == "done" else "failed")
        ),
    }
    progress_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        f"[VRS][PROGRESS-{int(percent):03d}] {step} | {message} | {detail or '-'}",
        flush=True,
    )


def _resolve_nav_file_from_obs_path(obs_path: Path | str) -> Path | None:
    obs_path = Path(obs_path)

    print(f"[DEBUG-NAV-RESOLVE] obs_path={obs_path}", flush=True)

    candidates = []

    # sobe até a pasta da data, ex:
    # storage/rbmc/spar/2026-04-24/converted/spar1141.26o
    # -> root esperado: storage/rbmc/spar/2026-04-24
    date_root = (
        obs_path.parent.parent
        if obs_path.parent.name == "converted"
        else obs_path.parent
    )

    print(f"[DEBUG-NAV-RESOLVE] date_root={date_root}", flush=True)

    search_dirs = [
        date_root / "extracted",
        date_root / "converted",
        date_root / "raw",
        date_root,
    ]

    nav_suffixes = (".n", ".g", ".l", ".26n", ".26g", ".26l", ".25n", ".24n", ".23n")

    for directory in search_dirs:
        if not directory.exists():
            continue

        for path in directory.iterdir():
            if not path.is_file():
                continue

            lower = path.name.lower()
            if lower.endswith(nav_suffixes):
                candidates.append(path)

    # deduplicação preservando ordem
    unique = []
    seen = set()
    for p in candidates:
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        unique.append(p)

    print(
        f"[DEBUG-NAV-RESOLVE] candidates={[str(p) for p in unique]}",
        flush=True,
    )

    if not unique:
        return None

    # prioridade para .n
    unique.sort(
        key=lambda p: (
            (
                0
                if p.name.lower().endswith(".26n") or p.name.lower().endswith(".n")
                else 1
            ),
            p.name.lower(),
        )
    )

    chosen = unique[0]
    print(f"[DEBUG-NAV-RESOLVE] chosen={chosen}", flush=True)
    return chosen


def process_real_vrs(job, profile_config: dict | None = None) -> dict:
    whole_timer = _timed("TOTAL")
    _debug("00", f"Iniciando processamento do job={job.id}")

    try:
        config_timer = _timed("01-CONFIG")
        config = normalize_vrs_config(profile_config)
        config_timer()

        apply_interpolated_station_offsets = config.get(
            "apply_interpolated_station_offsets", False
        )
        offset_distance_power = config.get("offset_distance_power", 2.0)
        offset_interpolation_min_stations = config.get(
            "offset_interpolation_min_stations", 2
        )
        max_station_offset_m = config.get("max_station_offset_m", 0.50)
        offset_apply_quality_factor = config.get("offset_apply_quality_factor", True)

        min_bases_per_observation = config.get("min_bases_per_observation", 2)
        min_satellites_per_epoch = config.get("min_satellites_per_epoch", 5)
        max_satellites_per_epoch = config.get("max_satellites_per_epoch", 10)
        min_satellite_presence_ratio = config.get("min_satellite_presence_ratio", 0.45)
        min_satellite_longest_arc = config.get("min_satellite_longest_arc", 80)
        local_continuity_window = config.get("local_continuity_window", 2)
        min_valid_in_local_window = config.get("min_valid_in_local_window", 4)
        prefer_nearest_base = config.get("prefer_nearest_base", True)
        strong_nearest_base_bias = config.get("strong_nearest_base_bias", True)
        require_dual_frequency = config.get("require_dual_frequency", True)

        priority_obs = config.get(
            "priority_obs",
            ["P1", "C1", "L1", "P2", "C2", "L2", "C5", "S1", "S2", "S5"],
        )

        preserve_full_user_window = config.get("preserve_full_user_window", True)
        generate_ppp_aux_file = config.get("generate_ppp_aux_file", True)
        ppp_trials = config.get("ppp_trials", [])

        if not ppp_trials:
            ppp_trials = [
                {
                    "strategy": "strict",
                    "min_consecutive_epochs": 120,
                    "min_satellites_per_epoch": 8,
                    "min_priority_obs_per_satellite_avg": 4.0,
                    "max_gap_factor": 2.0,
                },
                {
                    "strategy": "balanced",
                    "min_consecutive_epochs": 60,
                    "min_satellites_per_epoch": 6,
                    "min_priority_obs_per_satellite_avg": 3.0,
                    "max_gap_factor": 2.5,
                },
                {
                    "strategy": "short_rover",
                    "min_consecutive_epochs": 20,
                    "min_satellites_per_epoch": 4,
                    "min_priority_obs_per_satellite_avg": 2.0,
                    "max_gap_factor": 3.0,
                },
            ]

        if job.network_validation_status != "completed":
            raise ValueError(
                "A rede precisa estar validada com sucesso antes do processamento."
            )

        if (
            job.target_lat is None
            or job.target_lon is None
            or job.target_height is None
        ):
            raise ValueError(
                "Coordenadas alvo VRS incompletas. Informe latitude, longitude e altitude."
            )

        window_timer = _timed("02-WINDOW")
        (
            requested_start_local,
            requested_end_local,
            requested_start_utc,
            requested_end_utc,
        ) = _build_requested_window_utc(job)
        window_timer(
            f"{requested_start_utc.isoformat()} -> {requested_end_utc.isoformat()}"
        )

        target = {
            "lat": job.target_lat,
            "lon": job.target_lon,
            "height": job.target_height,
        }

        target_xyz_timer = _timed("03-TARGET-XYZ")
        tx, ty, tz = _geodetic_to_ecef(
            target["lat"],
            target["lon"],
            target["height"],
        )
        target_xyz_nominal = {"x": tx, "y": ty, "z": tz}
        target_xyz_timer()

        storage_root = Path(__file__).resolve().parents[2] / "storage"
        job_root = storage_root / "processing" / f"job_{job.id}"
        input_root = job_root / "inputs"
        metadata_root = job_root / "metadata"
        result_root = job_root / "results"
        log_root = job_root / "logs"

        fs_timer = _timed("04-FS")
        input_root.mkdir(parents=True, exist_ok=True)
        metadata_root.mkdir(parents=True, exist_ok=True)
        result_root.mkdir(parents=True, exist_ok=True)
        log_root.mkdir(parents=True, exist_ok=True)
        fs_timer(str(job_root))

        selected_station_codes = _load_selected_station_codes_from_job(job)
        _debug("05", f"Estações selecionadas: {selected_station_codes}")

        _progress(
            job_root,
            2,
            "start",
            "Iniciando processamento VRS",
            f"job={job.id}",
        )
        candidate_timer = _timed("06-RESOLVE-CANDIDATES")

        candidate_paths = _resolve_candidate_base_paths(job)
        candidate_timer(f"{len(candidate_paths)} candidatas")

        if len(candidate_paths) < 3:
            raise ValueError(
                "Não foi possível montar a rede com as estações selecionadas. "
                "Selecione ao menos 3 estações RBMC válidas no mapa."
            )

        _progress(
            job_root,
            10,
            "resolve_candidates",
            "Bases candidatas resolvidas",
            f"{len(candidate_paths)} candidatas",
        )

        prepare_timer = _timed("07-PREPARE-CANDIDATES")

        prepared_candidates = _prepare_candidate_files(candidate_paths, input_root)

        prepare_timer(f"{len(prepared_candidates)} preparadas")

        _progress(
            job_root,
            16,
            "prepare_candidates",
            "Arquivos candidatos preparados",
            f"{len(prepared_candidates)} preparadas",
        )

        meta_timer = _timed("08-EXTRACT-METAS")
        candidate_metas = []
        all_navigation_files = []

        for idx, item in enumerate(prepared_candidates, start=1):
            _debug("08A", f"Lendo meta base {idx}: {item['copied_path']}")
            meta = _extract_station_meta(
                item["copied_path"],
                label=item["label"],
                station_code=item.get("station_code"),
            )
            meta["source_type"] = item.get("source_type")
            meta["original_path"] = str(item["original_path"])
            meta["copied_path"] = str(item["copied_path"])
            meta["rinex_path"] = str(item["rinex_path"])

            candidate_metas.append(meta)
            all_navigation_files.extend(
                _find_navigation_files_for_base(item["original_path"])
            )

        meta_timer(f"{len(candidate_metas)} metas lidas")

        _progress(
            job_root,
            22,
            "extract_meta",
            "Metadados das bases lidos",
            f"{len(candidate_metas)} bases válidas",
        )

        unique_nav = []
        seen_nav = set()
        for item in all_navigation_files:
            key = item["file_path"]
            if key in seen_nav:
                continue
            seen_nav.add(key)
            unique_nav.append(item)
        navigation_files = unique_nav

        def _candidate_distance_score(base_meta: dict) -> float:
            return geodetic_distance(
                target["lat"],
                target["lon"],
                base_meta["geodetic"]["lat"],
                base_meta["geodetic"]["lon"],
            )

        def _triangle_area_score(master_meta: dict, aux_a: dict, aux_b: dict) -> float:
            # aproximação plana local suficiente para ranking geométrico
            mx = master_meta["geodetic"]["lon"] - target["lon"]
            my = master_meta["geodetic"]["lat"] - target["lat"]

            ax = aux_a["geodetic"]["lon"] - target["lon"]
            ay = aux_a["geodetic"]["lat"] - target["lat"]

            bx = aux_b["geodetic"]["lon"] - target["lon"]
            by = aux_b["geodetic"]["lat"] - target["lat"]

            # área do paralelogramo entre vetores TA e TB
            area = abs(ax * by - ay * bx)

            # penaliza auxiliares excessivamente distantes
            da = _candidate_distance_score(aux_a)
            db = _candidate_distance_score(aux_b)
            dist_penalty = 1.0 + (da / 200000.0) + (db / 200000.0)

            # penaliza bases muito coladas no mestre
            ma = geodetic_distance(
                master_meta["geodetic"]["lat"],
                master_meta["geodetic"]["lon"],
                aux_a["geodetic"]["lat"],
                aux_a["geodetic"]["lon"],
            )
            mb = geodetic_distance(
                master_meta["geodetic"]["lat"],
                master_meta["geodetic"]["lon"],
                aux_b["geodetic"]["lat"],
                aux_b["geodetic"]["lon"],
            )

            spread_bonus = (
                1.0 + min(ma, 300000.0) / 300000.0 + min(mb, 300000.0) / 300000.0
            )
            return (area * spread_bonus) / dist_penalty

        def _select_best_geometry_triplet(all_bases: list[dict]) -> list[dict]:
            if len(all_bases) < 3:
                raise ValueError(
                    "Menos de 3 bases disponíveis para composição geométrica."
                )

            enriched = []
            for base in all_bases:
                clone = dict(base)
                clone["distance_to_target_m"] = _candidate_distance_score(base)
                enriched.append(clone)

            enriched.sort(key=lambda item: item["distance_to_target_m"])

            # mestre = sempre a mais próxima do alvo
            master = enriched[0]

            others = enriched[1:]
            if len(others) < 2:
                raise ValueError(
                    "Não há bases auxiliares suficientes para compor a geometria."
                )

            best_pair = None
            best_score = -1.0

            for i in range(len(others)):
                for j in range(i + 1, len(others)):
                    aux_a = others[i]
                    aux_b = others[j]
                    score = _triangle_area_score(master, aux_a, aux_b)
                    if score > best_score:
                        best_score = score
                        best_pair = (aux_a, aux_b)

            if best_pair is None:
                raise ValueError(
                    "Falha ao selecionar o melhor par geométrico de auxiliares."
                )

            aux1, aux2 = best_pair
            return [master, aux1, aux2]

        select_triplet_timer = _timed("09-SELECT-TRIPLET")
        selected_bases = _select_best_geometry_triplet(candidate_metas)
        select_triplet_timer()

        base1_meta, base2_meta, base3_meta = selected_bases

        for base in (base1_meta, base2_meta, base3_meta):
            base["distance_to_target_m"] = geodetic_distance(
                target["lat"],
                target["lon"],
                base["geodetic"]["lat"],
                base["geodetic"]["lon"],
            )

        base1_copy = Path(base1_meta["copied_path"])
        base2_copy = Path(base2_meta["copied_path"])
        base3_copy = Path(base3_meta["copied_path"])

        base1_path = Path(base1_meta["original_path"])
        base2_path = Path(base2_meta["original_path"])
        base3_path = Path(base3_meta["original_path"])

        base1_rinex = Path(base1_meta["rinex_path"])
        base2_rinex = Path(base2_meta["rinex_path"])
        base3_rinex = Path(base3_meta["rinex_path"])

        def _resolve_processed_xyz(base_meta: dict) -> dict:
            refined = base_meta.get("xyz_refined")
            if isinstance(refined, dict):
                return refined

            return {
                "x": base_meta["xyz"]["x"],
                "y": base_meta["xyz"]["y"],
                "z": base_meta["xyz"]["z"],
            }

        _progress(
            job_root,
            28,
            "select_network",
            "Rede principal selecionada",
            f"{base1_meta.get('station_code')} / {base2_meta.get('station_code')} / {base3_meta.get('station_code')}",
        )

        offset_timer = _timed("10-OFFSETS")
        base1_offset = compute_station_coordinate_offset(
            base1_meta["xyz"],
            _resolve_processed_xyz(base1_meta),
        )
        base2_offset = compute_station_coordinate_offset(
            base2_meta["xyz"],
            _resolve_processed_xyz(base2_meta),
        )
        base3_offset = compute_station_coordinate_offset(
            base3_meta["xyz"],
            _resolve_processed_xyz(base3_meta),
        )
        offset_timer()

        ref_timer = _timed("11-REFERENCE-BASE")
        reference_base = base1_meta
        ref_timer(reference_base.get("file_name", "-"))

        common_timer = _timed("12-COMMON-WINDOW")
        common_window = get_common_time_window(
            [str(base1_copy), str(base2_copy), str(base3_copy)]
        )
        validate_common_window(common_window, job.requested_minutes)
        common_timer(
            f"{common_window['common_start'].isoformat()} -> {common_window['common_end'].isoformat()}"
        )

        effective_timer = _timed("13-EFFECTIVE-WINDOW")
        effective_start_utc = max(common_window["common_start"], requested_start_utc)
        effective_end_utc = min(common_window["common_end"], requested_end_utc)

        if effective_end_utc <= effective_start_utc:
            raise ValueError(
                "A janela solicitada não está contida na janela comum disponível entre as bases."
            )

        effective_duration_min = int(
            (effective_end_utc - effective_start_utc).total_seconds() / 60
        )

        if job.occupation_minutes is not None and effective_duration_min < int(
            job.occupation_minutes
        ):
            raise ValueError(
                "A janela efetiva disponível entre as bases é menor que o tempo de ocupação solicitado."
            )

        if job.requested_minutes and effective_duration_min < int(
            job.requested_minutes
        ):
            raise ValueError(
                "A janela efetiva disponível entre as bases é menor que os minutos solicitados."
            )

        effective_timer(f"{effective_duration_min} min")

        _progress(
            job_root,
            34,
            "effective_window",
            "Janela efetiva definida",
            f"{effective_duration_min} min",
        )

        epoch_summary_timer = _timed("14-EPOCH-SUMMARY")
        base1_epoch_summary = summarize_epochs(
            str(base1_copy),
            effective_start_utc,
            effective_end_utc,
        )
        base2_epoch_summary = summarize_epochs(
            str(base2_copy),
            effective_start_utc,
            effective_end_utc,
        )
        base3_epoch_summary = summarize_epochs(
            str(base3_copy),
            effective_start_utc,
            effective_end_utc,
        )
        epoch_summary_timer()

        blocks_timer = _timed("15-SYNC-BLOCKS")
        synchronized_observations = build_network_only_synchronized_observation_blocks(
            str(base1_copy),
            str(base2_copy),
            str(base3_copy),
            effective_start_utc,
            effective_end_utc,
        )
        blocks_timer()

        base1_sat_xyz_cache = {}
        base2_sat_xyz_cache = {}
        base3_sat_xyz_cache = {}

        base1_nav_path = _resolve_nav_file_from_obs_path(base1_path)
        base2_nav_path = _resolve_nav_file_from_obs_path(base2_path)
        base3_nav_path = _resolve_nav_file_from_obs_path(base3_path)

        print(f"[DEBUG-NAV-ROOT] base1_path={base1_path}", flush=True)
        print(f"[DEBUG-NAV-ROOT] base2_path={base2_path}", flush=True)
        print(f"[DEBUG-NAV-ROOT] base3_path={base3_path}", flush=True)

        print(f"[DEBUG-NAV] base1_nav_path={base1_nav_path}", flush=True)
        print(f"[DEBUG-NAV] base2_nav_path={base2_nav_path}", flush=True)
        print(f"[DEBUG-NAV] base3_nav_path={base3_nav_path}", flush=True)

        if base1_nav_path:
            base1_sat_xyz_cache = build_satellite_xyz_cache_from_rinex_nav(
                nav_file_path=str(base1_nav_path),
                start_dt=effective_start_utc,
                end_dt=effective_end_utc,
                step_seconds=15,
            )

        if base2_nav_path:
            base2_sat_xyz_cache = build_satellite_xyz_cache_from_rinex_nav(
                nav_file_path=str(base2_nav_path),
                start_dt=effective_start_utc,
                end_dt=effective_end_utc,
                step_seconds=15,
            )

        if base3_nav_path:
            base3_sat_xyz_cache = build_satellite_xyz_cache_from_rinex_nav(
                nav_file_path=str(base3_nav_path),
                start_dt=effective_start_utc,
                end_dt=effective_end_utc,
                step_seconds=15,
            )

        print(
            f"[DEBUG-NAV] cache sizes => "
            f"base1={len(base1_sat_xyz_cache)}, "
            f"base2={len(base2_sat_xyz_cache)}, "
            f"base3={len(base3_sat_xyz_cache)}",
            flush=True,
        )

        print(
            f"[DEBUG-NAV] base1 first epoch={sorted(base1_sat_xyz_cache.keys())[0] if base1_sat_xyz_cache else None}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV] base2 first epoch={sorted(base2_sat_xyz_cache.keys())[0] if base2_sat_xyz_cache else None}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV] base3 first epoch={sorted(base3_sat_xyz_cache.keys())[0] if base3_sat_xyz_cache else None}",
            flush=True,
        )

        if base1_sat_xyz_cache:
            first_epoch_key = sorted(base1_sat_xyz_cache.keys())[0]
            first_epoch_bucket = base1_sat_xyz_cache[first_epoch_key]
            print(
                f"[DEBUG-NAV] base1 first bucket sample sats={list(first_epoch_bucket.keys())[:15]}",
                flush=True,
            )

        if base2_sat_xyz_cache:
            first_epoch_key = sorted(base2_sat_xyz_cache.keys())[0]
            first_epoch_bucket = base2_sat_xyz_cache[first_epoch_key]
            print(
                f"[DEBUG-NAV] base2 first bucket sample sats={list(first_epoch_bucket.keys())[:15]}",
                flush=True,
            )

        if base3_sat_xyz_cache:
            first_epoch_key = sorted(base3_sat_xyz_cache.keys())[0]
            first_epoch_bucket = base3_sat_xyz_cache[first_epoch_key]
            print(
                f"[DEBUG-NAV] base3 first bucket sample sats={list(first_epoch_bucket.keys())[:15]}",
                flush=True,
            )

        print(
            f"[DEBUG-NAV] cache sizes => "
            f"base1={len(base1_sat_xyz_cache)}, "
            f"base2={len(base2_sat_xyz_cache)}, "
            f"base3={len(base3_sat_xyz_cache)}",
            flush=True,
        )

        numeric_timer = _timed("16-NUMERIC-EXTRACT")

        base1_numeric = extract_numeric_observations_in_window(
            str(base1_copy),
            effective_start_utc,
            effective_end_utc,
            max_epochs=None,
            sat_xyz_cache=base1_sat_xyz_cache,
        )
        base2_numeric = extract_numeric_observations_in_window(
            str(base2_copy),
            effective_start_utc,
            effective_end_utc,
            max_epochs=None,
            sat_xyz_cache=base2_sat_xyz_cache,
        )
        base3_numeric = extract_numeric_observations_in_window(
            str(base3_copy),
            effective_start_utc,
            effective_end_utc,
            max_epochs=None,
            sat_xyz_cache=base3_sat_xyz_cache,
        )

        # DEBUG TEMPORÁRIO — verificar se os satélites extraídos têm sat_xyz
        try:
            print("\n[DEBUG-SATXYZ] ===== INSPEÇÃO BASE1_NUMERIC =====", flush=True)
            print(
                f"[DEBUG-SATXYZ] epochs extraídas base1: {len(base1_numeric.get('epochs', []))}",
                flush=True,
            )

            if base1_numeric.get("epochs"):
                first_epoch = base1_numeric["epochs"][0]
                print(
                    f"[DEBUG-SATXYZ] first_epoch keys: {list(first_epoch.keys())}",
                    flush=True,
                )
                print(
                    f"[DEBUG-SATXYZ] first_epoch epoch: {first_epoch.get('epoch')}",
                    flush=True,
                )

                sat_records = first_epoch.get("satellite_records", []) or []
                print(
                    f"[DEBUG-SATXYZ] satellite_records count: {len(sat_records)}",
                    flush=True,
                )

                if sat_records:
                    first_sat = sat_records[0]
                    print(
                        f"[DEBUG-SATXYZ] first_sat keys: {list(first_sat.keys())}",
                        flush=True,
                    )
                    print(
                        f"[DEBUG-SATXYZ] first_sat full: {first_sat}",
                        flush=True,
                    )

                    print(
                        f"[DEBUG-SATXYZ] has sat_xyz? {'sat_xyz' in first_sat}",
                        flush=True,
                    )
                    print(
                        f"[DEBUG-SATXYZ] has satellite_xyz? {'satellite_xyz' in first_sat}",
                        flush=True,
                    )
                    print(
                        f"[DEBUG-SATXYZ] has xyz? {'xyz' in first_sat}",
                        flush=True,
                    )

                    if "sat_xyz" in first_sat:
                        print(
                            f"[DEBUG-SATXYZ] sat_xyz value: {first_sat.get('sat_xyz')}",
                            flush=True,
                        )
                    if "satellite_xyz" in first_sat:
                        print(
                            f"[DEBUG-SATXYZ] satellite_xyz value: {first_sat.get('satellite_xyz')}",
                            flush=True,
                        )
                    if "xyz" in first_sat:
                        print(
                            f"[DEBUG-SATXYZ] xyz value: {first_sat.get('xyz')}",
                            flush=True,
                        )
                else:
                    print(
                        "[DEBUG-SATXYZ] first_epoch sem satellite_records", flush=True
                    )
            else:
                print("[DEBUG-SATXYZ] base1_numeric sem epochs", flush=True)

            print("[DEBUG-SATXYZ] ===== FIM INSPEÇÃO =====\n", flush=True)

        except Exception as debug_exc:
            print(f"[DEBUG-SATXYZ] erro ao inspecionar: {debug_exc}", flush=True)

        numeric_timer()

        _progress(
            job_root,
            46,
            "numeric_extract",
            "Observações numéricas extraídas",
            "Pronto para geração geométrica",
        )

        numeric_summary_timer = _timed("17-NUMERIC-SUMMARY")
        base1_numeric_summary = summarize_numeric_observations(base1_numeric)
        base2_numeric_summary = summarize_numeric_observations(base2_numeric)
        base3_numeric_summary = summarize_numeric_observations(base3_numeric)
        numeric_summary_timer()

        seed_timer = _timed("18-NETWORK-SEED")
        virtual_station_seed = build_network_only_virtual_station_seed(
            base1_numeric,
            base2_numeric,
            base3_numeric,
            max_epochs=None,
        )
        seed_timer()

        offset_stations = [
            {
                "name": base1_meta.get("station_code")
                or base1_meta.get("file_name")
                or "base_1",
                "xyz": base1_meta["xyz"],
                "offset": base1_offset,
                "quality_score": 1.0,
            },
            {
                "name": base2_meta.get("station_code")
                or base2_meta.get("file_name")
                or "base_2",
                "xyz": base2_meta["xyz"],
                "offset": base2_offset,
                "quality_score": 1.0,
            },
            {
                "name": base3_meta.get("station_code")
                or base3_meta.get("file_name")
                or "base_3",
                "xyz": base3_meta["xyz"],
                "offset": base3_offset,
                "quality_score": 1.0,
            },
        ]

        interpolated_vrs_offset = {
            "applied": False,
            "reason": "Interpolação desativada no perfil.",
            "dx": 0.0,
            "dy": 0.0,
            "dz": 0.0,
            "magnitude": 0.0,
            "station_count_used": 0,
            "stations_used": [],
            "weights": [],
        }

        target_xyz_effective = dict(target_xyz_nominal)

        interp_timer = _timed("19-INTERPOLATED-OFFSET")
        if apply_interpolated_station_offsets:
            interpolated_vrs_offset = interpolate_vrs_offset(
                target_xyz=target_xyz_nominal,
                stations=offset_stations,
                distance_power=offset_distance_power,
                min_stations=offset_interpolation_min_stations,
                max_offset_m=max_station_offset_m,
                apply_quality_factor=offset_apply_quality_factor,
                apply_geometry_factor=False,
            )

            if interpolated_vrs_offset.get("applied"):
                target_xyz_effective = apply_interpolated_offset_to_target_xyz(
                    target_xyz_nominal,
                    interpolated_vrs_offset,
                )
        interp_timer()

        target_solution_timer = _timed("19B-TARGET-SOLUTION")

        target_xyz_final = {
            "x": float(target_xyz_effective["x"]),
            "y": float(target_xyz_effective["y"]),
            "z": float(target_xyz_effective["z"]),
        }

        target_lat_final, target_lon_final, target_h_final = ecef_to_geodetic(
            target_xyz_final["x"],
            target_xyz_final["y"],
            target_xyz_final["z"],
        )

        nominal_vs_final_delta = {
            "dx": float(target_xyz_final["x"] - target_xyz_nominal["x"]),
            "dy": float(target_xyz_final["y"] - target_xyz_nominal["y"]),
            "dz": float(target_xyz_final["z"] - target_xyz_nominal["z"]),
        }

        nominal_vs_final_delta["magnitude"] = (
            nominal_vs_final_delta["dx"] ** 2
            + nominal_vs_final_delta["dy"] ** 2
            + nominal_vs_final_delta["dz"] ** 2
        ) ** 0.5

        target_solution = {
            "nominal": {
                "geodetic": {
                    "lat": float(target["lat"]),
                    "lon": float(target["lon"]),
                    "height": float(target["height"]),
                },
                "xyz": {
                    "x": float(target_xyz_nominal["x"]),
                    "y": float(target_xyz_nominal["y"]),
                    "z": float(target_xyz_nominal["z"]),
                },
            },
            "final": {
                "geodetic": {
                    "lat": float(target_lat_final),
                    "lon": float(target_lon_final),
                    "height": float(target_h_final),
                },
                "xyz": {
                    "x": float(target_xyz_final["x"]),
                    "y": float(target_xyz_final["y"]),
                    "z": float(target_xyz_final["z"]),
                },
            },
            "delta_nominal_to_final": nominal_vs_final_delta,
            "interpolated_offset_applied": bool(
                interpolated_vrs_offset.get("applied", False)
            ),
            "interpolated_offset": interpolated_vrs_offset,
        }

        target_solution_timer(
            (
                f"lat={target_lat_final:.10f}, "
                f"lon={target_lon_final:.10f}, "
                f"h={target_h_final:.4f}, "
                f"d={nominal_vs_final_delta['magnitude']:.4f} m"
            )
        )

        _progress(
            job_root,
            55,
            "virtual_seed",
            "Gerando VRS geométrica por satélite e por época",
            "Aplicando Δρ, ΔI e ΔT na base mestre",
        )

        build_virtual_timer = _timed("20-BUILD-VIRTUAL-SEED")

        virtual_observation_seed = build_relative_correction_vrs_seed(
            master_numeric=base1_numeric,
            aux1_numeric=base2_numeric,
            aux2_numeric=base3_numeric,
            target_meta={
                "lat": target["lat"],
                "lon": target["lon"],
                "height": target["height"],
                "x": target_xyz_effective["x"],
                "y": target_xyz_effective["y"],
                "z": target_xyz_effective["z"],
                "nominal_x": target_xyz_nominal["x"],
                "nominal_y": target_xyz_nominal["y"],
                "nominal_z": target_xyz_nominal["z"],
                "interpolated_offset": interpolated_vrs_offset,
                "master_station_name": base1_meta.get("station_code")
                or base1_meta.get("file_name"),
                "master_xyz": base1_meta["xyz"],
                "aux1_xyz": base2_meta["xyz"],
                "aux2_xyz": base3_meta["xyz"],
            },
            max_epochs=None,
            min_satellites_per_epoch=max(4, min_satellites_per_epoch),
            require_dual_frequency=require_dual_frequency,
            priority_obs=priority_obs,
            gps_only=True,
        )
        build_virtual_timer(
            f"{virtual_observation_seed.get('generated_epoch_count', 0)} épocas"
        )

        _progress(
            job_root,
            62,
            "virtual_summary",
            "Sumarizando VRS geométrica",
            f"Épocas geradas: {virtual_observation_seed.get('generated_epoch_count', 0)}",
        )

        summary_virtual_timer = _timed("21-VIRTUAL-SUMMARY")
        virtual_observation_summary = summarize_virtual_observation_seed(
            virtual_observation_seed
        )
        summary_virtual_timer()

        _progress(
            job_root,
            68,
            "sanitize",
            "Sanitizando observações virtuais",
            "Removendo vazios e priorizando observáveis úteis",
        )

        sanitize_timer = _timed("22-SANITIZE")
        sanitized_virtual_data = sanitize_virtual_observations(
            virtual_observation_seed,
            min_obs_per_satellite=2,
            keep_only_priority_obs=True,
            priority_obs=priority_obs,
            drop_empty_epochs=True,
        )
        sanitize_timer(
            f"{sanitized_virtual_data.get('generated_epoch_count', 0)} épocas válidas"
        )

        if sanitized_virtual_data.get("generated_epoch_count", 0) == 0:
            raise ValueError(
                "A VRS sintetizada ficou sem épocas válidas após a sanitização."
            )

        sanitized_virtual_summary = summarize_sanitized_virtual_observations(
            sanitized_virtual_data
        )

        _progress(
            job_root,
            74,
            "ppp_seed",
            "Preparando seed final para PPP",
            "Aplicando motor relativo ancorado no alvo v4",
        )

        ppp_timer = _timed("23-PPP")
        ppp_block_selection = {
            "strategy": "target_anchored_relative_geometry_v4",
            "selected_epoch_count": 0,
            "selected_start": None,
            "selected_end": None,
            "selected_avg_satellites": 0.0,
            "selected_avg_priority_obs_per_sat": 0.0,
            "best_block_score": 0.0,
            "reason": "Produto PPP final gerado pelo motor relativo ancorado no alvo v4.",
            "blocks_found": [],
        }

        ppp_aux_data = None
        ppp_aux_summary = None
        ppp_rinex_path = None
        ppp_hardening_meta = {}
        ppp_physical_filter_meta = {}

        relative_ppp_timer = _timed("23A-TARGET-ANCHORED-V4")

        final_ppp_seed = build_physical_geometry_vrs_seed_v4(
            master_numeric=base1_numeric,
            aux_numeric_list=[base2_numeric, base3_numeric],
            target_meta={
                "lat": target_solution["final"]["geodetic"]["lat"],
                "lon": target_solution["final"]["geodetic"]["lon"],
                "height": target_solution["final"]["geodetic"]["height"],
                "x": target_xyz_effective["x"],
                "y": target_xyz_effective["y"],
                "z": target_xyz_effective["z"],
                "nominal_x": target_xyz_nominal["x"],
                "nominal_y": target_xyz_nominal["y"],
                "nominal_z": target_xyz_nominal["z"],
                "interpolated_offset": interpolated_vrs_offset,
                "master_station_name": base1_meta.get("station_code")
                or base1_meta.get("file_name")
                or "BASE_1",
                "master_xyz": base1_meta["xyz"],
                "aux_xyz_list": [base2_meta["xyz"], base3_meta["xyz"]],
                "aux_station_names": [
                    base2_meta.get("station_code")
                    or base2_meta.get("file_name")
                    or "BASE_2",
                    base3_meta.get("station_code")
                    or base3_meta.get("file_name")
                    or "BASE_3",
                ],
            },
            max_epochs=None,
            min_satellites_per_epoch=max(4, min_satellites_per_epoch),
            require_dual_frequency=require_dual_frequency,
            priority_obs=priority_obs,
            gps_only=True,
            distance_power=1.2,
            code_aux_scale=0.35,
            phase_aux_scale=0.20,
            prefer_plane_fit=False,
        )

        relative_ppp_timer(f"{final_ppp_seed.get('generated_epoch_count', 0)} épocas")

        v4_geom = final_ppp_seed.get("geometry_coefficients", {}) or {}

        _debug(
            "23A1",
            "PPP target-anchored v4 => "
            f"epochs={final_ppp_seed.get('generated_epoch_count', 0)} | "
            f"method={final_ppp_seed.get('method')} | "
            f"anchored={final_ppp_seed.get('geometry_coefficients', {}).get('anchored_solution', False)} | "
            f"epochs_with_sat_xyz={final_ppp_seed.get('geometry_coefficients', {}).get('epochs_with_satellite_xyz', 0)} | "
            f"epochs_without_sat_xyz={final_ppp_seed.get('geometry_coefficients', {}).get('epochs_without_satellite_xyz', 0)}",
        )

        if not final_ppp_seed.get("geometry_coefficients", {}).get(
            "anchored_solution", False
        ):
            _debug(
                "23A2",
                "ATENÇÃO: seed PPP sem ancoragem geométrica efetiva.",
            )

        if final_ppp_seed.get("generated_epoch_count", 0) == 0:
            raise ValueError("A VRS relativa v4 ficou sem épocas válidas para PPP.")

        diagnostics_timer = _timed("23B-DIAGNOSTICS")
        ppp_observation_diagnostics = generate_ppp_observation_diagnostics(
            final_ppp_seed,
            priority_obs=priority_obs,
            min_satellites_per_epoch=4,
        )
        diagnostics_timer()

        final_ppp_summary = summarize_virtual_observation_seed(final_ppp_seed)

        ppp_block_selection.update(
            {
                "selected_epoch_count": final_ppp_seed.get("generated_epoch_count", 0),
                "selected_start": (
                    final_ppp_seed["epochs"][0]["epoch"]
                    if final_ppp_seed.get("epochs")
                    else None
                ),
                "selected_end": (
                    final_ppp_seed["epochs"][-1]["epoch"]
                    if final_ppp_seed.get("epochs")
                    else None
                ),
                "selected_avg_satellites": final_ppp_summary.get(
                    "avg_satellites_per_epoch", 0.0
                ),
                "selected_avg_priority_obs_per_sat": 0.0,
                "best_block_score": float(
                    final_ppp_seed.get("generated_epoch_count", 0)
                ),
                "reason": (
                    "Produto final PPP montado pelo motor relativo ancorado no alvo v4."
                ),
            }
        )

        if generate_ppp_aux_file:
            ppp_aux_data = final_ppp_seed
            ppp_aux_summary = final_ppp_summary

        ppp_timer(f"{final_ppp_seed.get('generated_epoch_count', 0)} épocas finais")

        output_rinex_base_name = _safe_rinex_output_name(
            getattr(job, "output_rinex_name", None),
            default="BASE",
        )
        output_marker_name = _safe_marker_name(
            getattr(job, "output_marker_name", None),
            default="BASE",
        )

        virtual_observations_json_path = (
            result_root / "virtual_station_observations.json"
        )
        virtual_seed_path = result_root / "virtual_station_seed.json"
        virtual_numeric_summary_path = (
            result_root / "virtual_station_numeric_summary.json"
        )
        virtual_validation_path = result_root / "virtual_station_validation_report.json"

        sanitized_virtual_json_path = (
            result_root / "virtual_station_observations_sanitized.json"
        )
        sanitized_virtual_summary_path = (
            result_root / "virtual_station_numeric_summary_sanitized.json"
        )

        virtual_rinex_mixed_path = result_root / "virtual_base_mixed.obs"
        base_ppp_path = result_root / "BASE.obs"
        custom_final_rinex_path = result_root / f"{output_rinex_base_name}.obs"

        ppp_aux_json_path = result_root / "virtual_station_observations_ppp_aux.json"
        ppp_aux_summary_path = (
            result_root / "virtual_station_numeric_summary_ppp_aux.json"
        )
        ppp_aux_rinex_path = result_root / "base_ppp.obs"

        numeric_summary_path = result_root / "numeric_observations_summary.json"
        sync_summary_path = result_root / "sync_summary.json"

        observation_summary_path = result_root / "epoch_observations_summary.json"
        observation_blocks_path = result_root / "synchronized_observation_blocks.json"
        ppp_observation_diagnostics_path = (
            result_root / "ppp_observation_diagnostics.json"
        )

        write_timer = _timed("24-WRITE-OUTPUTS")

        write_ppp_observation_diagnostics(
            ppp_observation_diagnostics_path,
            ppp_observation_diagnostics,
        )

        write_virtual_observations_json(
            str(virtual_observations_json_path),
            virtual_observation_seed,
        )

        virtual_seed_path.write_text(
            json.dumps(virtual_station_seed, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        virtual_numeric_summary_path.write_text(
            json.dumps(virtual_observation_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        sanitized_virtual_summary_path.write_text(
            json.dumps(sanitized_virtual_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        write_sanitized_virtual_observations_json(
            sanitized_virtual_json_path,
            sanitized_virtual_data,
        )

        write_virtual_rinex_v3_obs(
            str(virtual_rinex_mixed_path),
            sanitized_virtual_data,
            marker_name=output_marker_name,
        )

        _progress(
            job_root,
            82,
            "write_outputs",
            "Escrevendo arquivos de saída",
            "Gerando RINEX mixed e RINEX final PPP",
        )

        ppp_final_source_mode = "target_virtual_solution"
        ppp_final_source_file = "virtual_solution_from_network"

        write_virtual_rinex_v2_obs(
            str(base_ppp_path),
            final_ppp_seed,
            marker_name=output_marker_name,
        )

        _progress(
            job_root,
            88,
            "validate_ppp",
            "Validando estrutura do RINEX final",
            base_ppp_path.name,
        )

        virtual_validation_report = validate_virtual_observations(str(base_ppp_path))

        if virtual_validation_report["status"] == "failed":
            _debug(
                "24A",
                "BASE.obs da VRS alvo falhou na validação estrutural; aplicando fallback PPP-safe da base de referência.",
            )

            build_ppp_safe_rinex_from_reference_base(
                reference_rinex_path=str(
                    Path(reference_base.get("copied_path") or base1_copy)
                ),
                output_path=str(base_ppp_path),
                start_utc=effective_start_utc,
                end_utc=effective_end_utc,
                marker_name=output_marker_name,
            )
            virtual_validation_report = validate_virtual_observations(
                str(base_ppp_path)
            )

            ppp_final_source_mode = "reference_base_fallback"
            ppp_final_source_file = reference_base.get("file_name") or safe_file_name(
                base1_copy
            )

        write_virtual_validation_report(
            virtual_validation_path,
            virtual_validation_report,
        )

        if virtual_validation_report["status"] == "failed":
            raise ValueError(
                "O arquivo final PPP (BASE.obs) falhou na validação estrutural mesmo após fallback. "
                f"Erros: {' | '.join(virtual_validation_report.get('errors', []))}"
            )

        _copy_if_different(base_ppp_path, custom_final_rinex_path)

        if ppp_aux_data and ppp_aux_data.get("generated_epoch_count", 0) > 0:
            write_virtual_observations_json(
                str(ppp_aux_json_path),
                ppp_aux_data,
            )

            if ppp_aux_summary is not None:
                ppp_aux_summary_path.write_text(
                    json.dumps(ppp_aux_summary, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            write_virtual_rinex_v2_obs(
                str(ppp_aux_rinex_path),
                ppp_aux_data,
                marker_name=output_marker_name,
            )
            ppp_rinex_path = ppp_aux_rinex_path

        numeric_summary_path.write_text(
            json.dumps(
                {
                    "processing_mode": "network_only",
                    "base_1": base1_numeric_summary,
                    "base_2": base2_numeric_summary,
                    "base_3": base3_numeric_summary,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        sync_summary_path.write_text(
            json.dumps(
                {
                    "requested_window_local": {
                        "start": requested_start_local.isoformat(sep=" "),
                        "end": requested_end_local.isoformat(sep=" "),
                        "timezone": LOCAL_TIMEZONE,
                    },
                    "requested_window_utc": {
                        "start": requested_start_utc.isoformat(),
                        "end": requested_end_utc.isoformat(),
                    },
                    "common_window_utc": {
                        "start": common_window["common_start"].isoformat(),
                        "end": common_window["common_end"].isoformat(),
                        "duration_seconds": common_window["duration_seconds"],
                    },
                    "effective_window_utc": {
                        "start": effective_start_utc.isoformat(),
                        "end": effective_end_utc.isoformat(),
                        "duration_seconds": int(
                            (effective_end_utc - effective_start_utc).total_seconds()
                        ),
                    },
                    "final_ppp_file": str(base_ppp_path),
                    "final_ppp_source": ppp_final_source_file,
                    "ppp_physical_filter": ppp_physical_filter_meta,
                    "ppp_hardening": ppp_hardening_meta,
                    "mixed_aux_file": str(virtual_rinex_mixed_path),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        observation_summary_path.write_text(
            json.dumps(
                synchronized_observations["summary"],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        observation_blocks_path.write_text(
            json.dumps(
                synchronized_observations["blocks"][:200],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        write_timer()

        metadata_timer = _timed("25-WRITE-METADATA")
        (metadata_root / "base1_meta.json").write_text(
            json.dumps(base1_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (metadata_root / "base2_meta.json").write_text(
            json.dumps(base2_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (metadata_root / "base3_meta.json").write_text(
            json.dumps(base3_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        (metadata_root / "base1_epochs_summary.json").write_text(
            json.dumps(base1_epoch_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (metadata_root / "base2_epochs_summary.json").write_text(
            json.dumps(base2_epoch_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (metadata_root / "base3_epochs_summary.json").write_text(
            json.dumps(base3_epoch_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        metadata_timer()

        manifest_timer = _timed("26-MANIFEST")
        manifest = {
            "job_id": job.id,
            "job_name": job.name,
            "processed_at": datetime.utcnow().isoformat() + "Z",
            "processing_mode": "network_only",
            "target": target,
            "profile_config_used": config,
            "selected_station_codes": selected_station_codes,
            "session": {
                "date": job.session_date,
                "start_time": job.start_time,
                "end_time": job.end_time,
                "occupation_minutes": job.occupation_minutes,
                "requested_minutes": job.requested_minutes,
                "timezone": LOCAL_TIMEZONE,
            },
            "requested_window_local": {
                "start": requested_start_local.isoformat(sep=" "),
                "end": requested_end_local.isoformat(sep=" "),
            },
            "requested_window_utc": {
                "start": requested_start_utc.isoformat(),
                "end": requested_end_utc.isoformat(),
            },
            "effective_processing_window_utc": {
                "start": effective_start_utc.isoformat(),
                "end": effective_end_utc.isoformat(),
            },
            "network_validation_status": job.network_validation_status,
            "network_validation_summary": job.network_validation_summary,
            "bases": [
                {
                    "index": 1,
                    "source_type": base1_meta.get("source_type"),
                    "station_code": base1_meta.get("station_code"),
                    "original": str(base1_path),
                    "used": str(base1_rinex),
                    "copied_to": str(base1_copy),
                    "distance_to_target_m": base1_meta.get("distance_to_target_m"),
                },
                {
                    "index": 2,
                    "source_type": base2_meta.get("source_type"),
                    "station_code": base2_meta.get("station_code"),
                    "original": str(base2_path),
                    "used": str(base2_rinex),
                    "copied_to": str(base2_copy),
                    "distance_to_target_m": base2_meta.get("distance_to_target_m"),
                },
                {
                    "index": 3,
                    "source_type": base3_meta.get("source_type"),
                    "station_code": base3_meta.get("station_code"),
                    "original": str(base3_path),
                    "used": str(base3_rinex),
                    "copied_to": str(base3_copy),
                    "distance_to_target_m": base3_meta.get("distance_to_target_m"),
                },
            ],
            "navigation_files": navigation_files,
        }

        manifest_path = job_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest_timer()

        virtual_meta_timer = _timed("27-VIRTUAL-META")
        virtual_base_meta = {
            "job_id": job.id,
            "job_name": job.name,
            "status": "ready_for_virtual_observation_generation",
            "processing_mode": "network_only",
            "target": target_solution,
            "profile_config_used": config,
            "selected_station_codes": selected_station_codes,
            "requested_window_local": {
                "start": requested_start_local.isoformat(sep=" "),
                "end": requested_end_local.isoformat(sep=" "),
                "timezone": LOCAL_TIMEZONE,
            },
            "requested_window_utc": {
                "start": requested_start_utc.isoformat(),
                "end": requested_end_utc.isoformat(),
            },
            "common_window": {
                "start": format_dt(common_window["common_start"]),
                "end": format_dt(common_window["common_end"]),
                "duration_seconds": common_window["duration_seconds"],
            },
            "effective_window_utc": {
                "start": effective_start_utc.isoformat(),
                "end": effective_end_utc.isoformat(),
                "duration_seconds": int(
                    (effective_end_utc - effective_start_utc).total_seconds()
                ),
            },
            "reference_base": {
                "file_name": reference_base.get("file_name"),
                "marker_name": reference_base.get("marker_name"),
                "station_code": reference_base.get("station_code"),
                "distance_to_target_m": reference_base.get("distance_to_target_m"),
                "geodetic": reference_base.get("geodetic"),
                "copied_path": reference_base.get("copied_path"),
                "original_path": reference_base.get("original_path"),
                "rinex_path": reference_base.get("rinex_path"),
            },
            "epoch_sync": {
                "base1_epoch_count_in_window": base1_epoch_summary[
                    "epoch_count_in_window"
                ],
                "base2_epoch_count_in_window": base2_epoch_summary[
                    "epoch_count_in_window"
                ],
                "base3_epoch_count_in_window": base3_epoch_summary[
                    "epoch_count_in_window"
                ],
            },
            "observation_sync": {
                "common_epoch_count": synchronized_observations["summary"].get(
                    "common_epoch_count"
                ),
                "epochs_with_common_satellites": synchronized_observations[
                    "summary"
                ].get("epochs_with_common_satellites"),
                "max_common_satellite_count": synchronized_observations["summary"].get(
                    "max_common_satellite_count"
                ),
            },
            "numeric_observation_summary": {
                "processing_mode": "network_only",
                "base1_epoch_count_extracted": base1_numeric_summary[
                    "epoch_count_extracted"
                ],
                "base2_epoch_count_extracted": base2_numeric_summary[
                    "epoch_count_extracted"
                ],
                "base3_epoch_count_extracted": base3_numeric_summary[
                    "epoch_count_extracted"
                ],
                "virtual_seed_common_epoch_count": virtual_station_seed[
                    "common_epoch_count"
                ],
                "virtual_seed_block_count": virtual_station_seed["seed_block_count"],
            },
            "virtual_generation": {
                "method": virtual_observation_seed["method"],
                "systems": virtual_observation_summary.get("systems", ["G"]),
                "generated_epoch_count": virtual_observation_seed[
                    "generated_epoch_count"
                ],
                "epochs_with_satellites": virtual_observation_summary[
                    "epochs_with_satellites"
                ],
                "avg_satellites_per_epoch": virtual_observation_summary.get(
                    "avg_satellites_per_epoch", 0.0
                ),
                "max_satellite_count_per_epoch": virtual_observation_summary[
                    "max_satellite_count_per_epoch"
                ],
                "common_observation_types": virtual_observation_summary[
                    "common_observation_types"
                ],
                "weights_reference": virtual_observation_summary.get(
                    "weights_reference", {}
                ),
                "min_bases_per_observation": virtual_observation_summary.get(
                    "min_bases_per_observation", min_bases_per_observation
                ),
                "min_satellites_per_epoch": virtual_observation_summary.get(
                    "min_satellites_per_epoch", min_satellites_per_epoch
                ),
                "min_satellite_presence_ratio": virtual_observation_summary.get(
                    "min_satellite_presence_ratio", min_satellite_presence_ratio
                ),
                "min_satellite_longest_arc": virtual_observation_summary.get(
                    "min_satellite_longest_arc", min_satellite_longest_arc
                ),
                "max_satellites_per_epoch": virtual_observation_summary.get(
                    "max_satellites_per_epoch", max_satellites_per_epoch
                ),
                "approved_satellite_count": virtual_observation_summary.get(
                    "approved_satellite_count", 0
                ),
                "require_dual_frequency": virtual_observation_summary.get(
                    "require_dual_frequency",
                    require_dual_frequency,
                ),
                "priority_obs": virtual_observation_summary.get(
                    "priority_obs", priority_obs
                ),
                "top_ranked_satellites": virtual_observation_summary.get(
                    "top_ranked_satellites", []
                )[:15],
                "virtual_rinex_mixed_path": str(virtual_rinex_mixed_path),
                "virtual_rinex_ppp_final_path": str(base_ppp_path),
                "virtual_rinex_ppp_aux_path": (
                    str(ppp_rinex_path) if ppp_rinex_path else None
                ),
            },
            "virtual_validation": {
                "validated_file": virtual_validation_report.get("validated_file"),
                "status": virtual_validation_report["status"],
                "errors": virtual_validation_report.get("errors", []),
                "warnings": virtual_validation_report["warnings"],
                "recommendations": virtual_validation_report["recommendations"],
                "stats": virtual_validation_report.get("stats", {}),
            },
            "virtual_sanitize": {
                "generated_epoch_count": sanitized_virtual_data[
                    "generated_epoch_count"
                ],
                "removed_satellites": sanitized_virtual_data["removed_satellites"],
                "removed_epochs": sanitized_virtual_data["removed_epochs"],
                "min_obs_per_satellite": sanitized_virtual_data[
                    "min_obs_per_satellite"
                ],
                "full_window_preserved": preserve_full_user_window,
            },
            "final_product": {
                "marker_name": output_marker_name,
                "ppp_final_rinex_path": str(base_ppp_path),
                "ppp_final_rinex_filename": base_ppp_path.name,
                "ppp_final_source_mode": ppp_final_source_mode,
                "ppp_final_source_file": ppp_final_source_file,
                "custom_rinex_path": str(custom_final_rinex_path),
                "custom_rinex_filename": custom_final_rinex_path.name,
                "mixed_aux_rinex_path": str(virtual_rinex_mixed_path),
                "ppp_aux_rinex_path": str(ppp_aux_rinex_path) if ppp_aux_data else None,
                "ppp_origin": "geometry_based_network_vrs",
                "ppp_geometry_coefficients": final_ppp_seed.get(
                    "geometry_coefficients", {}
                ),
                "ppp_hardening": ppp_hardening_meta,
                "ppp_physical_filter": ppp_physical_filter_meta,
            },
            "ppp_physical_filter": ppp_physical_filter_meta,
            "ppp_block_selection": {
                "selected_epoch_count": ppp_block_selection.get("selected_epoch_count"),
                "selected_start": ppp_block_selection.get("selected_start"),
                "selected_end": ppp_block_selection.get("selected_end"),
                "selected_avg_satellites": ppp_block_selection.get(
                    "selected_avg_satellites"
                ),
                "selected_avg_priority_obs_per_sat": ppp_block_selection.get(
                    "selected_avg_priority_obs_per_sat"
                ),
                "best_block_score": ppp_block_selection.get("best_block_score"),
                "blocks_found": ppp_block_selection.get("blocks_found", []),
                "reason": ppp_block_selection.get("reason"),
                "auxiliary_file_generated": bool(
                    ppp_aux_data and ppp_aux_data.get("generated_epoch_count", 0) > 0
                ),
            },
            "navigation_files": navigation_files,
            "offset_interpolation": {
                "enabled": apply_interpolated_station_offsets,
                "result": interpolated_vrs_offset,
            },
            "ppp_observation_diagnostics": {
                "path": str(ppp_observation_diagnostics_path),
                "summary": ppp_observation_diagnostics.get("summary", {}),
                "rankings": ppp_observation_diagnostics.get("rankings", {}),
            },
            "bases": [
                {
                    "file_name": base.get("file_name"),
                    "marker_name": base.get("marker_name"),
                    "station_code": base.get("station_code"),
                    "source_type": base.get("source_type"),
                    "distance_to_target_m": base.get("distance_to_target_m"),
                    "geodetic": base.get("geodetic"),
                    "interval_seconds": base.get("interval_seconds"),
                    "first_epoch_text": base.get("first_epoch_text"),
                    "last_epoch_text": base.get("last_epoch_text"),
                }
                for base in [base1_meta, base2_meta, base3_meta]
            ],
        }

        virtual_meta_path = result_root / "virtual_base_meta.json"
        virtual_meta_path.write_text(
            json.dumps(virtual_base_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        virtual_meta_timer()

        result_txt_timer = _timed("28-RESULT-TXT")
        systems_used = virtual_observation_summary.get("systems", ["G"])

        result_txt = result_root / f"job_{job.id}_vrs_result.txt"
        result_txt.write_text(
            "\n".join(
                [
                    "PROCESSAMENTO VRS REAL INICIADO",
                    f"Job: {job.id} - {job.name}",
                    "Modo: network_only",
                    f"Alvo VRS: lat={job.target_lat}, lon={job.target_lon}, h={job.target_height}",
                    (
                        "Alvo VRS final calculado: "
                        f"lat={target_lat_final:.10f}, "
                        f"lon={target_lon_final:.10f}, "
                        f"h={target_h_final:.4f}"
                    ),
                    (
                        "Delta nominal -> final: "
                        f"dX={nominal_vs_final_delta['dx']:.4f} m, "
                        f"dY={nominal_vs_final_delta['dy']:.4f} m, "
                        f"dZ={nominal_vs_final_delta['dz']:.4f} m, "
                        f"3D={nominal_vs_final_delta['magnitude']:.4f} m"
                    ),
                    f"Estações selecionadas no mapa: {', '.join(selected_station_codes) or 'nenhuma'}",
                    f"Sistemas GNSS no arquivo intermediário mixed: {', '.join(systems_used)}",
                    "Sistema GNSS no arquivo final compatível: G",
                    f"Exigir dupla frequência: {virtual_observation_summary.get('require_dual_frequency', require_dual_frequency)}",
                    f"Janela solicitada local: {job.session_date} {job.start_time} -> {job.end_time} ({LOCAL_TIMEZONE})",
                    f"Janela solicitada UTC: {requested_start_utc.isoformat()} -> {requested_end_utc.isoformat()}",
                    f"Janela efetiva de processamento UTC: {effective_start_utc.isoformat()} -> {effective_end_utc.isoformat()}",
                    f"Base mestre usada: {safe_file_name(base1_copy)}",
                    f"Base auxiliar 1 usada: {safe_file_name(base2_copy)}",
                    f"Base auxiliar 2 usada: {safe_file_name(base3_copy)}",
                    f"Base de referência: {reference_base.get('file_name')}",
                    f"RINEX mixed auxiliar: {virtual_rinex_mixed_path.name}",
                    f"RINEX final PPP/IBGE: {base_ppp_path.name}",
                    "Origem do PPP final: target_virtual_solution",
                    "Fonte do PPP final: virtual_solution_from_network",
                    (
                        "PPP v4 geometry: "
                        f"distance_power={final_ppp_seed.get('geometry_coefficients', {}).get('distance_power', 0.0):.4f}, "
                        f"master_target_distance_m={final_ppp_seed.get('geometry_coefficients', {}).get('master_target_distance_m', 0.0):.4f}, "
                        f"aux1_target_distance_m={final_ppp_seed.get('geometry_coefficients', {}).get('aux1_target_distance_m', 0.0):.4f}, "
                        f"aux2_target_distance_m={final_ppp_seed.get('geometry_coefficients', {}).get('aux2_target_distance_m', 0.0):.4f}, "
                        f"epochs_with_sat_xyz={final_ppp_seed.get('geometry_coefficients', {}).get('epochs_with_satellite_xyz', 0)}, "
                        f"epochs_without_sat_xyz={final_ppp_seed.get('geometry_coefficients', {}).get('epochs_without_satellite_xyz', 0)}, "
                        f"anchored_solution={final_ppp_seed.get('geometry_coefficients', {}).get('anchored_solution', False)}"
                    ),
                    f"Estações totais na interpolação: {final_ppp_seed.get('interpolation_stats', {}).get('stations_total', 0)}",
                    f"Épocas removidas no filtro físico PPP: {ppp_physical_filter_meta.get('removed_epochs', 0)}",
                    f"Instâncias satélite/época removidas no filtro físico PPP: {ppp_physical_filter_meta.get('removed_satellite_instances', 0)}",
                    f"Satélites mantidos no endurecimento PPP: {len(ppp_hardening_meta.get('kept_satellites', []))}",
                    f"Épocas removidas no endurecimento PPP: {ppp_hardening_meta.get('removed_epochs', 0)}",
                    f"Início do bloco PPP endurecido: {ppp_hardening_meta.get('selected_block_start')}",
                    f"Fim do bloco PPP endurecido: {ppp_hardening_meta.get('selected_block_end')}",
                    f"Validação PPP final: {virtual_validation_report.get('status')}",
                ]
            ),
            encoding="utf-8",
        )
        result_txt_timer()

        log_timer = _timed("29-LOG")
        log_path = log_root / "process.log"
        log_path.write_text(
            "\n".join(
                [
                    f"[{datetime.utcnow().isoformat()}Z] Processamento VRS real executado com sucesso.",
                    f"Selected station codes: {', '.join(selected_station_codes) or 'none'}",
                    f"Reference base: {reference_base.get('file_name')}",
                    f"Requested UTC window: {requested_start_utc.isoformat()} -> {requested_end_utc.isoformat()}",
                    f"Effective UTC window: {effective_start_utc.isoformat()} -> {effective_end_utc.isoformat()}",
                    f"Final PPP file: {base_ppp_path}",
                    f"Mixed auxiliary file: {virtual_rinex_mixed_path}",
                    (
                        "Target final geodetic: "
                        f"lat={target_lat_final:.10f}, "
                        f"lon={target_lon_final:.10f}, "
                        f"h={target_h_final:.4f}"
                    ),
                    (
                        "Target delta nominal->final: "
                        f"{nominal_vs_final_delta['magnitude']:.4f} m"
                    ),
                    (
                        "PPP v4 geometry: "
                        f"distance_power={final_ppp_seed.get('geometry_coefficients', {}).get('distance_power', 0.0):.4f}, "
                        f"master_target_distance_m={final_ppp_seed.get('geometry_coefficients', {}).get('master_target_distance_m', 0.0):.4f}, "
                        f"aux1_target_distance_m={final_ppp_seed.get('geometry_coefficients', {}).get('aux1_target_distance_m', 0.0):.4f}, "
                        f"aux2_target_distance_m={final_ppp_seed.get('geometry_coefficients', {}).get('aux2_target_distance_m', 0.0):.4f}, "
                        f"epochs_with_sat_xyz={final_ppp_seed.get('geometry_coefficients', {}).get('epochs_with_satellite_xyz', 0)}, "
                        f"epochs_without_sat_xyz={final_ppp_seed.get('geometry_coefficients', {}).get('epochs_without_satellite_xyz', 0)}, "
                        f"anchored_solution={final_ppp_seed.get('geometry_coefficients', {}).get('anchored_solution', False)}"
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        log_timer()

        whole_timer(f"job={job.id}")

        _progress(
            job_root,
            100,
            "done",
            "Processamento concluído",
            virtual_validation_report.get("status"),
        )

        return {
            "solution_path": str(result_txt),
            "processing_summary": "Processamento VRS real concluído com sucesso.",
            "manifest_path": str(manifest_path),
            "virtual_meta_path": str(virtual_meta_path),
            "job_root": str(job_root),
            "sync_summary_path": str(sync_summary_path),
            "observation_summary_path": str(observation_summary_path),
            "observation_blocks_path": str(observation_blocks_path),
            "numeric_summary_path": str(numeric_summary_path),
            "virtual_seed_path": str(virtual_seed_path),
            "virtual_observations_json_path": str(virtual_observations_json_path),
            "virtual_numeric_summary_path": str(virtual_numeric_summary_path),
            "virtual_validation_path": str(virtual_validation_path),
            "sanitized_virtual_json_path": str(sanitized_virtual_json_path),
            "sanitized_virtual_summary_path": str(sanitized_virtual_summary_path),
            "virtual_rinex_mixed_path": str(virtual_rinex_mixed_path),
            "base_ppp_path": str(base_ppp_path),
            "custom_final_rinex_path": str(custom_final_rinex_path),
            "ppp_aux_json_path": str(ppp_aux_json_path) if ppp_aux_data else None,
            "ppp_aux_summary_path": str(ppp_aux_summary_path) if ppp_aux_data else None,
            "ppp_aux_rinex_path": str(ppp_aux_rinex_path) if ppp_aux_data else None,
            "ppp_observation_diagnostics_path": str(ppp_observation_diagnostics_path),
        }

    except Exception as exc:
        try:
            if "job_root" in locals():
                _progress(
                    job_root,
                    100,
                    "error",
                    "Falha no processamento",
                    f"{type(exc).__name__}: {exc}",
                )
        except Exception:
            pass

        _debug("ERROR", f"{type(exc).__name__}: {exc}")
        print(traceback.format_exc(), flush=True)
        raise
