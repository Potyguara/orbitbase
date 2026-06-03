from __future__ import annotations

from math import sqrt
from typing import Any


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _distance_3d(a: dict, b: dict) -> float:
    ax = _safe_float(a.get("x")) or 0.0
    ay = _safe_float(a.get("y")) or 0.0
    az = _safe_float(a.get("z")) or 0.0

    bx = _safe_float(b.get("x")) or 0.0
    by = _safe_float(b.get("y")) or 0.0
    bz = _safe_float(b.get("z")) or 0.0

    return sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)


def _normalize_weights(weights: list[float]) -> list[float]:
    if not weights:
        return []

    total = sum(weights)
    if total <= 0:
        return [1.0 / len(weights)] * len(weights)

    return [w / total for w in weights]


def _inverse_distance_weights(
    distances: list[float],
    power: float = 2.0,
    min_distance_m: float = 0.001,
) -> list[float]:
    prepared = []
    for d in distances:
        if d is None or d <= 0:
            prepared.append(min_distance_m)
        else:
            prepared.append(float(d))

    raw = [1.0 / (d**power) for d in prepared]
    return _normalize_weights(raw)


def compute_station_coordinate_offset(
    input_xyz: dict,
    processed_xyz: dict,
) -> dict:
    """
    Offset = coordenada refinada/processada - coordenada cadastrada/nominal
    """
    in_x = _safe_float(input_xyz.get("x")) or 0.0
    in_y = _safe_float(input_xyz.get("y")) or 0.0
    in_z = _safe_float(input_xyz.get("z")) or 0.0

    pr_x = _safe_float(processed_xyz.get("x")) or 0.0
    pr_y = _safe_float(processed_xyz.get("y")) or 0.0
    pr_z = _safe_float(processed_xyz.get("z")) or 0.0

    dx = pr_x - in_x
    dy = pr_y - in_y
    dz = pr_z - in_z

    return {
        "dx": dx,
        "dy": dy,
        "dz": dz,
        "magnitude": sqrt(dx**2 + dy**2 + dz**2),
    }


def reject_offset_outliers(
    stations: list[dict],
    max_offset_m: float = 1.0,
) -> list[dict]:
    """
    Remove estações cujo offset seja excessivo.
    """
    filtered = []

    for station in stations:
        offset = station.get("offset", {}) or {}
        magnitude = _safe_float(offset.get("magnitude")) or 0.0

        if magnitude <= max_offset_m:
            filtered.append(station)

    return filtered


def interpolate_vrs_offset(
    target_xyz: dict,
    stations: list[dict],
    distance_power: float = 2.0,
    min_stations: int = 2,
    max_offset_m: float = 1.0,
    apply_quality_factor: bool = True,
    apply_geometry_factor: bool = False,
) -> dict:
    """
    stations esperado:
    [
      {
        "name": "...",
        "xyz": {"x": ..., "y": ..., "z": ...},
        "offset": {"dx": ..., "dy": ..., "dz": ..., "magnitude": ...},
        "quality_score": 0.0..1.0,   # opcional
        "geometry_score": 0.0..1.0,  # opcional
      },
      ...
    ]
    """
    usable = []

    for station in stations:
        xyz = station.get("xyz")
        offset = station.get("offset")

        if not xyz or not offset:
            continue

        if _safe_float(offset.get("dx")) is None:
            continue
        if _safe_float(offset.get("dy")) is None:
            continue
        if _safe_float(offset.get("dz")) is None:
            continue

        clone = dict(station)
        clone["distance_to_target_m"] = _distance_3d(target_xyz, xyz)
        usable.append(clone)

    usable = reject_offset_outliers(usable, max_offset_m=max_offset_m)

    if len(usable) < min_stations:
        return {
            "applied": False,
            "reason": "Número insuficiente de estações válidas para interpolação de offset.",
            "station_count_used": len(usable),
            "dx": 0.0,
            "dy": 0.0,
            "dz": 0.0,
            "magnitude": 0.0,
            "stations_used": [],
            "weights": [],
        }

    distances = [s["distance_to_target_m"] for s in usable]
    weights = _inverse_distance_weights(distances, power=distance_power)

    adjusted_weights = []
    for idx, station in enumerate(usable):
        w = weights[idx]

        if apply_quality_factor:
            quality_score = _safe_float(station.get("quality_score"))
            if quality_score is not None and quality_score > 0:
                w *= quality_score

        if apply_geometry_factor:
            geometry_score = _safe_float(station.get("geometry_score"))
            if geometry_score is not None and geometry_score > 0:
                w *= geometry_score

        adjusted_weights.append(w)

    adjusted_weights = _normalize_weights(adjusted_weights)

    dx = 0.0
    dy = 0.0
    dz = 0.0

    stations_used = []
    weights_used = []

    for station, weight in zip(usable, adjusted_weights):
        offset = station["offset"]

        sx = _safe_float(offset.get("dx")) or 0.0
        sy = _safe_float(offset.get("dy")) or 0.0
        sz = _safe_float(offset.get("dz")) or 0.0

        dx += sx * weight
        dy += sy * weight
        dz += sz * weight

        stations_used.append(
            {
                "name": station.get("name"),
                "distance_to_target_m": station.get("distance_to_target_m"),
                "offset_magnitude": (station.get("offset", {}) or {}).get("magnitude"),
                "quality_score": station.get("quality_score"),
                "geometry_score": station.get("geometry_score"),
            }
        )

        weights_used.append(weight)

    return {
        "applied": True,
        "reason": "Offset interpolado com sucesso.",
        "station_count_used": len(usable),
        "dx": dx,
        "dy": dy,
        "dz": dz,
        "magnitude": sqrt(dx**2 + dy**2 + dz**2),
        "stations_used": stations_used,
        "weights": weights_used,
    }


def apply_interpolated_offset_to_target_xyz(
    target_xyz: dict,
    interpolated_offset: dict,
) -> dict:
    tx = _safe_float(target_xyz.get("x")) or 0.0
    ty = _safe_float(target_xyz.get("y")) or 0.0
    tz = _safe_float(target_xyz.get("z")) or 0.0

    dx = _safe_float(interpolated_offset.get("dx")) or 0.0
    dy = _safe_float(interpolated_offset.get("dy")) or 0.0
    dz = _safe_float(interpolated_offset.get("dz")) or 0.0

    return {
        "x": tx + dx,
        "y": ty + dy,
        "z": tz + dz,
    }
