from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from math import sqrt
from statistics import mean, median
from typing import Any

GPS_L1_WAVELENGTH_M = 0.190293672798365
GPS_L2_WAVELENGTH_M = 0.244210213424568


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
            return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    return None


def _normalize_time_grid(time_grid: list[datetime]) -> list[datetime]:
    normalized = []

    for t in time_grid or []:
        dt = _ensure_utc_datetime(t)
        if dt is not None:
            normalized.append(dt)

    normalized.sort()
    return normalized


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _distance_xyz(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    return sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _range_to_satellite(
    sat_geom: dict,
    recv_xyz: tuple[float, float, float],
) -> float | None:
    try:
        sx = float(sat_geom["x"])
        sy = float(sat_geom["y"])
        sz = float(sat_geom["z"])
        rx, ry, rz = recv_xyz
        return sqrt((sx - rx) ** 2 + (sy - ry) ** 2 + (sz - rz) ** 2)
    except Exception:
        return None


def _phase_cycles_to_meters(obs_code: str, value_cycles: float) -> float | None:
    obs = (obs_code or "").strip().upper()

    if obs == "L1":
        return value_cycles * GPS_L1_WAVELENGTH_M

    if obs == "L2":
        return value_cycles * GPS_L2_WAVELENGTH_M

    return None


def _phase_meters_to_cycles(obs_code: str, value_m: float) -> float | None:
    obs = (obs_code or "").strip().upper()

    if obs == "L1":
        return value_m / GPS_L1_WAVELENGTH_M

    if obs == "L2":
        return value_m / GPS_L2_WAVELENGTH_M

    return None


def _is_supported_satellite(sat: str) -> bool:
    sat = (sat or "").strip().upper()
    return len(sat) == 3 and sat.startswith("G") and sat[1:].isdigit()


def _build_base_position_map(
    base_numeric_data: list[dict],
) -> dict[str, tuple[float, float, float]]:
    out = {}

    for base in base_numeric_data or []:
        station = (base.get("station_code") or "").strip().upper()
        if not station:
            continue

        x = base.get("approx_x")
        y = base.get("approx_y")
        z = base.get("approx_z")

        if x is None or y is None or z is None:
            continue

        out[station] = (float(x), float(y), float(z))

    return out


def _inverse_distance_weights(
    base_positions: dict[str, tuple[float, float, float]],
    target_xyz: tuple[float, float, float],
    stations: list[str],
    power: float = 1.0,
) -> dict[str, float]:
    raw = {}

    for st in stations:
        pos = base_positions.get(st)
        if not pos:
            continue

        d = _distance_xyz(pos, target_xyz)
        raw[st] = 1.0 / max(d, 1.0) ** power

    s = sum(raw.values())
    if s <= 0:
        return {}

    return {k: v / s for k, v in raw.items()}


def _build_base_satellite_series(
    base_numeric_data: list[dict],
) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}

    for base in base_numeric_data or []:
        station = (base.get("station_code") or "").strip().upper()
        if not station:
            continue

        station_map: dict[str, dict] = {}

        for epoch in base.get("epochs", []) or []:
            epoch_dt = _ensure_utc_datetime(
                epoch.get("epoch") or epoch.get("epoch_utc")
            )
            if epoch_dt is None:
                continue

            satellites = (
                epoch.get("satellites")
                or epoch.get("observations")
                or epoch.get("sats")
                or []
            )

            if not isinstance(satellites, list):
                continue

            for sat_item in satellites:
                if not isinstance(sat_item, dict):
                    continue

                sat = (
                    sat_item.get("satellite")
                    or sat_item.get("sat")
                    or sat_item.get("prn")
                    or ""
                )
                sat = str(sat).strip().upper()

                if not _is_supported_satellite(sat):
                    continue

                obs_map = sat_item.get("observations") or sat_item.get("obs") or {}
                if not isinstance(obs_map, dict) or not obs_map:
                    continue

                sat_series = station_map.setdefault(
                    sat,
                    {
                        "times": [],
                        "samples": [],
                    },
                )
                sat_series["times"].append(epoch_dt)
                sat_series["samples"].append(
                    {
                        "epoch": epoch_dt,
                        "observations": dict(obs_map),
                    }
                )

        for sat in station_map:
            zipped = sorted(
                zip(station_map[sat]["times"], station_map[sat]["samples"]),
                key=lambda x: x[0],
            )
            station_map[sat]["times"] = [x[0] for x in zipped]
            station_map[sat]["samples"] = [x[1] for x in zipped]

        out[station] = station_map

    return out


