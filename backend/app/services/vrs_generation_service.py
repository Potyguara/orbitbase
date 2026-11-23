from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import math
from collections import defaultdict
from copy import deepcopy

SUPPORTED_OBS_PRIORITY = [
    "C1",
    "P1",
    "L1",
    "C2",
    "P2",
    "L2",
    "C5",
    "S1",
    "S2",
    "S5",
]

DEFAULT_PRIORITY_OBS = [
    "C1",
    "P1",
    "L1",
    "C2",
    "P2",
    "L2",
    "S1",
    "S2",
]


def write_ppp_observation_diagnostics(output_path: str, diagnostics: dict) -> str:
    from pathlib import Path
    import json

    path = Path(output_path)
    path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


def _preferred_code_l1(obs: dict) -> float | None:
    return (
        _safe_float(obs.get("P1"))
        if _safe_float(obs.get("P1")) is not None
        else _safe_float(obs.get("C1"))
    )


def _preferred_code_l2(obs: dict) -> float | None:
    return (
        _safe_float(obs.get("P2"))
        if _safe_float(obs.get("P2")) is not None
        else _safe_float(obs.get("C2"))
    )


def _phase_l1(obs: dict) -> float | None:
    return _safe_float(obs.get("L1"))


def _phase_l2(obs: dict) -> float | None:
    return _safe_float(obs.get("L2"))


def _epoch_interval_seconds_from_seed(seed: dict) -> float:
    epochs = seed.get("epochs", []) or []
    return _estimate_interval_seconds(epochs)


def _contiguous_epoch_gap_seconds(
    prev_epoch_iso: str | None,
    curr_epoch_iso: str | None,
) -> float | None:
    if not prev_epoch_iso or not curr_epoch_iso:
        return None
    try:
        prev_dt = _extract_epoch_datetime(prev_epoch_iso)
        curr_dt = _extract_epoch_datetime(curr_epoch_iso)
        return (curr_dt - prev_dt).total_seconds()
    except Exception:
        return None


def _satellite_obs_quality_flags(
    prev_obs: dict | None,
    curr_obs: dict | None,
    max_code_jump_m: float,
    max_phase_jump_cycles: float,
    max_code_l1_l2_diff_m: float,
    max_phase_l1_l2_diff_cycles: float,
) -> dict:
    prev_obs = prev_obs or {}
    curr_obs = curr_obs or {}

    c1 = _preferred_code_l1(curr_obs)
    c2 = _preferred_code_l2(curr_obs)
    l1 = _phase_l1(curr_obs)
    l2 = _phase_l2(curr_obs)

    prev_c1 = _preferred_code_l1(prev_obs)
    prev_c2 = _preferred_code_l2(prev_obs)
    prev_l1 = _phase_l1(prev_obs)
    prev_l2 = _phase_l2(prev_obs)

    flags: list[str] = []

    if c1 is not None and prev_c1 is not None:
        if abs(c1 - prev_c1) > max_code_jump_m:
            flags.append("code_jump_l1")

    if c2 is not None and prev_c2 is not None:
        if abs(c2 - prev_c2) > max_code_jump_m:
            flags.append("code_jump_l2")

    if l1 is not None and prev_l1 is not None:
        if abs(l1 - prev_l1) > max_phase_jump_cycles:
            flags.append("phase_jump_l1")

    if l2 is not None and prev_l2 is not None:
        if abs(l2 - prev_l2) > max_phase_jump_cycles:
            flags.append("phase_jump_l2")

    if c1 is not None and c2 is not None:
        if abs(c2 - c1) > max_code_l1_l2_diff_m:
            flags.append("dual_code_inconsistency")

    if l1 is not None and l2 is not None:
        if abs(l2 - l1) > max_phase_l1_l2_diff_cycles:
            flags.append("dual_phase_inconsistency")

    return {
        "is_valid": len(flags) == 0,
        "flags": flags,
        "c1": c1,
        "c2": c2,
        "l1": l1,
        "l2": l2,
    }


def physically_filter_ppp_seed(
    seed: dict,
    min_satellites_per_epoch: int = 5,
    min_priority_obs_per_satellite: int = 4,
    max_code_jump_m: float = 1200.0,
    max_phase_jump_cycles: float = 120000.0,
    max_code_l1_l2_diff_m: float = 800.0,
    max_phase_l1_l2_diff_cycles: float = 80000.0,
    max_epoch_gap_factor: float = 1.5,
    priority_obs: list[str] | None = None,
) -> dict:
    """
    Filtro físico conservador:
    - compara cada satélite consigo mesmo entre épocas consecutivas;
    - remove satélite/época com salto absurdo;
    - elimina épocas que ficam pobres demais;
    - mantém metadados de depuração.
    """
    priority_obs = priority_obs or DEFAULT_PRIORITY_OBS

    epochs = seed.get("epochs", []) or []
    if not epochs:
        cloned = dict(seed)
        cloned["ppp_physical_filter"] = {
            "removed_epochs": 0,
            "removed_satellite_instances": 0,
            "satellite_rejections": {},
            "reason": "Seed sem épocas.",
        }
        return cloned

    interval_seconds = _epoch_interval_seconds_from_seed(seed)
    max_allowed_gap_seconds = interval_seconds * max_epoch_gap_factor

    prev_epoch_by_sat: dict[str, str] = {}
    prev_obs_by_sat: dict[str, dict] = {}

    filtered_epochs: list[dict] = []
    removed_epochs = 0
    removed_satellite_instances = 0
    satellite_rejections: dict[str, dict] = {}

    for epoch in epochs:
        epoch_iso = epoch.get("epoch")
        sats = epoch.get("satellites", []) or []
        kept_sats = []

        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if not sat_id:
                continue

            obs = sat.get("observations", {}) or {}

            if _get_satellite_system(sat_id) != "G":
                removed_satellite_instances += 1
                satellite_rejections.setdefault(sat_id, {"count": 0, "reasons": {}})
                satellite_rejections[sat_id]["count"] += 1
                satellite_rejections[sat_id]["reasons"]["non_gps"] = (
                    satellite_rejections[sat_id]["reasons"].get("non_gps", 0) + 1
                )
                continue

            if not _has_required_dual_frequency(obs):
                removed_satellite_instances += 1
                satellite_rejections.setdefault(sat_id, {"count": 0, "reasons": {}})
                satellite_rejections[sat_id]["count"] += 1
                satellite_rejections[sat_id]["reasons"]["missing_dual_frequency"] = (
                    satellite_rejections[sat_id]["reasons"].get(
                        "missing_dual_frequency", 0
                    )
                    + 1
                )
                continue

            priority_count = sum(
                1
                for obs_name in priority_obs
                if _safe_float(obs.get(obs_name)) is not None
            )
            if priority_count < min_priority_obs_per_satellite:
                removed_satellite_instances += 1
                satellite_rejections.setdefault(sat_id, {"count": 0, "reasons": {}})
                satellite_rejections[sat_id]["count"] += 1
                satellite_rejections[sat_id]["reasons"]["low_priority_obs"] = (
                    satellite_rejections[sat_id]["reasons"].get("low_priority_obs", 0)
                    + 1
                )
                continue

            prev_epoch_iso = prev_epoch_by_sat.get(sat_id)
            prev_obs = prev_obs_by_sat.get(sat_id)

            gap_seconds = _contiguous_epoch_gap_seconds(prev_epoch_iso, epoch_iso)
            use_temporal_check = (
                gap_seconds is not None and gap_seconds <= max_allowed_gap_seconds
            )

            if use_temporal_check:
                quality = _satellite_obs_quality_flags(
                    prev_obs=prev_obs,
                    curr_obs=obs,
                    max_code_jump_m=max_code_jump_m,
                    max_phase_jump_cycles=max_phase_jump_cycles,
                    max_code_l1_l2_diff_m=max_code_l1_l2_diff_m,
                    max_phase_l1_l2_diff_cycles=max_phase_l1_l2_diff_cycles,
                )
                if not quality["is_valid"]:
                    removed_satellite_instances += 1
                    satellite_rejections.setdefault(sat_id, {"count": 0, "reasons": {}})
                    satellite_rejections[sat_id]["count"] += 1
                    for reason in quality["flags"]:
                        satellite_rejections[sat_id]["reasons"][reason] = (
                            satellite_rejections[sat_id]["reasons"].get(reason, 0) + 1
                        )
                    continue

            kept_sats.append(
                {
                    **sat,
                    "satellite": sat_id,
                }
            )
            prev_epoch_by_sat[sat_id] = epoch_iso
            prev_obs_by_sat[sat_id] = obs

        kept_sats.sort(key=lambda item: item.get("satellite", ""))

        if len(kept_sats) >= min_satellites_per_epoch:
            filtered_epochs.append(
                {
                    **epoch,
                    "satellites": kept_sats,
                    "generated_satellite_count": len(kept_sats),
                }
            )
        else:
            removed_epochs += 1

    cloned = deepcopy(seed)
    cloned["epochs"] = filtered_epochs
    cloned["generated_epoch_count"] = len(filtered_epochs)
    cloned["systems"] = ["G"] if filtered_epochs else []
    cloned["ppp_physical_filter"] = {
        "removed_epochs": removed_epochs,
        "removed_satellite_instances": removed_satellite_instances,
        "satellite_rejections": satellite_rejections,
        "interval_seconds": interval_seconds,
        "max_allowed_gap_seconds": max_allowed_gap_seconds,
        "max_code_jump_m": max_code_jump_m,
        "max_phase_jump_cycles": max_phase_jump_cycles,
        "max_code_l1_l2_diff_m": max_code_l1_l2_diff_m,
        "max_phase_l1_l2_diff_cycles": max_phase_l1_l2_diff_cycles,
        "min_satellites_per_epoch": min_satellites_per_epoch,
        "min_priority_obs_per_satellite": min_priority_obs_per_satellite,
    }
    return cloned


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _count_non_null(observations: dict) -> int:
    return sum(1 for value in (observations or {}).values() if value is not None)


