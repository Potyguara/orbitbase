from __future__ import annotations

import math
from datetime import datetime

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


def _common_observation_types_from_many(*numeric_sets: dict) -> list[str]:
    if not numeric_sets:
        return []

    common = None
    for item in numeric_sets:
        obs = set(item.get("observation_types", []) or [])
        common = obs if common is None else (common & obs)

    common = common or set()
    ordered = [obs for obs in SUPPORTED_OBS_PRIORITY if obs in common]
    extra = sorted(common - set(ordered))
    return ordered + extra


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


def _ecef_norm(x: float, y: float, z: float) -> float:
    return math.sqrt(x * x + y * y + z * z)


def _ecef_distance(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    return _ecef_norm(a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _build_target_xyz(target_meta: dict) -> tuple[float, float, float]:
    x = _safe_float(target_meta.get("x"))
    y = _safe_float(target_meta.get("y"))
    z = _safe_float(target_meta.get("z"))

    if x is not None and y is not None and z is not None:
        return x, y, z

    lat = _safe_float(target_meta.get("lat"))
    lon = _safe_float(target_meta.get("lon"))
    height = _safe_float(target_meta.get("height"))

    if lat is None or lon is None or height is None:
        raise ValueError("target_meta sem coordenadas suficientes para o alvo.")

    return _geodetic_to_ecef(lat, lon, height)


def _extract_sat_xyz_from_record(sat_record: dict) -> tuple[float, float, float] | None:
    sat_xyz = (
        sat_record.get("sat_xyz")
        or sat_record.get("satellite_xyz")
        or sat_record.get("xyz")
    )
    if not isinstance(sat_xyz, dict):
        return None

    x = _safe_float(sat_xyz.get("x"))
    y = _safe_float(sat_xyz.get("y"))
    z = _safe_float(sat_xyz.get("z"))

    if x is None or y is None or z is None:
        return None

    return (x, y, z)


def _geometry_delta_rho(
    sat_xyz: tuple[float, float, float],
    rx_from_xyz: tuple[float, float, float],
    rx_to_xyz: tuple[float, float, float],
) -> float:
    rho_from = _ecef_distance(sat_xyz, rx_from_xyz)
    rho_to = _ecef_distance(sat_xyz, rx_to_xyz)
    return rho_to - rho_from


def _interpolate_aux_delta(
    values: list[float | None],
    distances: list[float],
    distance_power: float = 1.2,
) -> float | None:
    usable = []
    for v, d in zip(values, distances):
        vv = _safe_float(v)
        dd = _safe_float(d)
        if vv is None or dd is None:
            continue
        usable.append((vv, max(dd, 1.0)))

    if not usable:
        return None

    if len(usable) == 1:
        return usable[0][0]

    weighted_sum = 0.0
    weight_sum = 0.0

    for value, dist in usable:
        w = 1.0 / (dist**distance_power)
        weighted_sum += w * value
        weight_sum += w

    if weight_sum <= 0:
        return None

    return weighted_sum / weight_sum


def _apply_obs_with_geometry_anchor(
    master_value: float | None,
    interpolated_aux_delta: float | None,
    obs_name: str,
    delta_rho_geom: float | None,
    code_aux_scale: float = 0.35,
    phase_aux_scale: float = 0.20,
    snr_aux_scale: float = 0.00,
) -> float | None:
    m = _safe_float(master_value)
    if m is None:
        return None

    obs_name = (obs_name or "").upper()
    aux_delta = _safe_float(interpolated_aux_delta)
    geom = _safe_float(delta_rho_geom)

    if obs_name in {"C1", "P1", "C2", "P2", "C5"}:
        result = m
        if geom is not None:
            result += geom
        if aux_delta is not None:
            result += code_aux_scale * aux_delta
        return result

    if obs_name in {"L1", "L2"}:
        result = m
        if geom is not None:
            result += geom
        if aux_delta is not None:
            result += phase_aux_scale * aux_delta
        return result

    if obs_name in {"S1", "S2", "S5"}:
        result = m
        if aux_delta is not None:
            result += snr_aux_scale * aux_delta
        return result

    return m


SPEED_OF_LIGHT = 299792458.0
GPS_L1_HZ = 1575.42e6
GPS_L2_HZ = 1227.60e6
GPS_L5_HZ = 1176.45e6

GPS_WAVELENGTHS = {
    "L1": SPEED_OF_LIGHT / GPS_L1_HZ,
    "L2": SPEED_OF_LIGHT / GPS_L2_HZ,
    "L5": SPEED_OF_LIGHT / GPS_L5_HZ,
}


def _is_code_obs(obs_name: str) -> bool:
    return (obs_name or "").upper().startswith(("C", "P"))


def _is_phase_obs(obs_name: str) -> bool:
    return (obs_name or "").upper().startswith("L")


def _is_snr_obs(obs_name: str) -> bool:
    return (obs_name or "").upper().startswith("S")


def _phase_wavelength_m(obs_name: str, sat_id: str | None = None) -> float | None:
    obs = (obs_name or "").upper()
    return GPS_WAVELENGTHS.get(obs)


def _obs_to_meters(
    obs_name: str,
    value: float | None,
    sat_id: str | None = None,
) -> float | None:
    v = _safe_float(value)
    if v is None:
        return None

    if _is_code_obs(obs_name):
        return v

    if _is_phase_obs(obs_name):
        wl = _phase_wavelength_m(obs_name, sat_id=sat_id)
        if wl is None:
            return None
        return v * wl

    if _is_snr_obs(obs_name):
        return v

    return v


def _meters_to_obs_units(
    obs_name: str,
    value_m: float | None,
    sat_id: str | None = None,
) -> float | None:
    v = _safe_float(value_m)
    if v is None:
        return None

    if _is_code_obs(obs_name):
        return v

    if _is_phase_obs(obs_name):
        wl = _phase_wavelength_m(obs_name, sat_id=sat_id)
        if wl is None or abs(wl) < 1e-12:
            return None
        return v / wl

    if _is_snr_obs(obs_name):
        return v

    return v


def build_physical_geometry_vrs_seed_v4(
    master_numeric: dict,
    aux_numeric_list: list[dict],
    target_meta: dict,
    max_epochs: int | None = None,
    min_satellites_per_epoch: int = 4,
    require_dual_frequency: bool = True,
    priority_obs: list[str] | None = None,
    gps_only: bool = True,
    distance_power: float = 1.2,
    code_aux_scale: float = 0.35,
    phase_aux_scale: float = 0.10,
    prefer_plane_fit: bool = False,
) -> dict:
    """
    VRS v4 - modo físico dual-frequency para PPP

    Estratégia:
    - usa a base mestre como referência física;
    - aplica delta geométrico explícito:
          delta_rho_target = rho_target - rho_master
    - remove a componente geométrica das auxiliares:
          residual_aux = (obs_aux_m - obs_master_m) - (rho_aux - rho_master)
    - interpola o resíduo de rede no alvo;
    - recompõe a observação do alvo:
          obs_target_m = obs_master_m + delta_rho_target + scale * residual_interp
    - para fase: converte ciclos -> metros antes da modelagem e metros -> ciclos no final
    - para SNR: mantém a observação da mestre, sem deslocamento geométrico

    Esta versão é apropriada para geração do seed final PPP.
    """
    priority_obs = priority_obs or DEFAULT_PRIORITY_OBS

    if not aux_numeric_list:
        raise ValueError("aux_numeric_list vazio.")

    master_epochs = _index_epochs(master_numeric)
    aux_epochs_list = [_index_epochs(item) for item in aux_numeric_list]

    common_epoch_keys = set(master_epochs.keys())
    for aux_epochs in aux_epochs_list:
        common_epoch_keys &= set(aux_epochs.keys())
    common_epoch_keys = sorted(common_epoch_keys)

    if max_epochs is not None:
        common_epoch_keys = common_epoch_keys[:max_epochs]

    common_obs_types = _common_observation_types_from_many(
        master_numeric, *aux_numeric_list
    )

    usable_obs_types = [
        obs
        for obs in common_obs_types
        if obs in {"C1", "P1", "L1", "C2", "P2", "L2", "C5", "L5", "S1", "S2", "S5"}
    ]

    if not usable_obs_types:
        raise ValueError(
            "Não há observáveis comuns suficientes para gerar a VRS v4 física."
        )

    master_xyz_dict = target_meta.get("master_xyz") or {}
    if not {"x", "y", "z"} <= set(master_xyz_dict.keys()):
        raise ValueError("target_meta['master_xyz'] incompleto.")

    master_xyz = (
        float(master_xyz_dict["x"]),
        float(master_xyz_dict["y"]),
        float(master_xyz_dict["z"]),
    )

    aux_xyz_dicts = target_meta.get("aux_xyz_list") or []
    aux_xyz_list = []
    for item in aux_xyz_dicts:
        if not {"x", "y", "z"} <= set(item.keys()):
            raise ValueError("Um item de target_meta['aux_xyz_list'] está incompleto.")
        aux_xyz_list.append(
            (
                float(item["x"]),
                float(item["y"]),
                float(item["z"]),
            )
        )

    if len(aux_xyz_list) != len(aux_numeric_list):
        raise ValueError("aux_xyz_list incompatível com aux_numeric_list.")

    target_xyz = _build_target_xyz(target_meta)

    aux_target_distances = [
        _ecef_distance(aux_xyz, target_xyz) for aux_xyz in aux_xyz_list
    ]
    master_target_distance = _ecef_distance(master_xyz, target_xyz)

    seed_epochs = []
    epochs_with_satellite_xyz = 0
    epochs_without_satellite_xyz = 0

    for epoch_iso in common_epoch_keys:
        master_epoch = master_epochs[epoch_iso]
        master_sats = _index_satellite_records(master_epoch)

        aux_sats_list = []
        for aux_epochs in aux_epochs_list:
            aux_epoch = aux_epochs[epoch_iso]
            aux_sats_list.append(_index_satellite_records(aux_epoch))

        epoch_satellites = []
        epoch_has_sat_xyz = False

        for sat_id in sorted(master_sats.keys()):
            norm_sat = _normalize_satellite_id(sat_id)

            if gps_only and _get_satellite_system(norm_sat) != "G":
                continue

            master_sat_record = master_sats.get(sat_id) or master_sats.get(norm_sat)
            if not master_sat_record:
                continue

            master_obs = master_sat_record.get("observations", {}) or {}

            aux_obs_records = []
            aux_sat_records = []
            for aux_sats in aux_sats_list:
                aux_sat_record = aux_sats.get(sat_id) or aux_sats.get(norm_sat) or {}
                aux_sat_records.append(aux_sat_record)
                aux_obs_records.append(aux_sat_record.get("observations", {}) or {})

            sat_xyz = _extract_sat_xyz_from_record(master_sat_record)
            if sat_xyz is None:
                for aux_sat_record in aux_sat_records:
                    sat_xyz = _extract_sat_xyz_from_record(aux_sat_record)
                    if sat_xyz is not None:
                        break

            if sat_xyz is None:
                continue

            epoch_has_sat_xyz = True

            rho_master = _ecef_distance(sat_xyz, master_xyz)
            rho_target = _ecef_distance(sat_xyz, target_xyz)
            delta_rho_target = rho_target - rho_master

            corrected_obs = {}

            for obs_name in usable_obs_types:
                if _is_snr_obs(obs_name):
                    corrected_obs[obs_name] = _safe_float(master_obs.get(obs_name))
                    continue

                master_raw = _safe_float(master_obs.get(obs_name))
                if master_raw is None:
                    corrected_obs[obs_name] = None
                    continue

                master_m = _obs_to_meters(obs_name, master_raw, sat_id=norm_sat)
                if master_m is None:
                    corrected_obs[obs_name] = None
                    continue

                residual_aux_values = []

                for aux_obs, aux_xyz in zip(aux_obs_records, aux_xyz_list):
                    aux_raw = _safe_float(aux_obs.get(obs_name))
                    if aux_raw is None:
                        residual_aux_values.append(None)
                        continue

                    aux_m = _obs_to_meters(obs_name, aux_raw, sat_id=norm_sat)
                    if aux_m is None:
                        residual_aux_values.append(None)
                        continue

                    rho_aux = _ecef_distance(sat_xyz, aux_xyz)
                    delta_rho_aux = rho_aux - rho_master

                    residual_aux = (aux_m - master_m) - delta_rho_aux
                    residual_aux_values.append(residual_aux)

                interpolated_residual = _interpolate_aux_delta(
                    values=residual_aux_values,
                    distances=aux_target_distances,
                    distance_power=distance_power,
                )

                if interpolated_residual is None:
                    interpolated_residual = 0.0

                obs_name_u = (obs_name or "").upper()
                scale = code_aux_scale if _is_code_obs(obs_name_u) else phase_aux_scale

                target_m = (
                    master_m + delta_rho_target + float(scale) * interpolated_residual
                )

                corrected_obs[obs_name] = _meters_to_obs_units(
                    obs_name,
                    target_m,
                    sat_id=norm_sat,
                )

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
                    "sat_xyz_available": True,
                    "debug_geometry": {
                        "rho_master_m": float(rho_master),
                        "rho_target_m": float(rho_target),
                        "delta_rho_target_m": float(delta_rho_target),
                    },
                }
            )

        if epoch_has_sat_xyz:
            epochs_with_satellite_xyz += 1
        else:
            epochs_without_satellite_xyz += 1

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
                    "has_satellite_xyz": True,
                }
            )

    anchored = epochs_with_satellite_xyz > 0

    return {
        "processing_mode": "relative_correction_vrs_v4",
        "method": (
            "target_anchored_dual_frequency_geometry_v4"
            if anchored
            else "relative_geometry_v4_without_satellite_xyz"
        ),
        "target": target_meta,
        "master_based": True,
        "master_station_name": target_meta.get("master_station_name"),
        "common_observation_types": usable_obs_types,
        "common_epoch_count": len(common_epoch_keys),
        "generated_epoch_count": len(seed_epochs),
        "systems": ["G"] if seed_epochs else [],
        "epochs": seed_epochs,
        "geometry_coefficients": {
            "distance_power": float(distance_power),
            "master_target_distance_m": float(master_target_distance),
            "aux_target_distance_m": [float(x) for x in aux_target_distances],
            "epochs_with_satellite_xyz": int(epochs_with_satellite_xyz),
            "epochs_without_satellite_xyz": int(epochs_without_satellite_xyz),
            "anchored_solution": bool(anchored),
            "validation_mode": "dual_frequency_physical",
            "code_aux_scale": float(code_aux_scale),
            "phase_aux_scale": float(phase_aux_scale),
        },
        "interpolation_stats": {
            "stations_total": 1 + len(aux_numeric_list),
            "stations_aux": len(aux_numeric_list),
            "gps_only": bool(gps_only),
        },
    }