def _get_geometry_epoch_map(geometry_cache: dict) -> dict[datetime, dict]:
    out = {}

    for item in geometry_cache.get("epochs", []) or []:
        epoch_dt = _ensure_utc_datetime(item.get("epoch"))
        if epoch_dt is None:
            continue

        satellites = item.get("satellites") or {}
        if isinstance(satellites, dict):
            sat_map = {}
            for sat, payload in satellites.items():
                sat = str(sat).strip().upper()
                if _is_supported_satellite(sat) and isinstance(payload, dict):
                    sat_map[sat] = payload
            out[epoch_dt] = sat_map

    return out


def _estimate_native_interval_seconds(base_numeric_data: list[dict]) -> int:
    deltas = []

    for base in base_numeric_data or []:
        epochs = base.get("epochs") or []
        if len(epochs) < 2:
            continue

        dts = []
        for ep in epochs[:10]:
            dt = _ensure_utc_datetime(ep.get("epoch") or ep.get("epoch_utc"))
            if dt is not None:
                dts.append(dt)

        if len(dts) < 2:
            continue

        for i in range(1, len(dts)):
            delta = (dts[i] - dts[i - 1]).total_seconds()
            if delta > 0:
                deltas.append(delta)

    if not deltas:
        return 15

    try:
        return max(1, int(round(median(deltas))))
    except Exception:
        return 15


def _find_bracketing_samples(
    series_times: list[datetime],
    series_samples: list[dict],
    target_epoch: datetime,
    tolerance_seconds: float,
) -> tuple[dict | None, dict | None]:
    if not series_times or not series_samples:
        return None, None

    idx = bisect_left(series_times, target_epoch)

    prev_sample = None
    next_sample = None

    if idx > 0:
        prev_candidate = series_samples[idx - 1]
        prev_dt = _ensure_utc_datetime(prev_candidate.get("epoch"))
        if prev_dt is not None:
            if abs((target_epoch - prev_dt).total_seconds()) <= tolerance_seconds:
                prev_sample = prev_candidate

    if idx < len(series_times):
        next_candidate = series_samples[idx]
        next_dt = _ensure_utc_datetime(next_candidate.get("epoch"))
        if next_dt is not None:
            if abs((next_dt - target_epoch).total_seconds()) <= tolerance_seconds:
                next_sample = next_candidate

    if prev_sample is None and next_sample is None:
        return None, None

    return prev_sample, next_sample


def _find_nearest_sample(
    series_times: list[datetime],
    series_samples: list[dict],
    target_epoch: datetime,
    tolerance_seconds: float,
) -> dict | None:
    if not series_times or not series_samples:
        return None

    idx = bisect_left(series_times, target_epoch)
    candidates = []

    if idx > 0:
        candidates.append(series_samples[idx - 1])

    if idx < len(series_samples):
        candidates.append(series_samples[idx])

    best = None
    best_abs = None

    for sample in candidates:
        dt = _ensure_utc_datetime(sample.get("epoch"))
        if dt is None:
            continue

        diff = abs((target_epoch - dt).total_seconds())
        if diff > tolerance_seconds:
            continue

        if best is None or diff < best_abs:
            best = sample
            best_abs = diff

    return best