def _index_epochs(numeric_data: dict) -> dict[str, dict]:
    return {epoch["epoch"]: epoch for epoch in numeric_data.get("epochs", [])}


def _index_satellite_records(epoch_record: dict) -> dict[str, dict]:
    sats = epoch_record.get("satellite_records", []) or []
    return {sat["satellite"]: sat for sat in sats}


def _extract_epoch_datetime(epoch_iso: str) -> datetime:
    return datetime.fromisoformat(epoch_iso)


def _estimate_interval_seconds(epochs: list[dict]) -> float:
    if len(epochs) < 2:
        return 15.0

    try:
        dt1 = _extract_epoch_datetime(epochs[0]["epoch"])
        dt2 = _extract_epoch_datetime(epochs[1]["epoch"])
        delta = (dt2 - dt1).total_seconds()
        if delta > 0:
            return float(delta)
    except Exception:
        pass

    return 15.0


def _geodetic_to_ecef(
    lat_deg: float, lon_deg: float, h_m: float
) -> tuple[float, float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)

    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)

    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)

    x = (n + h_m) * cos_lat * cos_lon
    y = (n + h_m) * cos_lat * sin_lon
    z = (n * (1 - e2) + h_m) * sin_lat

    return x, y, z


def _ecef_to_geodetic(
    x: float,
    y: float,
    z: float,
) -> tuple[float, float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    b = a * (1 - f)
    ep2 = (a * a - b * b) / (b * b)

    p = math.sqrt(x * x + y * y)
    lon = math.atan2(y, x)

    th = math.atan2(a * z, b * p)
    sin_th = math.sin(th)
    cos_th = math.cos(th)

    lat = math.atan2(
        z + ep2 * b * sin_th**3,
        p - e2 * a * cos_th**3,
    )

    sin_lat = math.sin(lat)
    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    h = p / math.cos(lat) - n

    return math.degrees(lat), math.degrees(lon), h


def _ecef_vector_from_dict(data: dict) -> tuple[float, float, float]:
    return (
        float(data.get("x", 0.0) or 0.0),
        float(data.get("y", 0.0) or 0.0),
        float(data.get("z", 0.0) or 0.0),
    )


def _vector_sub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vector_add(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vector_scale(
    v: tuple[float, float, float], s: float
) -> tuple[float, float, float]:
    return (v[0] * s, v[1] * s, v[2] * s)


def _vector_norm(v: tuple[float, float, float]) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def _ecef_delta_to_enu(
    dx: float,
    dy: float,
    dz: float,
    ref_lat_deg: float,
    ref_lon_deg: float,
) -> tuple[float, float, float]:
    lat = math.radians(ref_lat_deg)
    lon = math.radians(ref_lon_deg)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)

    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

    return east, north, up


def _solve_two_baseline_coefficients(
    target_offset: tuple[float, float, float],
    baseline_1: tuple[float, float, float],
    baseline_2: tuple[float, float, float],
) -> tuple[float, float, float]:
    b1x, b1y, b1z = baseline_1
    b2x, b2y, b2z = baseline_2
    tx, ty, tz = target_offset

    s11 = b1x * b1x + b1y * b1y + b1z * b1z
    s22 = b2x * b2x + b2y * b2y + b2z * b2z
    s12 = b1x * b2x + b1y * b2y + b1z * b2z

    r1 = b1x * tx + b1y * ty + b1z * tz
    r2 = b2x * tx + b2y * ty + b2z * tz

    det = s11 * s22 - s12 * s12

    if abs(det) < 1e-12:
        return 0.0, 0.0, float("inf")

    a = (r1 * s22 - r2 * s12) / det
    b = (s11 * r2 - s12 * r1) / det

    reconstructed = _vector_add(
        _vector_scale(baseline_1, a),
        _vector_scale(baseline_2, b),
    )
    residual = _vector_sub(target_offset, reconstructed)

    return a, b, _vector_norm(residual)


def _solve_two_baseline_coefficients_enu(
    master_xyz: tuple[float, float, float],
    aux1_xyz: tuple[float, float, float],
    aux2_xyz: tuple[float, float, float],
    target_xyz: tuple[float, float, float],
) -> tuple[float, float, float]:
    master_lat, master_lon, _ = _ecef_to_geodetic(*master_xyz)

    b1 = _vector_sub(aux1_xyz, master_xyz)
    b2 = _vector_sub(aux2_xyz, master_xyz)
    bt = _vector_sub(target_xyz, master_xyz)

    e1, n1, _ = _ecef_delta_to_enu(b1[0], b1[1], b1[2], master_lat, master_lon)
    e2, n2, _ = _ecef_delta_to_enu(b2[0], b2[1], b2[2], master_lat, master_lon)
    et, nt, _ = _ecef_delta_to_enu(bt[0], bt[1], bt[2], master_lat, master_lon)

    det = e1 * n2 - e2 * n1

    if abs(det) < 1e-8:
        return 0.0, 0.0, abs(det)

    coef_a = (et * n2 - e2 * nt) / det
    coef_b = (e1 * nt - et * n1) / det

    est_e = coef_a * e1 + coef_b * e2
    est_n = coef_a * n1 + coef_b * n2

    residual = math.sqrt((est_e - et) ** 2 + (est_n - nt) ** 2)
    return coef_a, coef_b, residual


def _normalize_satellite_id(sat: str) -> str:
    sat = (sat or "").strip().upper()
    if not sat:
        return sat

    if len(sat) >= 2 and sat[0] in {"G", "R", "E", "C"}:
        prefix = sat[0]
        suffix = sat[1:].strip()
        try:
            return f"{prefix}{int(suffix):02d}"
        except Exception:
            return sat

    return sat


def _get_satellite_system(sat: str) -> str:
    sat = _normalize_satellite_id(sat)
    if sat and sat[0] in {"G", "R", "E", "C"}:
        return sat[0]
    return "G"


def _detect_systems_in_seed(seed: dict) -> list[str]:
    systems = set()

    for epoch in seed.get("epochs", []) or []:
        for sat in epoch.get("satellites", []) or []:
            sat_id = sat.get("satellite")
            systems.add(_get_satellite_system(sat_id))

    ordered = [s for s in ["G", "R", "E", "C"] if s in systems]
    return ordered or ["G"]


def _inverse_distance_reference(
    distances: list[float],
    strong_nearest_base_bias: bool = True,
) -> list[float]:
    adjusted = []
    for d in distances:
        if d is None or d <= 0:
            adjusted.append(0.001)
        else:
            adjusted.append(float(d))

    power = 3.0 if strong_nearest_base_bias else 2.0
    inv = [1.0 / (d**power) for d in adjusted]
    total = sum(inv)

    if total == 0:
        return [1.0 / len(adjusted)] * len(adjusted)

    return [v / total for v in inv]


def _weights_for_available(
    reference_weights: list[float],
    available_indexes: list[int],
) -> list[float]:
    usable = [reference_weights[idx] for idx in available_indexes]
    total = sum(usable)

    if total == 0:
        return [1.0 / len(usable)] * len(usable)

    return [w / total for w in usable]


def _obs_family_factor(obs_name: str) -> float:
    obs_name = (obs_name or "").upper()

    if obs_name in {"C1", "P1", "C2", "P2"}:
        return 1.00

    if obs_name in {"L1", "L2"}:
        return 0.25

    if obs_name in {"S1", "S2"}:
        return 0.05

    return 0.0


def _weighted_mean(values: list[tuple[float, float]]) -> float | None:
    usable = [(v, w) for v, w in values if v is not None and w > 0]
    if not usable:
        return None

    sw = sum(w for _, w in usable)
    if sw <= 0:
        return None

    return sum(v * w for v, w in usable) / sw


def _stable_blend_master_and_network(
    master_value: float | None,
    aux1_value: float | None,
    aux2_value: float | None,
    coef_a: float,
    coef_b: float,
    obs_name: str,
) -> float | None:
    m = _safe_float(master_value)
    a1 = _safe_float(aux1_value)
    a2 = _safe_float(aux2_value)

    if m is None:
        return None

    family_factor = _obs_family_factor(obs_name)

    if a1 is None and a2 is None:
        return m

    corrections: list[tuple[float, float]] = []

    if a1 is not None:
        corrections.append((a1 - m, abs(coef_a)))

    if a2 is not None:
        corrections.append((a2 - m, abs(coef_b)))

    interpolated_correction = _weighted_mean(corrections)
    if interpolated_correction is None:
        return m

    corrected = m + family_factor * interpolated_correction

    if obs_name in {"L1", "L2"}:
        corrected = 0.85 * m + 0.15 * corrected
    elif obs_name in {"C1", "P1", "C2", "P2"}:
        corrected = 0.35 * m + 0.65 * corrected
    elif obs_name in {"S1", "S2"}:
        corrected = 0.90 * m + 0.10 * corrected

    return corrected


def _has_required_dual_frequency(obs: dict) -> bool:
    obs = obs or {}

    has_l1 = _safe_float(obs.get("L1")) is not None
    has_l2 = _safe_float(obs.get("L2")) is not None

    has_code_1 = (
        _safe_float(obs.get("C1")) is not None or _safe_float(obs.get("P1")) is not None
    )
    has_code_2 = (
        _safe_float(obs.get("C2")) is not None or _safe_float(obs.get("P2")) is not None
    )

    return has_l1 and has_l2 and has_code_1 and has_code_2


def _common_observation_types(
    master_numeric: dict,
    aux1_numeric: dict,
    aux2_numeric: dict,
) -> list[str]:
    obs1 = set(master_numeric.get("observation_types", []) or [])
    obs2 = set(aux1_numeric.get("observation_types", []) or [])
    obs3 = set(aux2_numeric.get("observation_types", []) or [])

    common = obs1 & obs2 & obs3
    ordered = [obs for obs in SUPPORTED_OBS_PRIORITY if obs in common]
    extra = sorted(common - set(ordered))
    return ordered + extra


def _build_approx_xyz_text(target: dict) -> str:
    x = target.get("x")
    y = target.get("y")
    z = target.get("z")

    if x is None or y is None or z is None:
        lat = _safe_float(target.get("lat"))
        lon = _safe_float(target.get("lon"))
        height = _safe_float(target.get("height"))

        if lat is not None and lon is not None and height is not None:
            x, y, z = _geodetic_to_ecef(lat, lon, height)

    if x is None or y is None or z is None:
        x, y, z = 0.0, 0.0, 0.0

    return f"{x:14.4f}{y:14.4f}{z:14.4f}"


def _pad_label(content: str, label: str) -> str:
    return f"{content[:60]:<60}{label:<20}"


def _format_obs_value_rnx2(value: float | None) -> str:
    if value is None:
        return " " * 16
    return f"{value:14.3f}  "


def _format_obs_value_rnx3(value: float | None) -> str:
    if value is None:
        return " " * 16
    return f"{value:14.3f}  "


def _format_rinex2_epoch_lines(
    dt: datetime,
    sat_ids: list[str],
) -> list[str]:
    yy = dt.year % 100
    sec = dt.second + dt.microsecond / 1_000_000

    prns = [sid[1:] for sid in sat_ids]
    first = (
        f" {yy:02d} {dt.month:02d} {dt.day:02d} {dt.hour:02d} {dt.minute:02d}"
        f"{sec:11.7f}  0{len(prns):3d}"
    )

    lines = []
    first_chunk = prns[:12]
    lines.append(first + "".join(f"{prn:>3}" for prn in first_chunk))

    remaining = prns[12:]
    while remaining:
        chunk = remaining[:12]
        remaining = remaining[12:]
        lines.append(" " * 32 + "".join(f"{prn:>3}" for prn in chunk))

    return lines


def _obs_code_value(obs: dict, band: str) -> float | None:
    obs = obs or {}

    if band == "1":
        for key in ["P1", "C1"]:
            val = _safe_float(obs.get(key))
            if val is not None:
                return val

    if band == "2":
        for key in ["P2", "C2"]:
            val = _safe_float(obs.get(key))
            if val is not None:
                return val

    return None


def _obs_phase_value(obs: dict, band: str) -> float | None:
    obs = obs or {}

    if band == "1":
        return _safe_float(obs.get("L1"))

    if band == "2":
        return _safe_float(obs.get("L2"))

    return None


def _estimate_delta_rho_from_code(
    master_obs: dict,
    aux_obs: dict,
    band: str,
) -> float | None:
    """
    Aproxima Δρ entre duas estações para um satélite/época usando código.
    Embute também parte de I/T, mas serve como observável bruto auxiliar.
    """
    m_code = _obs_code_value(master_obs, band)
    a_code = _obs_code_value(aux_obs, band)

    if m_code is None or a_code is None:
        return None

    return a_code - m_code


def _estimate_iono_proxy_dual_frequency(obs: dict) -> float | None:
    """
    Proxy ionosférico simples por combinação geométrica entre bandas 1 e 2.
    Não depende de frequência explícita em Hz; aqui usamos apenas um indicador
    diferencial robusto e consistente entre bases.
    """
    c1 = _obs_code_value(obs, "1")
    c2 = _obs_code_value(obs, "2")
    l1 = _obs_phase_value(obs, "1")
    l2 = _obs_phase_value(obs, "2")

    usable = []

    if c1 is not None and c2 is not None:
        usable.append(c2 - c1)

    if l1 is not None and l2 is not None:
        usable.append(l1 - l2)

    if not usable:
        return None

    return sum(usable) / len(usable)


def _estimate_tropo_proxy(obs: dict) -> float | None:
    """
    Proxy troposférico residual simples.
    Como não temos aqui elevação do satélite nem mapeamento troposférico rigoroso,
    usamos a coerência média código-fase como componente lenta.
    """
    c1 = _obs_code_value(obs, "1")
    c2 = _obs_code_value(obs, "2")
    l1 = _obs_phase_value(obs, "1")
    l2 = _obs_phase_value(obs, "2")

    values = []

    if c1 is not None and l1 is not None:
        values.append(c1 - l1)

    if c2 is not None and l2 is not None:
        values.append(c2 - l2)

    if not values:
        return None

    return sum(values) / len(values)


def _weighted_delta(
    master_obs: dict,
    aux1_obs: dict,
    aux2_obs: dict,
    coef_a: float,
    coef_b: float,
    estimator,
) -> float | None:
    """
    Combina diferença entre mestre e auxiliares usando os coeficientes geométricos.
    """
    d1 = estimator(master_obs, aux1_obs)
    d2 = estimator(master_obs, aux2_obs)

    usable = []
    if d1 is not None:
        usable.append((d1, abs(coef_a), coef_a))

    if d2 is not None:
        usable.append((d2, abs(coef_b), coef_b))

    if not usable:
        return None

    total_w = sum(max(w, 1e-9) for _, w, _ in usable)
    if total_w <= 0:
        return None

    value = 0.0
    for delta, weight_abs, signed_coef in usable:
        normalized_w = max(weight_abs, 1e-9) / total_w
        signed_w = normalized_w * (1 if signed_coef >= 0 else -1)
        value += delta * signed_w

    return value


def _estimate_delta_iono(
    master_obs: dict,
    aux1_obs: dict,
    aux2_obs: dict,
    coef_a: float,
    coef_b: float,
) -> float:
    def _est(master: dict, aux: dict) -> float | None:
        m = _estimate_iono_proxy_dual_frequency(master)
        a = _estimate_iono_proxy_dual_frequency(aux)
        if m is None or a is None:
            return None
        return a - m

    val = _weighted_delta(master_obs, aux1_obs, aux2_obs, coef_a, coef_b, _est)
    return float(val or 0.0)


def _estimate_delta_tropo(
    master_obs: dict,
    aux1_obs: dict,
    aux2_obs: dict,
    coef_a: float,
    coef_b: float,
) -> float:
    def _est(master: dict, aux: dict) -> float | None:
        m = _estimate_tropo_proxy(master)
        a = _estimate_tropo_proxy(aux)
        if m is None or a is None:
            return None
        return a - m

    val = _weighted_delta(master_obs, aux1_obs, aux2_obs, coef_a, coef_b, _est)
    return float(val or 0.0)


def _estimate_delta_rho_band(
    master_obs: dict,
    aux1_obs: dict,
    aux2_obs: dict,
    coef_a: float,
    coef_b: float,
    band: str,
) -> float:
    def _est(master: dict, aux: dict) -> float | None:
        return _estimate_delta_rho_from_code(master, aux, band)

    val = _weighted_delta(master_obs, aux1_obs, aux2_obs, coef_a, coef_b, _est)
    return float(val or 0.0)


def _apply_physical_vrs_correction(
    obs_name: str,
    master_obs: dict,
    aux1_obs: dict,
    aux2_obs: dict,
    coef_a: float,
    coef_b: float,
) -> float | None:
    """
    Modelo simplificado:
      Código  : P_vrs ~= P_master + Δρ + ΔT + ΔI
      Fase    : L_vrs ~= L_master + α*(Δρ + ΔT - ΔI)
    onde α<1 para evitar explosão numérica nas fases.
    """
    obs_name = (obs_name or "").upper()

    delta_i = _estimate_delta_iono(master_obs, aux1_obs, aux2_obs, coef_a, coef_b)
    delta_t = _estimate_delta_tropo(master_obs, aux1_obs, aux2_obs, coef_a, coef_b)

    if obs_name in {"C1", "P1"}:
        base = _obs_code_value(master_obs, "1")
        if base is None:
            return None
        delta_rho = _estimate_delta_rho_band(
            master_obs, aux1_obs, aux2_obs, coef_a, coef_b, "1"
        )
        return base + delta_rho + delta_t + delta_i

    if obs_name in {"C2", "P2"}:
        base = _obs_code_value(master_obs, "2")
        if base is None:
            return None
        delta_rho = _estimate_delta_rho_band(
            master_obs, aux1_obs, aux2_obs, coef_a, coef_b, "2"
        )
        return base + delta_rho + delta_t + delta_i

    if obs_name == "L1":
        base = _obs_phase_value(master_obs, "1")
        if base is None:
            return None
        delta_rho = _estimate_delta_rho_band(
            master_obs, aux1_obs, aux2_obs, coef_a, coef_b, "1"
        )
        phase_factor = 0.20
        return base + phase_factor * (delta_rho + delta_t - delta_i)

    if obs_name == "L2":
        base = _obs_phase_value(master_obs, "2")
        if base is None:
            return None
        delta_rho = _estimate_delta_rho_band(
            master_obs, aux1_obs, aux2_obs, coef_a, coef_b, "2"
        )
        phase_factor = 0.20
        return base + phase_factor * (delta_rho + delta_t - delta_i)

    if obs_name == "S1":
        return _safe_float(master_obs.get("S1"))

    if obs_name == "S2":
        return _safe_float(master_obs.get("S2"))

    return _safe_float(master_obs.get(obs_name))


def build_geometry_based_network_vrs_seed(
    master_numeric: dict,
    aux1_numeric: dict,
    aux2_numeric: dict,
    target_meta: dict,
    max_epochs: int | None = None,
    min_satellites_per_epoch: int = 4,
    require_dual_frequency: bool = False,
    priority_obs: list[str] | None = None,
) -> dict:
    """
    Geração VRS física simplificada, por satélite e por época.

    Estratégia:
    - a base mestre continua sendo a espinha dorsal;
    - os auxiliares entram como correção diferencial;
    - os coeficientes geométricos são resolvidos no plano ENU;
    - para cada satélite/época:
        * estima Δρ por banda a partir dos códigos
        * estima ΔI por proxy dual-frequency
        * estima ΔT por proxy residual lenta
        * corrige código e fase separadamente
    """

    priority_obs = priority_obs or DEFAULT_PRIORITY_OBS

    master_epochs = _index_epochs(master_numeric)
    aux1_epochs = _index_epochs(aux1_numeric)
    aux2_epochs = _index_epochs(aux2_numeric)

    common_epoch_keys = sorted(
        set(master_epochs.keys()) & set(aux1_epochs.keys()) & set(aux2_epochs.keys())
    )

    if max_epochs is not None:
        common_epoch_keys = common_epoch_keys[:max_epochs]

    common_obs_types = _common_observation_types(
        master_numeric,
        aux1_numeric,
        aux2_numeric,
    )

    usable_obs_types = [
        obs
        for obs in common_obs_types
        if obs in ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]
    ]

    if not usable_obs_types:
        raise ValueError(
            "Não há observáveis comuns suficientes para gerar a VRS geométrica."
        )

    master_xyz = _ecef_vector_from_dict(target_meta.get("master_xyz") or {})
    aux1_xyz = _ecef_vector_from_dict(target_meta.get("aux1_xyz") or {})
    aux2_xyz = _ecef_vector_from_dict(target_meta.get("aux2_xyz") or {})
    target_xyz = _ecef_vector_from_dict(
        {
            "x": target_meta.get("x"),
            "y": target_meta.get("y"),
            "z": target_meta.get("z"),
        }
    )

    coef_a, coef_b, residual_norm = _solve_two_baseline_coefficients_enu(
        master_xyz,
        aux1_xyz,
        aux2_xyz,
        target_xyz,
    )

    coef_a = max(-3.0, min(3.0, coef_a))
    coef_b = max(-3.0, min(3.0, coef_b))

    seed_epochs = []
    delta_diagnostics = []

    for epoch_iso in common_epoch_keys:
        master_epoch = master_epochs[epoch_iso]
        aux1_epoch = aux1_epochs[epoch_iso]
        aux2_epoch = aux2_epochs[epoch_iso]

        master_sats = _index_satellite_records(master_epoch)
        aux1_sats = _index_satellite_records(aux1_epoch)
        aux2_sats = _index_satellite_records(aux2_epoch)

        epoch_satellites = []
        epoch_debug = {
            "epoch": epoch_iso,
            "satellites": [],
        }

        for sat_id in sorted(master_sats.keys()):
            norm_sat = _normalize_satellite_id(sat_id)

            if _get_satellite_system(norm_sat) != "G":
                continue

            master_obs = (master_sats.get(sat_id) or {}).get("observations", {}) or {}
            aux1_obs = (aux1_sats.get(sat_id) or {}).get("observations", {}) or {}
            aux2_obs = (aux2_sats.get(sat_id) or {}).get("observations", {}) or {}

            corrected_obs = {}

            for obs_name in usable_obs_types:
                corrected_obs[obs_name] = _apply_physical_vrs_correction(
                    obs_name=obs_name,
                    master_obs=master_obs,
                    aux1_obs=aux1_obs,
                    aux2_obs=aux2_obs,
                    coef_a=coef_a,
                    coef_b=coef_b,
                )

            nn = _count_non_null(corrected_obs)
            if nn == 0:
                continue

            if require_dual_frequency and not _has_required_dual_frequency(
                corrected_obs
            ):
                continue

            priority_count = sum(
                1 for k in priority_obs if _safe_float(corrected_obs.get(k)) is not None
            )

            epoch_satellites.append(
                {
                    "satellite": norm_sat,
                    "available_observation_count": nn,
                    "priority_obs_count": priority_count,
                    "observations": corrected_obs,
                }
            )

            epoch_debug["satellites"].append(
                {
                    "satellite": norm_sat,
                    "delta_rho_l1": _estimate_delta_rho_band(
                        master_obs, aux1_obs, aux2_obs, coef_a, coef_b, "1"
                    ),
                    "delta_rho_l2": _estimate_delta_rho_band(
                        master_obs, aux1_obs, aux2_obs, coef_a, coef_b, "2"
                    ),
                    "delta_iono": _estimate_delta_iono(
                        master_obs, aux1_obs, aux2_obs, coef_a, coef_b
                    ),
                    "delta_tropo": _estimate_delta_tropo(
                        master_obs, aux1_obs, aux2_obs, coef_a, coef_b
                    ),
                    "priority_obs_count": priority_count,
                }
            )

        epoch_satellites.sort(
            key=lambda item: (
                item.get("priority_obs_count", 0),
                item.get("available_observation_count", 0),
                item.get("satellite", ""),
            ),
            reverse=True,
        )

        if len(epoch_satellites) >= min_satellites_per_epoch:
            seed_epochs.append(
                {
                    "epoch": epoch_iso,
                    "generated_satellite_count": len(epoch_satellites),
                    "satellites": epoch_satellites,
                }
            )
            delta_diagnostics.append(epoch_debug)

    return {
        "processing_mode": "geometry_based_network_vrs",
        "method": "geometry_based_network_vrs_seed_v4_physical_delta_model",
        "target": target_meta,
        "geometry_coefficients": {
            "coef_a": coef_a,
            "coef_b": coef_b,
            "residual_norm_m": residual_norm,
        },
        "master_based": True,
        "master_station_name": target_meta.get("master_station_name"),
        "common_observation_types": usable_obs_types,
        "common_epoch_count": len(common_epoch_keys),
        "generated_epoch_count": len(seed_epochs),
        "systems": ["G"] if seed_epochs else [],
        "epochs": seed_epochs,
        "delta_model_diagnostics": {
            "epoch_count": len(delta_diagnostics),
            "sample_epochs": delta_diagnostics[:20],
        },
    }


