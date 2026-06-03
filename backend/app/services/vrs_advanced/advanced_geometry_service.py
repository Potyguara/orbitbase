from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _ensure_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    text = str(value).strip()
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


def _epoch_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _parse_sp3_epoch(line: str) -> datetime | None:
    # Exemplo:
    # *  2026  3 10 12 15  0.00000000
    if not line.startswith("*"):
        return None

    try:
        parts = line[1:].split()
        if len(parts) < 6:
            return None

        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        minute = int(parts[4])
        second_float = float(parts[5])

        second_int = int(second_float)
        microsecond = int(round((second_float - second_int) * 1_000_000))

        if microsecond >= 1_000_000:
            second_int += 1
            microsecond -= 1_000_000

        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second_int,
            microsecond,
            tzinfo=timezone.utc,
        )
    except Exception:
        return None


def _parse_sp3_position_line(line: str) -> tuple[str, dict[str, float | None]] | None:
    """
    Lê linha P de SP3.
    Formato típico:
    PG01  12345.123456  23456.123456  34567.123456  123.456789

    XYZ em km no SP3.
    Aqui convertemos para metros.
    Clock permanece em microssegundos, se disponível.
    """
    if not line.startswith("P"):
        return None

    try:
        sat = line[1:4].strip()
        if not sat:
            return None

        x_km = _safe_float(line[4:18].strip(), None)
        y_km = _safe_float(line[18:32].strip(), None)
        z_km = _safe_float(line[32:46].strip(), None)
        clk_us = _safe_float(line[46:60].strip(), None)

        if x_km is None or y_km is None or z_km is None:
            return None

        return sat, {
            "x": x_km * 1000.0,
            "y": y_km * 1000.0,
            "z": z_km * 1000.0,
            "clock_us": clk_us,
        }
    except Exception:
        return None


def _read_sp3_positions(
    sp3_path: Path,
) -> tuple[list[datetime], dict[str, list[tuple[datetime, dict]]]]:
    """
    Retorna:
    - lista ordenada de épocas do SP3
    - dicionário por satélite:
        sat -> [(epoch, {"x":..., "y":..., "z":..., "clock_us":...}), ...]
    """
    epoch_list: list[datetime] = []
    sat_series: dict[str, list[tuple[datetime, dict]]] = {}

    current_epoch: datetime | None = None

    with sp3_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            if not line:
                continue

            if line.startswith("*"):
                current_epoch = _parse_sp3_epoch(line)
                if current_epoch is not None:
                    epoch_list.append(current_epoch)
                continue

            if current_epoch is None:
                continue

            if line.startswith("P"):
                parsed = _parse_sp3_position_line(line)
                if parsed is None:
                    continue

                sat, pos = parsed
                sat_series.setdefault(sat, []).append((current_epoch, pos))

    epoch_list = sorted(set(epoch_list))

    for sat in sat_series:
        sat_series[sat].sort(key=lambda item: item[0])

    return epoch_list, sat_series


def _interpolate_scalar(
    v0: float | None, v1: float | None, alpha: float
) -> float | None:
    if v0 is None or v1 is None:
        return None
    return v0 + (v1 - v0) * alpha


def _interpolate_satellite_position(
    series: list[tuple[datetime, dict]],
    target_epoch: datetime,
) -> dict | None:
    """
    Interpolação linear entre duas épocas vizinhas do SP3.
    """
    if not series:
        return None

    times = [item[0] for item in series]
    idx = bisect_left(times, target_epoch)

    if idx < len(times) and times[idx] == target_epoch:
        pos = dict(series[idx][1])
        pos["interpolated"] = False
        return pos

    if idx == 0 or idx >= len(times):
        return None

    t0, p0 = series[idx - 1]
    t1, p1 = series[idx]

    dt_total = (t1 - t0).total_seconds()
    if dt_total <= 0:
        return None

    dt_target = (target_epoch - t0).total_seconds()
    alpha = dt_target / dt_total

    return {
        "x": _interpolate_scalar(p0.get("x"), p1.get("x"), alpha),
        "y": _interpolate_scalar(p0.get("y"), p1.get("y"), alpha),
        "z": _interpolate_scalar(p0.get("z"), p1.get("z"), alpha),
        "clock_us": _interpolate_scalar(p0.get("clock_us"), p1.get("clock_us"), alpha),
        "interpolated": True,
    }


def build_precise_geometry_cache(
    time_grid: list,
    precise_products: dict,
    navigation_context: list[dict],
) -> dict:
    """
    Monta cache geométrico real a partir de SP3.

    Saída principal:
    {
      "epochs_requested": ...,
      "epochs_with_geometry": ...,
      "status": "completed",
      "source": "sp3_linear_interpolation",
      "satellite_count": ...,
      "epochs": [
        {
          "epoch": "2026-03-10T12:03:45Z",
          "satellites": {
            "G01": {"x": ..., "y": ..., "z": ..., "clock_us": ..., "interpolated": True},
            ...
          }
        },
        ...
      ]
    }
    """
    normalized_time_grid = []
    for t in time_grid or []:
        dt = _ensure_utc_datetime(t)
        if dt is not None:
            normalized_time_grid.append(dt)

    normalized_time_grid.sort()

    sp3_path_raw = precise_products.get("sp3_path")
    if not sp3_path_raw:
        return {
            "epochs_requested": len(normalized_time_grid),
            "epochs_with_geometry": 0,
            "status": "missing_sp3_path",
            "products": precise_products,
            "navigation_context_count": len(navigation_context or []),
            "epochs": [],
        }

    sp3_path = Path(sp3_path_raw)
    if not sp3_path.exists():
        return {
            "epochs_requested": len(normalized_time_grid),
            "epochs_with_geometry": 0,
            "status": "sp3_file_not_found",
            "products": precise_products,
            "navigation_context_count": len(navigation_context or []),
            "sp3_path": str(sp3_path),
            "epochs": [],
        }

    sp3_epochs, sat_series = _read_sp3_positions(sp3_path)

    if not sp3_epochs or not sat_series:
        return {
            "epochs_requested": len(normalized_time_grid),
            "epochs_with_geometry": 0,
            "status": "sp3_without_positions",
            "products": precise_products,
            "navigation_context_count": len(navigation_context or []),
            "sp3_path": str(sp3_path),
            "epochs": [],
        }

    epochs_output = []
    epochs_with_geometry = 0

    satellite_codes = sorted(sat_series.keys())

    for target_epoch in normalized_time_grid:
        sat_positions = {}

        for sat in satellite_codes:
            interp = _interpolate_satellite_position(
                series=sat_series[sat],
                target_epoch=target_epoch,
            )
            if interp is None:
                continue

            sat_positions[sat] = interp

        if sat_positions:
            epochs_with_geometry += 1

        epochs_output.append(
            {
                "epoch": _epoch_to_iso(target_epoch),
                "satellites": sat_positions,
            }
        )

    return {
        "epochs_requested": len(normalized_time_grid),
        "epochs_with_geometry": epochs_with_geometry,
        "status": "completed" if epochs_with_geometry > 0 else "no_geometry_generated",
        "source": "sp3_linear_interpolation",
        "sp3_path": str(sp3_path),
        "products": precise_products,
        "navigation_context_count": len(navigation_context or []),
        "sp3_epoch_count": len(sp3_epochs),
        "satellite_count": len(satellite_codes),
        "first_sp3_epoch": _epoch_to_iso(sp3_epochs[0]),
        "last_sp3_epoch": _epoch_to_iso(sp3_epochs[-1]),
        "epochs": epochs_output,
    }