def _interpolate_scalar_in_time(
    prev_dt: datetime | None,
    prev_val: float | None,
    next_dt: datetime | None,
    next_val: float | None,
    target_dt: datetime,
) -> float | None:
    if prev_val is None and next_val is None:
        return None

    if prev_val is not None and next_val is None:
        return prev_val

    if prev_val is None and next_val is not None:
        return next_val

    if prev_dt is None or next_dt is None:
        return prev_val if prev_val is not None else next_val

    if next_dt <= prev_dt:
        return prev_val

    total = (next_dt - prev_dt).total_seconds()
    delta = (target_dt - prev_dt).total_seconds()
    alpha = delta / total

    return float(prev_val) + (float(next_val) - float(prev_val)) * alpha


def _interpolate_observation_map_in_time(
    prev_sample: dict | None,
    next_sample: dict | None,
    target_epoch: datetime,
    obs_codes: list[str],
) -> dict[str, float]:
    out: dict[str, float] = {}

    prev_dt = _ensure_utc_datetime(prev_sample.get("epoch")) if prev_sample else None
    next_dt = _ensure_utc_datetime(next_sample.get("epoch")) if next_sample else None

    prev_obs = (prev_sample or {}).get("observations") or {}
    next_obs = (next_sample or {}).get("observations") or {}

    for obs_code in obs_codes:
        prev_val = prev_obs.get(obs_code)
        next_val = next_obs.get(obs_code)

        try:
            prev_val = None if prev_val is None else float(prev_val)
        except Exception:
            prev_val = None

        try:
            next_val = None if next_val is None else float(next_val)
        except Exception:
            next_val = None

        interp = _interpolate_scalar_in_time(
            prev_dt=prev_dt,
            prev_val=prev_val,
            next_dt=next_dt,
            next_val=next_val,
            target_dt=target_epoch,
        )

        if interp is not None:
            out[obs_code] = interp

    return out


def _extract_code_observations_conservative(
    prev_sample: dict | None,
    next_sample: dict | None,
    target_epoch: datetime,
) -> dict[str, float]:
    code_obs = ["C1", "P1", "C2", "P2"]
    return _interpolate_observation_map_in_time(
        prev_sample=prev_sample,
        next_sample=next_sample,
        target_epoch=target_epoch,
        obs_codes=code_obs,
    )


def _extract_phase_observations_conservative(
    nearest_sample: dict | None,
) -> dict[str, float]:
    if nearest_sample is None:
        return {}

    obs = nearest_sample.get("observations") or {}
    if not isinstance(obs, dict):
        return {}

    out = {}

    for obs_code in ("L1", "L2"):
        value = obs.get(obs_code)
        try:
            if value is not None:
                out[obs_code] = float(value)
        except Exception:
            pass

    return out


def _extract_snr_observations_conservative(
    nearest_sample: dict | None,
) -> dict[str, float]:
    if nearest_sample is None:
        return {}

    obs = nearest_sample.get("observations") or {}
    if not isinstance(obs, dict):
        return {}

    out = {}

    for obs_code in ("S1", "S2"):
        value = obs.get(obs_code)
        try:
            if value is not None:
                out[obs_code] = float(value)
        except Exception:
            pass

    return out


def _select_master_base(
    base_items: list[dict],
    target_xyz: tuple[float, float, float],
    base_positions: dict[str, tuple[float, float, float]],
) -> dict | None:
    candidates = []

    for item in base_items:
        st = item.get("station_code")
        pos = base_positions.get(st)
        if pos is None:
            continue

        d = _distance_xyz(pos, target_xyz)
        candidates.append((d, item))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    med = median(values)
    return median([abs(v - med) for v in values])