def build_virtual_observation_seed(
    base1_numeric: dict,
    base2_numeric: dict,
    base3_numeric: dict,
    base1_distance_m: float,
    base2_distance_m: float,
    base3_distance_m: float,
    target_meta: dict,
    max_epochs: int | None = None,
    min_bases_per_observation: int = 2,
    min_satellites_per_epoch: int = 5,
    min_satellite_presence_ratio: float = 0.45,
    min_satellite_longest_arc: int = 80,
    max_satellites_per_epoch: int = 8,
    local_continuity_window: int = 2,
    min_valid_in_local_window: int = 4,
    prefer_nearest_base: bool = True,
    strong_nearest_base_bias: bool = True,
    require_dual_frequency: bool = True,
    priority_obs: list[str] | None = None,
) -> dict:
    base1_epochs = _index_epochs(base1_numeric)
    base2_epochs = _index_epochs(base2_numeric)
    base3_epochs = _index_epochs(base3_numeric)

    common_epochs = sorted(
        set(base1_epochs.keys()) & set(base2_epochs.keys()) & set(base3_epochs.keys())
    )

    if max_epochs is not None:
        common_epochs = common_epochs[:max_epochs]

    common_obs_types = _common_observation_types(
        base1_numeric,
        base2_numeric,
        base3_numeric,
    )

    reference_weights = _inverse_distance_reference(
        [base1_distance_m, base2_distance_m, base3_distance_m],
        strong_nearest_base_bias=strong_nearest_base_bias,
    )

    seed_epochs = []

    for epoch_iso in common_epochs:
        e1 = base1_epochs[epoch_iso]
        e2 = base2_epochs[epoch_iso]
        e3 = base3_epochs[epoch_iso]

        s1 = _index_satellite_records(e1)
        s2 = _index_satellite_records(e2)
        s3 = _index_satellite_records(e3)

        union_sats = sorted(set(s1.keys()) | set(s2.keys()) | set(s3.keys()))
        synthesized_satellites = []

        for sat in union_sats:
            obs1 = (s1.get(sat) or {}).get("observations", {}) or {}
            obs2 = (s2.get(sat) or {}).get("observations", {}) or {}
            obs3 = (s3.get(sat) or {}).get("observations", {}) or {}

            synthesized = {}

            for obs_name in common_obs_types:
                values = [
                    _safe_float(obs1.get(obs_name)),
                    _safe_float(obs2.get(obs_name)),
                    _safe_float(obs3.get(obs_name)),
                ]
                available_indexes = [
                    idx for idx, value in enumerate(values) if value is not None
                ]

                if len(available_indexes) < min_bases_per_observation:
                    synthesized[obs_name] = None
                    continue

                weights = _weights_for_available(reference_weights, available_indexes)

                value = 0.0
                for local_weight, source_index in zip(weights, available_indexes):
                    value += values[source_index] * local_weight

                synthesized[obs_name] = value

            nn = _count_non_null(synthesized)
            if nn == 0:
                continue

            if require_dual_frequency and not _has_required_dual_frequency(synthesized):
                continue

            synthesized_satellites.append(
                {
                    "satellite": _normalize_satellite_id(sat),
                    "available_observation_count": nn,
                    "observations": synthesized,
                }
            )

        synthesized_satellites.sort(
            key=lambda item: (
                item.get("available_observation_count", 0),
                item.get("satellite", ""),
            ),
            reverse=True,
        )
        synthesized_satellites = synthesized_satellites[:max_satellites_per_epoch]

        if len(synthesized_satellites) >= min_satellites_per_epoch:
            seed_epochs.append(
                {
                    "epoch": epoch_iso,
                    "generated_satellite_count": len(synthesized_satellites),
                    "satellites": synthesized_satellites,
                }
            )

    seed = {
        "processing_mode": "network_only_virtual_seed",
        "method": "inverse_distance_weighting_auxiliary_only",
        "target": target_meta,
        "weights_reference": {
            "base_1": reference_weights[0],
            "base_2": reference_weights[1],
            "base_3": reference_weights[2],
        },
        "common_observation_types": common_obs_types,
        "common_epoch_count": len(common_epochs),
        "generated_epoch_count": len(seed_epochs),
        "systems": _detect_systems_in_seed({"epochs": seed_epochs}),
        "epochs": seed_epochs,
    }
    return seed