def _robust_delta_from_master(
    base_items: list[dict],
    obs_code: str,
    master_station: str,
    target_xyz: tuple[float, float, float],
    base_positions: dict[str, tuple[float, float, float]],
    max_abs_delta: float,
    dispersion_limit: float,
) -> tuple[float, dict]:
    master_res = None
    weighted_deltas = []

    for item in base_items:
        st = item.get("station_code")
        res = item.get("residuals", {}).get(obs_code)
        if st is None or res is None:
            continue

        if st == master_station:
            master_res = float(res)

    if master_res is None:
        return 0.0, {
            "usable_count": 0,
            "dispersion_mad": None,
            "fallback_master_only": True,
        }

    usable_stations = []

    for item in base_items:
        st = item.get("station_code")
        if st is None or st == master_station:
            continue

        res = item.get("residuals", {}).get(obs_code)
        if res is None:
            continue

        delta = float(res) - master_res
        usable_stations.append(st)
        weighted_deltas.append((st, delta))

    if not weighted_deltas:
        return 0.0, {
            "usable_count": 0,
            "dispersion_mad": 0.0,
            "fallback_master_only": True,
        }

    dispersion = _mad([d for _, d in weighted_deltas])

    if dispersion > dispersion_limit:
        return 0.0, {
            "usable_count": len(weighted_deltas),
            "dispersion_mad": dispersion,
            "fallback_master_only": True,
        }

    weights = _inverse_distance_weights(
        base_positions=base_positions,
        target_xyz=target_xyz,
        stations=usable_stations,
        power=1.0,
    )
    if not weights:
        return 0.0, {
            "usable_count": len(weighted_deltas),
            "dispersion_mad": dispersion,
            "fallback_master_only": True,
        }

    num = 0.0
    den = 0.0

    for st, delta in weighted_deltas:
        w = weights.get(st)
        if w is None:
            continue
        num += w * delta
        den += w

    if den <= 0:
        return 0.0, {
            "usable_count": len(weighted_deltas),
            "dispersion_mad": dispersion,
            "fallback_master_only": True,
        }

    correction = num / den

    if correction > max_abs_delta:
        correction = max_abs_delta
    elif correction < -max_abs_delta:
        correction = -max_abs_delta

    return correction, {
        "usable_count": len(weighted_deltas),
        "dispersion_mad": dispersion,
        "fallback_master_only": False,
    }


def _build_conservative_observation_set(
    sat: str,
    base_items: list[dict],
    target_xyz: tuple[float, float, float],
    base_positions: dict[str, tuple[float, float, float]],
    interpolate_code: bool,
    interpolate_phase: bool,
    interpolate_snr: bool,
) -> tuple[dict[str, float], dict]:
    out_obs: dict[str, float] = {}
    debug: dict[str, Any] = {
        "satellite": sat,
        "master_station": None,
        "code_debug": {},
        "phase_debug": {},
        "snr_debug": {},
    }

    if not base_items:
        return out_obs, debug

    master_item = _select_master_base(
        base_items=base_items,
        target_xyz=target_xyz,
        base_positions=base_positions,
    )
    if master_item is None:
        return out_obs, debug

    master_station = master_item["station_code"]
    rho_target = float(master_item["rho_target"])
    debug["master_station"] = master_station

    code_obs = ["C1", "P1", "C2", "P2"]
    phase_obs = ["L1", "L2"]
    snr_obs = ["S1", "S2"]

    if interpolate_code:
        for obs_code in code_obs:
            master_res = master_item["residuals"].get(obs_code)
            if master_res is None:
                continue

            correction, corr_debug = _robust_delta_from_master(
                base_items=base_items,
                obs_code=obs_code,
                master_station=master_station,
                target_xyz=target_xyz,
                base_positions=base_positions,
                max_abs_delta=2.0,
                dispersion_limit=8.0,
            )

            out_obs[obs_code] = rho_target + float(master_res) + correction
            debug["code_debug"][obs_code] = corr_debug

    if interpolate_phase:
        for obs_code in phase_obs:
            master_res = master_item["residuals"].get(obs_code)
            if master_res is None:
                continue

            correction, corr_debug = _robust_delta_from_master(
                base_items=base_items,
                obs_code=obs_code,
                master_station=master_station,
                target_xyz=target_xyz,
                base_positions=base_positions,
                max_abs_delta=0.08,
                dispersion_limit=0.25,
            )

            phase_target_m = rho_target + float(master_res) + correction
            phase_cycles = _phase_meters_to_cycles(obs_code, phase_target_m)

            if phase_cycles is not None:
                out_obs[obs_code] = phase_cycles
                debug["phase_debug"][obs_code] = corr_debug

    if interpolate_snr:
        for obs_code in snr_obs:
            vals = []
            stations = []

            for item in base_items:
                st = item["station_code"]
                val = item["residuals"].get(obs_code)
                if val is None:
                    continue
                vals.append((st, float(val)))
                stations.append(st)

            if not vals:
                continue

            weights = _inverse_distance_weights(
                base_positions=base_positions,
                target_xyz=target_xyz,
                stations=stations,
                power=1.0,
            )
            if not weights:
                continue

            num = 0.0
            den = 0.0
            for st, val in vals:
                w = weights.get(st)
                if w is None:
                    continue
                num += w * val
                den += w

            if den > 0:
                out_obs[obs_code] = num / den
                debug["snr_debug"][obs_code] = {
                    "usable_count": len(vals),
                    "weighted": True,
                }

    return out_obs, debug