def build_master_based_network_corrected_seed(
    master_numeric: dict,
    aux1_numeric: dict,
    aux2_numeric: dict,
    master_distance_m: float,
    aux1_distance_m: float,
    aux2_distance_m: float,
    target_meta: dict,
    max_epochs: int | None = None,
    min_satellites_per_epoch: int = 4,
    require_dual_frequency: bool = True,
    priority_obs: list[str] | None = None,
) -> dict:
    priority_obs = priority_obs or DEFAULT_PRIORITY_OBS

    master_epochs = _index_epochs(master_numeric)
    aux1_epochs = _index_epochs(aux1_numeric)
    aux2_epochs = _index_epochs(aux2_numeric)

    common_epoch_keys = sorted(
        set(master_epochs.keys()) & set(aux1_epochs.keys()) & set(aux2_epochs.keys())
    )

    if max_epochs is not None:
        common_epoch_keys = common_epoch_keys[:max_epochs]

    common_obs_types = _common_observation_types(
        master_numeric,
        aux1_numeric,
        aux2_numeric,
    )

    usable_obs_types = [
        obs
        for obs in common_obs_types
        if obs in ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]
    ]

    if not usable_obs_types:
        raise ValueError(
            "Não há observáveis compatíveis para gerar o seed final master-based."
        )

    target_weights = _inverse_distance_reference(
        [master_distance_m, aux1_distance_m, aux2_distance_m],
        strong_nearest_base_bias=False,
    )

    seed_epochs = []

    for epoch_iso in common_epoch_keys:
        master_epoch = master_epochs[epoch_iso]
        aux1_epoch = aux1_epochs[epoch_iso]
        aux2_epoch = aux2_epochs[epoch_iso]

        master_sats = _index_satellite_records(master_epoch)
        aux1_sats = _index_satellite_records(aux1_epoch)
        aux2_sats = _index_satellite_records(aux2_epoch)

        epoch_satellites = []

        for sat_id in sorted(master_sats.keys()):
            norm_sat = _normalize_satellite_id(sat_id)

            if _get_satellite_system(norm_sat) != "G":
                continue

            master_obs = (master_sats.get(sat_id) or {}).get("observations", {}) or {}
            aux1_obs = (aux1_sats.get(sat_id) or {}).get("observations", {}) or {}
            aux2_obs = (aux2_sats.get(sat_id) or {}).get("observations", {}) or {}

            corrected_obs = {}

            for obs_name in usable_obs_types:
                m = _safe_float(master_obs.get(obs_name))
                a1 = _safe_float(aux1_obs.get(obs_name))
                a2 = _safe_float(aux2_obs.get(obs_name))

                if m is None:
                    corrected_obs[obs_name] = None
                    continue

                values = [m, a1, a2]
                available_indexes = [
                    idx for idx, value in enumerate(values) if value is not None
                ]

                if len(available_indexes) == 1:
                    corrected_obs[obs_name] = m
                    continue

                weights = _weights_for_available(target_weights, available_indexes)
                interpolated_value = 0.0
                for local_weight, source_index in zip(weights, available_indexes):
                    interpolated_value += values[source_index] * local_weight

                corrected_obs[obs_name] = interpolated_value

            nn = _count_non_null(corrected_obs)
            if nn == 0:
                continue

            if require_dual_frequency and not _has_required_dual_frequency(
                corrected_obs
            ):
                continue

            epoch_satellites.append(
                {
                    "satellite": norm_sat,
                    "available_observation_count": nn,
                    "priority_obs_count": sum(
                        1
                        for k in priority_obs
                        if _safe_float(corrected_obs.get(k)) is not None
                    ),
                    "observations": corrected_obs,
                }
            )

        epoch_satellites.sort(
            key=lambda item: (
                item.get("priority_obs_count", 0),
                item.get("available_observation_count", 0),
                item.get("satellite", ""),
            ),
            reverse=True,
        )

        if len(epoch_satellites) >= min_satellites_per_epoch:
            seed_epochs.append(
                {
                    "epoch": epoch_iso,
                    "generated_satellite_count": len(epoch_satellites),
                    "satellites": epoch_satellites,
                }
            )

    seed = {
        "processing_mode": "master_based_network_correction",
        "method": "master_based_target_interpolation_v2",
        "target": target_meta,
        "master_based": True,
        "master_station_name": target_meta.get("master_station_name"),
        "common_observation_types": usable_obs_types,
        "common_epoch_count": len(common_epoch_keys),
        "generated_epoch_count": len(seed_epochs),
        "systems": ["G"] if seed_epochs else [],
        "epochs": seed_epochs,
    }

    return seed