def _filter_epoch_observations_conservative(
    virtual_epochs: list[dict],
    min_satellites_per_epoch: int,
    interpolate_code: bool,
    interpolate_phase: bool,
) -> list[dict]:
    filtered = []

    for epoch in virtual_epochs:
        sats = epoch.get("observations") or []
        new_sats = []

        for sat_item in sats:
            sat = str(sat_item.get("satellite") or "").strip().upper()
            if not _is_supported_satellite(sat):
                continue

            obs = sat_item.get("observations") or {}
            if not isinstance(obs, dict):
                continue

            has_code_l1 = any(k in obs for k in ("C1", "P1"))
            has_code_l2 = any(k in obs for k in ("C2", "P2"))
            has_any_code = has_code_l1 or has_code_l2
            has_phase = any(k in obs for k in ("L1", "L2"))

            if interpolate_phase:
                if not has_any_code:
                    continue
                if not has_phase:
                    continue
            else:
                if not has_any_code:
                    continue

            new_sats.append(
                {
                    "satellite": sat,
                    "observations": obs,
                }
            )

        new_sats.sort(key=lambda x: x["satellite"])

        if len(new_sats) >= min_satellites_per_epoch:
            filtered.append(
                {
                    "epoch": epoch["epoch"],
                    "satellite_count": len(new_sats),
                    "observations": new_sats,
                    "debug": epoch.get("debug"),
                }
            )

    return filtered


def _decimate_virtual_epochs(
    virtual_epochs: list[dict],
    output_step_seconds: int,
) -> list[dict]:
    if output_step_seconds <= 1:
        return virtual_epochs

    if not virtual_epochs:
        return virtual_epochs

    first_dt = _ensure_utc_datetime(virtual_epochs[0].get("epoch"))
    if first_dt is None:
        return virtual_epochs

    out = []

    for epoch in virtual_epochs:
        epoch_dt = _ensure_utc_datetime(epoch.get("epoch"))
        if epoch_dt is None:
            continue

        delta = (epoch_dt - first_dt).total_seconds()
        if delta < 0:
            continue

        if int(round(delta)) % output_step_seconds == 0:
            out.append(epoch)

    return out