def summarize_virtual_observation_seed(seed: dict) -> dict:
    epochs = seed.get("epochs", []) or []
    obs_types = seed.get("common_observation_types", []) or []

    max_sat_count = 0
    epochs_with_satellites = 0
    per_obs_non_null = {obs: 0 for obs in obs_types}
    total_satellite_instances = 0

    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        if sats:
            epochs_with_satellites += 1

        sat_count = len(sats)
        total_satellite_instances += sat_count
        max_sat_count = max(max_sat_count, sat_count)

        for sat in sats:
            observations = sat.get("observations", {}) or {}
            for obs_name, value in observations.items():
                if value is not None:
                    per_obs_non_null[obs_name] = per_obs_non_null.get(obs_name, 0) + 1

    avg_satellites_per_epoch = (
        total_satellite_instances / len(epochs) if epochs else 0.0
    )

    return {
        "processing_mode": seed.get("processing_mode"),
        "method": seed.get("method"),
        "systems": seed.get("systems", ["G"]),
        "common_epoch_count": seed.get("common_epoch_count", 0),
        "generated_epoch_count": seed.get("generated_epoch_count", 0),
        "epochs_with_satellites": epochs_with_satellites,
        "max_satellite_count_per_epoch": max_sat_count,
        "avg_satellites_per_epoch": avg_satellites_per_epoch,
        "common_observation_types": obs_types,
        "per_observation_non_null": per_obs_non_null,
    }