def interpolate_advanced_observations(
    time_grid: list[datetime],
    base_numeric_data: list[dict],
    geometry_cache: dict,
    config: dict,
) -> dict:
    normalized_time_grid = _normalize_time_grid(time_grid)

    target_xyz = (
        float(config.get("target_x", 0.0)),
        float(config.get("target_y", 0.0)),
        float(config.get("target_z", 0.0)),
    )

    min_common_bases = int(config.get("min_common_bases", 3) or 3)
    min_satellites_per_epoch = int(config.get("min_satellites_per_epoch", 4) or 4)

    interpolate_code = bool(config.get("interpolate_code_observables", True))
    interpolate_phase = bool(config.get("interpolate_phase_observables", True))
    interpolate_snr = bool(config.get("interpolate_snr", False))

    tolerance_seconds = float(config.get("max_interpolation_gap_seconds", 30) or 30)

    internal_step_seconds = int(config.get("target_step_seconds", 1) or 1)
    native_interval_seconds = _estimate_native_interval_seconds(base_numeric_data)

    configured_output_step = config.get("output_step_seconds")
    if configured_output_step is None:
        output_step_seconds = native_interval_seconds
    else:
        output_step_seconds = int(configured_output_step or native_interval_seconds)

    if output_step_seconds < 1:
        output_step_seconds = native_interval_seconds

    phase_tolerance_seconds = min(
        max(1.0, native_interval_seconds * 0.25),
        tolerance_seconds,
    )

    code_tolerance_seconds = tolerance_seconds

    base_positions = _build_base_position_map(base_numeric_data)
    base_series = _build_base_satellite_series(base_numeric_data)
    geometry_epoch_map = _get_geometry_epoch_map(geometry_cache)

    print(
        f"[ADV-INT] normalized_time_grid={len(normalized_time_grid)} "
        f"usable_bases={len(base_positions)} "
        f"target_xyz={target_xyz} "
        f"internal_step={internal_step_seconds} "
        f"native_interval={native_interval_seconds} "
        f"output_step={output_step_seconds} "
        f"phase_tolerance={phase_tolerance_seconds}",
        flush=True,
    )

    virtual_epochs_raw = []
    debug_epochs = []

    for target_epoch in normalized_time_grid:
        epoch_geometry = geometry_epoch_map.get(target_epoch, {})
        sat_base_residuals: dict[str, list[dict]] = {}
        bases_at_epoch_debug = []

        for station, sat_series in base_series.items():
            base_xyz = base_positions.get(station)
            if base_xyz is None:
                continue

            base_sat_count = 0

            for sat, sat_payload in sat_series.items():
                if not _is_supported_satellite(sat):
                    continue

                sat_geom = epoch_geometry.get(sat)
                if sat_geom is None:
                    continue

                rho_base = _range_to_satellite(sat_geom, base_xyz)
                rho_target = _range_to_satellite(sat_geom, target_xyz)
                if rho_base is None or rho_target is None:
                    continue

                prev_sample, next_sample = _find_bracketing_samples(
                    series_times=sat_payload["times"],
                    series_samples=sat_payload["samples"],
                    target_epoch=target_epoch,
                    tolerance_seconds=code_tolerance_seconds,
                )

                nearest_phase_sample = _find_nearest_sample(
                    series_times=sat_payload["times"],
                    series_samples=sat_payload["samples"],
                    target_epoch=target_epoch,
                    tolerance_seconds=phase_tolerance_seconds,
                )

                residuals = {}

                if interpolate_code:
                    code_obs = _extract_code_observations_conservative(
                        prev_sample=prev_sample,
                        next_sample=next_sample,
                        target_epoch=target_epoch,
                    )

                    for obs_code in ("C1", "P1", "C2", "P2"):
                        value = code_obs.get(obs_code)
                        if value is None:
                            continue
                        residuals[obs_code] = float(value) - rho_base

                if interpolate_phase:
                    phase_obs = _extract_phase_observations_conservative(
                        nearest_sample=nearest_phase_sample
                    )

                    for obs_code in ("L1", "L2"):
                        value_cycles = phase_obs.get(obs_code)
                        if value_cycles is None:
                            continue

                        value_m = _phase_cycles_to_meters(obs_code, float(value_cycles))
                        if value_m is None:
                            continue

                        residuals[obs_code] = value_m - rho_base

                if interpolate_snr:
                    snr_obs = _extract_snr_observations_conservative(
                        nearest_sample=nearest_phase_sample
                    )

                    for obs_code in ("S1", "S2"):
                        value = snr_obs.get(obs_code)
                        if value is None:
                            continue
                        residuals[obs_code] = float(value)

                if not residuals:
                    continue

                sat_base_residuals.setdefault(sat, []).append(
                    {
                        "station_code": station,
                        "rho_target": rho_target,
                        "rho_base": rho_base,
                        "residuals": residuals,
                    }
                )
                base_sat_count += 1

            bases_at_epoch_debug.append(
                {
                    "station_code": station,
                    "satellite_count": base_sat_count,
                }
            )

        virtual_satellites = []
        epoch_sat_debug = []

        for sat, base_items in sat_base_residuals.items():
            stations = sorted({item["station_code"] for item in base_items})
            if len(stations) < min_common_bases:
                continue

            out_obs, sat_debug = _build_conservative_observation_set(
                sat=sat,
                base_items=base_items,
                target_xyz=target_xyz,
                base_positions=base_positions,
                interpolate_code=interpolate_code,
                interpolate_phase=interpolate_phase,
                interpolate_snr=interpolate_snr,
            )

            if out_obs:
                virtual_satellites.append(
                    {
                        "satellite": sat,
                        "observations": out_obs,
                    }
                )
                epoch_sat_debug.append(sat_debug)

        virtual_satellites.sort(key=lambda x: x["satellite"])

        if not virtual_satellites:
            continue

        epoch_key = target_epoch.isoformat()

        virtual_epochs_raw.append(
            {
                "epoch": epoch_key,
                "satellite_count": len(virtual_satellites),
                "observations": virtual_satellites,
                "debug": {
                    "satellites": epoch_sat_debug,
                },
            }
        )

        debug_epochs.append(
            {
                "epoch": epoch_key,
                "base_count": len(bases_at_epoch_debug),
                "mean_satellite_count": (
                    mean(item["satellite_count"] for item in bases_at_epoch_debug)
                    if bases_at_epoch_debug
                    else 0.0
                ),
                "bases": bases_at_epoch_debug,
                "geometry_satellite_count": len(epoch_geometry),
            }
        )

    print(
        f"[ADV-INT] virtual_epochs_raw={len(virtual_epochs_raw)}",
        flush=True,
    )

    virtual_epochs = _filter_epoch_observations_conservative(
        virtual_epochs=virtual_epochs_raw,
        min_satellites_per_epoch=min_satellites_per_epoch,
        interpolate_code=interpolate_code,
        interpolate_phase=interpolate_phase,
    )

    print(
        f"[ADV-INT] virtual_epochs_after_filter={len(virtual_epochs)}",
        flush=True,
    )

    virtual_epochs = _decimate_virtual_epochs(
        virtual_epochs=virtual_epochs,
        output_step_seconds=output_step_seconds,
    )

    final_epoch_set = {ep["epoch"] for ep in virtual_epochs}
    debug_epochs = [ep for ep in debug_epochs if ep.get("epoch") in final_epoch_set]

    print(
        f"[ADV-INT] virtual_epochs={len(virtual_epochs)} "
        f"epochs_debug={len(debug_epochs)} "
        f"output_step_seconds={output_step_seconds}",
        flush=True,
    )

    return {
        "status": "completed",
        "message": "Interpolação avançada conservadora executada com sucesso.",
        "epochs_generated": len(virtual_epochs),
        "epochs_skipped": max(0, len(normalized_time_grid) - len(virtual_epochs)),
        "approx_x": target_xyz[0],
        "approx_y": target_xyz[1],
        "approx_z": target_xyz[2],
        "interval_seconds": float(output_step_seconds),
        "internal_interval_seconds": float(internal_step_seconds),
        "native_interval_seconds": float(native_interval_seconds),
        "epochs": [
            {
                "epoch": ep["epoch"],
                "satellite_count": ep["satellite_count"],
                "observations": ep["observations"],
            }
            for ep in virtual_epochs
        ],
        "epochs_debug": debug_epochs,
        "summary": {
            "time_grid_count": len(normalized_time_grid),
            "input_bases": len(base_numeric_data or []),
            "usable_bases": len(base_positions),
            "geometry_cache_present": bool(geometry_cache),
            "geometry_epochs": len(geometry_cache.get("epochs", []) or []),
            "tolerance_seconds": tolerance_seconds,
            "phase_tolerance_seconds": phase_tolerance_seconds,
            "internal_step_seconds": internal_step_seconds,
            "native_interval_seconds": native_interval_seconds,
            "output_step_seconds": output_step_seconds,
            "first_target_epoch": (
                virtual_epochs[0]["epoch"] if virtual_epochs else None
            ),
            "last_target_epoch": (
                virtual_epochs[-1]["epoch"] if virtual_epochs else None
            ),
        },
    }