def select_best_ppp_ready_block(
    seed: dict,
    min_run_epochs: int = 120,
    min_satellites_keep: int = 6,
) -> dict:
    epochs = seed.get("epochs", []) or []
    if not epochs:
        return {
            "selected_epochs": [],
            "selected_epoch_count": 0,
            "selected_start": None,
            "selected_end": None,
            "score": 0.0,
        }

    scored = []
    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        sat_count = len(sats)
        score = float(sat_count)
        scored.append(
            {
                "epoch": epoch,
                "score": score,
                "sat_count": sat_count,
            }
        )

    best_block = []
    current_block = []

    for item in scored:
        if item["sat_count"] >= min_satellites_keep:
            current_block.append(item)
        else:
            if len(current_block) >= min_run_epochs:
                if sum(x["score"] for x in current_block) > sum(
                    x["score"] for x in best_block
                ):
                    best_block = current_block[:]
            current_block = []

    if len(current_block) >= min_run_epochs:
        if sum(x["score"] for x in current_block) > sum(x["score"] for x in best_block):
            best_block = current_block[:]

    if not best_block:
        best_block = [x for x in scored if x["sat_count"] >= min_satellites_keep]

    selected_epochs = [x["epoch"] for x in best_block]

    return {
        "selected_epochs": selected_epochs,
        "selected_epoch_count": len(selected_epochs),
        "selected_start": selected_epochs[0]["epoch"] if selected_epochs else None,
        "selected_end": selected_epochs[-1]["epoch"] if selected_epochs else None,
        "score": sum(x["score"] for x in best_block),
    }


def filter_seed_by_selected_epochs(seed: dict, selected_epoch_isos: list[str]) -> dict:
    selected_set = set(selected_epoch_isos or [])

    filtered_epochs = [
        epoch
        for epoch in (seed.get("epochs", []) or [])
        if epoch.get("epoch") in selected_set
    ]

    cloned = dict(seed)
    cloned["epochs"] = filtered_epochs
    cloned["generated_epoch_count"] = len(filtered_epochs)
    cloned["selected_epoch_count"] = len(filtered_epochs)
    cloned["systems"] = _detect_systems_in_seed(cloned)

    return cloned


def harden_ppp_seed_for_ibge(
    seed: dict,
    min_satellite_presence_ratio: float = 0.85,
    min_satellite_longest_arc: int = 180,
    min_satellites_per_epoch: int = 6,
    min_priority_obs_per_satellite: int = 4,
    min_block_length_epochs: int = 120,
    max_epoch_gap_inside_block: int = 1,
    priority_obs: list[str] | None = None,
) -> dict:
    priority_obs = priority_obs or DEFAULT_PRIORITY_OBS

    epochs = seed.get("epochs", []) or []
    if not epochs:
        cloned = dict(seed)
        cloned["epochs"] = []
        cloned["generated_epoch_count"] = 0
        cloned["ppp_hardening"] = {
            "removed_epochs": 0,
            "removed_satellites": [],
            "kept_satellites": [],
            "selected_block_start": None,
            "selected_block_end": None,
            "selected_block_length_epochs": 0,
        }
        return cloned

    def _epoch_dt(epoch_record: dict) -> datetime:
        return _extract_epoch_datetime(epoch_record["epoch"])

    def _sat_is_valid_for_ppp(sat: dict) -> bool:
        sat_id = _normalize_satellite_id(sat.get("satellite"))
        if not sat_id or _get_satellite_system(sat_id) != "G":
            return False

        obs = sat.get("observations", {}) or {}

        if not _has_required_dual_frequency(obs):
            return False

        priority_count = sum(
            1 for obs_name in priority_obs if _safe_float(obs.get(obs_name)) is not None
        )
        if priority_count < min_priority_obs_per_satellite:
            return False

        return True

    def _build_satellite_presence_map(epochs_list: list[dict]) -> dict[str, list[int]]:
        sat_map: dict[str, list[int]] = defaultdict(list)

        for idx, epoch in enumerate(epochs_list):
            sats = epoch.get("satellites", []) or []
            for sat in sats:
                if not _sat_is_valid_for_ppp(sat):
                    continue
                sat_id = _normalize_satellite_id(sat.get("satellite"))
                sat_map[sat_id].append(idx)

        return sat_map

    def _compute_longest_consecutive_arc(indexes: list[int]) -> int:
        if not indexes:
            return 0

        longest = 1
        current = 1

        for i in range(1, len(indexes)):
            if indexes[i] - indexes[i - 1] <= max_epoch_gap_inside_block:
                current += 1
            else:
                longest = max(longest, current)
                current = 1

        longest = max(longest, current)
        return longest

    def _find_best_continuous_epoch_block(
        epoch_candidates: list[dict],
    ) -> tuple[int, int]:
        if not epoch_candidates:
            return -1, -1

        if len(epoch_candidates) == 1:
            return 0, 0

        best_start = 0
        best_end = 0
        current_start = 0

        interval_guess = _estimate_interval_seconds(epoch_candidates)

        for i in range(1, len(epoch_candidates)):
            prev_dt = _epoch_dt(epoch_candidates[i - 1])
            curr_dt = _epoch_dt(epoch_candidates[i])
            delta_sec = (curr_dt - prev_dt).total_seconds()

            if delta_sec > interval_guess * (max_epoch_gap_inside_block + 1.5):
                if (i - 1 - current_start) > (best_end - best_start):
                    best_start = current_start
                    best_end = i - 1
                current_start = i

        if (len(epoch_candidates) - 1 - current_start) > (best_end - best_start):
            best_start = current_start
            best_end = len(epoch_candidates) - 1

        return best_start, best_end

    total_epochs = len(epochs)

    sat_presence_map = _build_satellite_presence_map(epochs)

    kept_satellites: list[str] = []
    removed_satellites: list[dict] = []

    for sat_id, epoch_indexes in sat_presence_map.items():
        presence_ratio = len(epoch_indexes) / total_epochs if total_epochs > 0 else 0.0
        longest_arc = _compute_longest_consecutive_arc(epoch_indexes)

        if (
            presence_ratio >= min_satellite_presence_ratio
            and longest_arc >= min_satellite_longest_arc
        ):
            kept_satellites.append(sat_id)
        else:
            removed_satellites.append(
                {
                    "satellite": sat_id,
                    "presence_ratio": round(presence_ratio, 4),
                    "longest_arc": int(longest_arc),
                }
            )

    kept_satellites = sorted(set(kept_satellites))

    prelim_epochs = []
    removed_epochs_pre_block = 0

    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        filtered_sats = []

        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if sat_id not in kept_satellites:
                continue

            if not _sat_is_valid_for_ppp(sat):
                continue

            filtered_sats.append(
                {
                    **sat,
                    "satellite": sat_id,
                }
            )

        filtered_sats.sort(key=lambda item: item.get("satellite", ""))

        if len(filtered_sats) >= min_satellites_per_epoch:
            prelim_epochs.append(
                {
                    **epoch,
                    "satellites": filtered_sats,
                    "generated_satellite_count": len(filtered_sats),
                }
            )
        else:
            removed_epochs_pre_block += 1

    if not prelim_epochs:
        cloned = dict(seed)
        cloned["epochs"] = []
        cloned["generated_epoch_count"] = 0
        cloned["systems"] = []
        cloned["ppp_hardening"] = {
            "removed_epochs": total_epochs,
            "removed_satellites": removed_satellites,
            "kept_satellites": kept_satellites,
            "selected_block_start": None,
            "selected_block_end": None,
            "selected_block_length_epochs": 0,
            "reason": "Nenhuma época sobreviveu ao endurecimento preliminar.",
        }
        return cloned

    block_start_idx, block_end_idx = _find_best_continuous_epoch_block(prelim_epochs)
    block_epochs = prelim_epochs[block_start_idx : block_end_idx + 1]

    if len(block_epochs) < min_block_length_epochs:
        selected_epochs = prelim_epochs
        selected_block_start = prelim_epochs[0]["epoch"]
        selected_block_end = prelim_epochs[-1]["epoch"]
    else:
        selected_epochs = block_epochs
        selected_block_start = block_epochs[0]["epoch"]
        selected_block_end = block_epochs[-1]["epoch"]

    block_sat_presence_map = _build_satellite_presence_map(selected_epochs)
    block_total_epochs = len(selected_epochs)

    final_kept_satellites: list[str] = []
    additionally_removed_satellites: list[dict] = []

    for sat_id, epoch_indexes in block_sat_presence_map.items():
        presence_ratio = (
            len(epoch_indexes) / block_total_epochs if block_total_epochs > 0 else 0.0
        )
        longest_arc = _compute_longest_consecutive_arc(epoch_indexes)

        if (
            presence_ratio >= min_satellite_presence_ratio
            and longest_arc >= min_satellite_longest_arc
        ):
            final_kept_satellites.append(sat_id)
        else:
            additionally_removed_satellites.append(
                {
                    "satellite": sat_id,
                    "presence_ratio": round(presence_ratio, 4),
                    "longest_arc": int(longest_arc),
                    "stage": "block_refinement",
                }
            )

    final_kept_satellites = sorted(set(final_kept_satellites))

    final_epochs = []
    removed_epochs_final = 0

    for epoch in selected_epochs:
        sats = epoch.get("satellites", []) or []
        final_sats = []

        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if sat_id not in final_kept_satellites:
                continue

            if not _sat_is_valid_for_ppp(sat):
                continue

            final_sats.append(
                {
                    **sat,
                    "satellite": sat_id,
                }
            )

        final_sats.sort(key=lambda item: item.get("satellite", ""))

        if len(final_sats) >= min_satellites_per_epoch:
            final_epochs.append(
                {
                    **epoch,
                    "satellites": final_sats,
                    "generated_satellite_count": len(final_sats),
                }
            )
        else:
            removed_epochs_final += 1

    cloned = dict(seed)
    cloned["epochs"] = final_epochs
    cloned["generated_epoch_count"] = len(final_epochs)
    cloned["systems"] = ["G"] if final_epochs else []
    cloned["ppp_hardening"] = {
        "removed_epochs": int(removed_epochs_pre_block + removed_epochs_final),
        "removed_satellites": removed_satellites + additionally_removed_satellites,
        "kept_satellites": final_kept_satellites,
        "selected_block_start": selected_block_start,
        "selected_block_end": selected_block_end,
        "selected_block_length_epochs": len(final_epochs),
        "min_satellite_presence_ratio": min_satellite_presence_ratio,
        "min_satellite_longest_arc": min_satellite_longest_arc,
        "min_satellites_per_epoch": min_satellites_per_epoch,
        "min_priority_obs_per_satellite": min_priority_obs_per_satellite,
        "min_block_length_epochs": min_block_length_epochs,
        "max_epoch_gap_inside_block": max_epoch_gap_inside_block,
    }

    return cloned


def write_virtual_observations_json(output_path: str, seed: dict) -> str:
    path = Path(output_path)
    path.write_text(
        json.dumps(seed, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


def _write_rinex_v3_mixed_obs(
    output_path: str,
    seed: dict,
    marker_name: str = "ORBIVRS",
) -> str:
    path = Path(output_path)

    obs_types = seed.get("common_observation_types", []) or []
    epochs = seed.get("epochs", []) or []
    target = seed.get("target", {}) or {}
    systems = seed.get("systems") or _detect_systems_in_seed(seed)

    if not epochs:
        raise ValueError("Não há épocas válidas para escrever o RINEX 3.04.")

    first_dt = _extract_epoch_datetime(epochs[0]["epoch"])
    last_dt = _extract_epoch_datetime(epochs[-1]["epoch"])
    interval = _estimate_interval_seconds(epochs)

    lines: list[str] = []

    lines.append(
        _pad_label(
            f"{'3.04':>9}           OBSERVATION DATA    M", "RINEX VERSION / TYPE"
        )
    )
    lines.append(
        _pad_label(
            f"{'OrbitBase':<20}{'OrbitBase':<20}{datetime.utcnow().strftime('%Y%m%d %H%M%S UTC'):<20}",
            "PGM / RUN BY / DATE",
        )
    )
    lines.append(_pad_label(marker_name[:60], "MARKER NAME"))
    lines.append(_pad_label("GEODETIC", "MARKER TYPE"))
    lines.append(
        _pad_label(f"{'OrbitBase':<20}{'Biome Projetos':<40}", "OBSERVER / AGENCY")
    )
    lines.append(
        _pad_label(
            f"{'VRS':<20}{'ORBITBASE VRS':<20}{'1.0':<20}", "REC # / TYPE / VERS"
        )
    )
    lines.append(_pad_label(f"{'VRS':<20}{'VIRTUAL':<40}", "ANT # / TYPE"))
    lines.append(_pad_label(_build_approx_xyz_text(target), "APPROX POSITION XYZ"))
    lines.append(
        _pad_label(f"{0.0:14.4f}{0.0:14.4f}{0.0:14.4f}", "ANTENNA: DELTA H/E/N")
    )

    for system in systems:
        sys_obs = obs_types[:]
        first_chunk = sys_obs[:13]
        line = f"{system:1s}{len(sys_obs):5d}" + "".join(
            f"{obs:>4}" for obs in first_chunk
        )
        lines.append(_pad_label(line, "SYS / # / OBS TYPES"))

        remaining = sys_obs[13:]
        while remaining:
            chunk = remaining[:13]
            remaining = remaining[13:]
            line = " " * 6 + "".join(f"{obs:>4}" for obs in chunk)
            lines.append(_pad_label(line, "SYS / # / OBS TYPES"))

    lines.append(
        _pad_label(
            f"{first_dt.year:6d}{first_dt.month:6d}{first_dt.day:6d}{first_dt.hour:6d}{first_dt.minute:6d}{first_dt.second + first_dt.microsecond / 1_000_000:13.7f}{'GPS':>8}",
            "TIME OF FIRST OBS",
        )
    )
    lines.append(
        _pad_label(
            f"{last_dt.year:6d}{last_dt.month:6d}{last_dt.day:6d}{last_dt.hour:6d}{last_dt.minute:6d}{last_dt.second + last_dt.microsecond / 1_000_000:13.7f}{'GPS':>8}",
            "TIME OF LAST OBS",
        )
    )
    lines.append(_pad_label(f"{interval:10.3f}", "INTERVAL"))
    lines.append(_pad_label("", "END OF HEADER"))

    for epoch in epochs:
        epoch_iso = epoch.get("epoch")
        sats = epoch.get("satellites", []) or []
        if not epoch_iso or not sats:
            continue

        dt = _extract_epoch_datetime(epoch_iso)
        sat_ids = sorted(
            _normalize_satellite_id(s["satellite"]) for s in sats if s.get("satellite")
        )
        lines.append(
            f"> {dt.year:4d} {dt.month:02d} {dt.day:02d} {dt.hour:02d} {dt.minute:02d} "
            f"{dt.second + dt.microsecond / 1_000_000:10.7f}  0{len(sat_ids):3d}"
        )

        sat_map = {_normalize_satellite_id(s["satellite"]): s for s in sats}

        for sat_id in sat_ids:
            sat = sat_map[sat_id]
            obs = sat.get("observations", {}) or {}
            values = [obs.get(obs_name) for obs_name in obs_types]

            chunks = []
            for start in range(0, len(values), 4):
                chunk = values[start : start + 4]
                chunks.append("".join(_format_obs_value_rnx3(v) for v in chunk))

            if not chunks:
                lines.append(sat_id)
                continue

            lines.append(sat_id + chunks[0])
            for piece in chunks[1:]:
                lines.append(" " * 3 + piece)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def write_master_based_ibge_ppp_rinex_v2(
    output_path: str,
    seed: dict,
    marker_name: str = "BASE",
) -> str:
    path = Path(output_path)

    epochs = seed.get("epochs", []) or []
    target = seed.get("target", {}) or {}

    if not epochs:
        raise ValueError("Não há épocas válidas para escrever o RINEX final PPP.")

    preferred_obs = ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]
    available_obs = seed.get("common_observation_types", []) or []

    obs_types = [obs for obs in preferred_obs if obs in available_obs]

    has_code_l1 = any(obs in obs_types for obs in ["C1", "P1"])
    has_code_l2 = any(obs in obs_types for obs in ["C2", "P2"])
    has_phase_l1 = "L1" in obs_types
    has_phase_l2 = "L2" in obs_types

    if not has_code_l1:
        raise ValueError(
            "Não há observável de código em L1 (C1/P1) no produto final PPP."
        )

    if not has_phase_l1:
        raise ValueError("Não há observável de fase L1 no produto final PPP.")

    if not has_code_l2:
        raise ValueError(
            "Não há observável de código em L2 (C2/P2) no produto final PPP."
        )

    if not has_phase_l2:
        raise ValueError("Não há observável de fase L2 no produto final PPP.")

    first_dt = _extract_epoch_datetime(epochs[0]["epoch"])
    last_dt = _extract_epoch_datetime(epochs[-1]["epoch"])
    interval = _estimate_interval_seconds(epochs)

    marker_name = (marker_name or "BASE").strip().upper()[:60] or "BASE"

    def _pad80(content: str, label: str) -> str:
        return f"{content[:60]:<60}{label:<20}"

    def _fmt_obs(value: float | None) -> str:
        if value is None:
            return " " * 16
        return f"{value:14.3f}  "

    def _fmt_time_of_obs(dt: datetime) -> str:
        return (
            f"{dt.year:6d}"
            f"{dt.month:6d}"
            f"{dt.day:6d}"
            f"{dt.hour:6d}"
            f"{dt.minute:6d}"
            f"{dt.second + dt.microsecond / 1_000_000:13.7f}"
            f"{'GPS':>8}"
        )

    def _fmt_epoch_lines(dt: datetime, sat_ids: list[str]) -> list[str]:
        yy = dt.year % 100
        sec = dt.second + dt.microsecond / 1_000_000

        prns = [sid[1:] for sid in sat_ids]
        first = (
            f" {yy:02d} {dt.month:02d} {dt.day:02d} {dt.hour:02d} {dt.minute:02d}"
            f"{sec:11.7f}  0{len(prns):3d}"
        )

        lines = []
        first_chunk = prns[:12]
        lines.append(first + "".join(f"{prn:>3}" for prn in first_chunk))

        remaining = prns[12:]
        while remaining:
            chunk = remaining[:12]
            remaining = remaining[12:]
            lines.append(" " * 32 + "".join(f"{prn:>3}" for prn in chunk))

        return lines

    prn_obs_counts: dict[str, dict[str, int]] = {}
    prn_presence: dict[str, int] = {}

    valid_epoch_records = []

    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        gps_sat_records = []

        for sat in sats:
            sid = _normalize_satellite_id(sat.get("satellite"))
            if _get_satellite_system(sid) != "G":
                continue

            obs = sat.get("observations", {}) or {}

            code1 = (
                _safe_float(obs.get("C1")) is not None
                or _safe_float(obs.get("P1")) is not None
            )
            code2 = (
                _safe_float(obs.get("C2")) is not None
                or _safe_float(obs.get("P2")) is not None
            )
            phase1 = _safe_float(obs.get("L1")) is not None
            phase2 = _safe_float(obs.get("L2")) is not None

            if not (code1 and code2 and phase1 and phase2):
                continue

            gps_sat_records.append((sid, sat))

            prn = sid[1:]
            prn_presence[prn] = prn_presence.get(prn, 0) + 1
            prn_obs_counts.setdefault(prn, {})

            for obs_name in obs_types:
                if _safe_float(obs.get(obs_name)) is not None:
                    prn_obs_counts[prn][obs_name] = (
                        prn_obs_counts[prn].get(obs_name, 0) + 1
                    )

        gps_sat_records.sort(key=lambda item: int(item[0][1:]))

        if gps_sat_records:
            valid_epoch_records.append((epoch, gps_sat_records))

    if not valid_epoch_records:
        raise ValueError(
            "Nenhuma época permaneceu válida após o filtro PPP GPS dual-frequency."
        )

    lines: list[str] = []

    lines.append(
        _pad80(
            f"{'2.11':>9}           OBSERVATION DATA    G",
            "RINEX VERSION / TYPE",
        )
    )
    lines.append(
        _pad80(
            f"{'OrbitBase':<20}{'OrbitBase':<20}{datetime.utcnow().strftime('%d-%b-%y %H:%M').upper():<20}",
            "PGM / RUN BY / DATE",
        )
    )
    lines.append(_pad80(marker_name, "MARKER NAME"))
    lines.append(_pad80("", "MARKER NUMBER"))
    lines.append(
        _pad80(f"{'OrbitBase':<20}{'Biome Projetos':<40}", "OBSERVER / AGENCY")
    )
    lines.append(
        _pad80(
            f"{'VRS':<20}{'ORBITBASE VRS':<20}{'1.0':<20}",
            "REC # / TYPE / VERS",
        )
    )
    lines.append(_pad80(f"{'VRS':<20}{'VIRTUAL':<40}", "ANT # / TYPE"))
    lines.append(_pad80(_build_approx_xyz_text(target), "APPROX POSITION XYZ"))
    lines.append(_pad80(f"{0.0:14.4f}{0.0:14.4f}{0.0:14.4f}", "ANTENNA: DELTA H/E/N"))
    lines.append(_pad80(f"{1:6d}{1:6d}", "WAVELENGTH FACT L1/2"))

    count = len(obs_types)
    first_chunk = obs_types[:9]
    lines.append(
        _pad80(
            f"{count:6d}" + "".join(f"{obs:>6}" for obs in first_chunk),
            "# / TYPES OF OBSERV",
        )
    )

    remaining = obs_types[9:]
    while remaining:
        chunk = remaining[:9]
        remaining = remaining[9:]
        lines.append(
            _pad80(
                " " * 6 + "".join(f"{obs:>6}" for obs in chunk),
                "# / TYPES OF OBSERV",
            )
        )

    lines.append(
        _pad80(
            f"{len(sorted(prn_presence.keys(), key=lambda x: int(x))):6d}",
            "# OF SATELLITES",
        )
    )
    lines.append(_pad80(_fmt_time_of_obs(first_dt), "TIME OF FIRST OBS"))
    lines.append(_pad80(_fmt_time_of_obs(last_dt), "TIME OF LAST OBS"))
    lines.append(_pad80(f"{interval:10.3f}", "INTERVAL"))
    lines.append(_pad80("VIRTUAL REFERENCE STATION - OrbitBase", "COMMENT"))

    for prn in sorted(prn_presence.keys(), key=lambda x: int(x)):
        counts = [
            prn_obs_counts.get(prn, {}).get(obs_name, 0) for obs_name in obs_types
        ]

        first_counts = counts[:9]
        lines.append(
            _pad80(
                f"G{int(prn):>2d}" + "".join(f"{c:6d}" for c in first_counts),
                "PRN / # OF OBS",
            )
        )

        extra = counts[9:]
        while extra:
            chunk = extra[:9]
            extra = extra[9:]
            lines.append(
                _pad80(
                    " " * 3 + "".join(f"{c:6d}" for c in chunk),
                    "PRN / # OF OBS",
                )
            )

    lines.append(_pad80("", "END OF HEADER"))

    for epoch, gps_sat_records in valid_epoch_records:
        epoch_iso = epoch.get("epoch")
        dt = _extract_epoch_datetime(epoch_iso)

        sat_ids = [sid for sid, _ in gps_sat_records]
        lines.extend(_fmt_epoch_lines(dt, sat_ids))

        for _, sat in gps_sat_records:
            obs = sat.get("observations", {}) or {}
            values = [obs.get(obs_name) for obs_name in obs_types]

            for start in range(0, len(values), 5):
                chunk = values[start : start + 5]
                lines.append("".join(_fmt_obs(v) for v in chunk))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def write_virtual_rinex_v3_obs(
    output_path: str,
    seed: dict,
    marker_name: str = "ORBIVRS",
) -> str:
    return _write_rinex_v3_mixed_obs(
        output_path=output_path,
        seed=seed,
        marker_name=marker_name,
    )


def write_virtual_rinex_v2_obs(
    output_path: str,
    seed: dict,
    marker_name: str = "BASE",
) -> str:
    return write_master_based_ibge_ppp_rinex_v2(
        output_path=output_path,
        seed=seed,
        marker_name=marker_name,
    )
