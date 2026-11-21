from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.rinex_nav_satpos_service import (
    build_satellite_position_cache_from_nav,
    detect_navigation_file_near_observation,
)


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
    if not line.startswith("*"):
        return None

    try:
        parts = line[1:].split()
        if len(parts) < 6:
            print(
                f"[ADV-SP3-EPOCH] linha inválida len(parts)<6 :: {line!r}", flush=True
            )
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

        dt = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second_int,
            microsecond,
            tzinfo=timezone.utc,
        )
        return dt

    except Exception as exc:
        print(
            f"[ADV-SP3-EPOCH] erro={type(exc).__name__} line={line!r}",
            flush=True,
        )
        return None


def _parse_sp3_position_line(line: str) -> tuple[str, dict[str, float | None]] | None:
    if not line.startswith("P"):
        return None

    try:
        sat = line[1:4].strip()
        if not sat:
            print(f"[ADV-SP3-POS] sat vazio :: {line!r}", flush=True)
            return None

        x_km = _safe_float(line[4:18].strip(), None)
        y_km = _safe_float(line[18:32].strip(), None)
        z_km = _safe_float(line[32:46].strip(), None)
        clk_us = _safe_float(line[46:60].strip(), None)

        if x_km is None or y_km is None or z_km is None:
            print(
                "[ADV-SP3-POS] coordenada inválida "
                f"sat={sat} x={x_km} y={y_km} z={z_km} line={line!r}",
                flush=True,
            )
            return None

        return sat, {
            "x": x_km * 1000.0,
            "y": y_km * 1000.0,
            "z": z_km * 1000.0,
            "clock_us": clk_us,
        }

    except Exception as exc:
        print(
            f"[ADV-SP3-POS] erro={type(exc).__name__} line={line!r}",
            flush=True,
        )
        return None


def _read_sp3_positions(
    sp3_path: Path,
) -> tuple[list[datetime], dict[str, list[tuple[datetime, dict]]]]:
    epoch_list: list[datetime] = []
    sat_series: dict[str, list[tuple[datetime, dict]]] = {}

    current_epoch: datetime | None = None
    total_lines = 0
    epoch_marker_count = 0
    position_marker_count = 0
    parsed_epoch_count = 0
    parsed_position_count = 0
    sample_epoch_line = None
    sample_position_line = None
    sample_lines: list[str] = []

    try:
        with sp3_path.open("r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                total_lines += 1
                line = raw_line.rstrip("\n")

                if total_lines <= 8:
                    sample_lines.append(line)

                if not line:
                    continue

                if line.startswith("*"):
                    epoch_marker_count += 1
                    if sample_epoch_line is None:
                        sample_epoch_line = line

                    current_epoch = _parse_sp3_epoch(line)
                    if current_epoch is not None:
                        epoch_list.append(current_epoch)
                        parsed_epoch_count += 1
                    continue

                if current_epoch is None:
                    continue

                if line.startswith("P"):
                    position_marker_count += 1
                    if sample_position_line is None:
                        sample_position_line = line

                    parsed = _parse_sp3_position_line(line)
                    if parsed is None:
                        continue

                    sat, pos = parsed
                    sat_series.setdefault(sat, []).append((current_epoch, pos))
                    parsed_position_count += 1

    except Exception as exc:
        print(
            f"[ADV-SP3-READ] erro abrindo/lendo path={sp3_path} "
            f"erro={type(exc).__name__}: {exc}",
            flush=True,
        )
        return [], {}

    epoch_list = sorted(set(epoch_list))

    for sat in sat_series:
        sat_series[sat].sort(key=lambda item: item[0])

    print(
        "[ADV-SP3-READ] "
        f"path={sp3_path} "
        f"exists={sp3_path.exists()} "
        f"size={sp3_path.stat().st_size if sp3_path.exists() else 'missing'} "
        f"total_lines={total_lines} "
        f"epoch_marker_count={epoch_marker_count} "
        f"position_marker_count={position_marker_count} "
        f"parsed_epoch_count={parsed_epoch_count} "
        f"parsed_position_count={parsed_position_count} "
        f"epochs={len(epoch_list)} "
        f"satellites={len(sat_series)}",
        flush=True,
    )

    print(
        f"[ADV-SP3-READ] sample_epoch_line={sample_epoch_line!r}",
        flush=True,
    )
    print(
        f"[ADV-SP3-READ] sample_position_line={sample_position_line!r}",
        flush=True,
    )
    print(
        f"[ADV-SP3-READ] first_lines={sample_lines!r}",
        flush=True,
    )

    if epoch_list:
        print(
            f"[ADV-SP3-READ] first_epoch={_epoch_to_iso(epoch_list[0])} "
            f"last_epoch={_epoch_to_iso(epoch_list[-1])}",
            flush=True,
        )

    if sat_series:
        satellites_sample = list(sorted(sat_series.keys()))[:12]
        print(
            f"[ADV-SP3-READ] satellites_sample={satellites_sample}",
            flush=True,
        )

    return epoch_list, sat_series


def _interpolate_scalar(
    v0: float | None,
    v1: float | None,
    alpha: float,
) -> float | None:
    if v0 is None or v1 is None:
        return None
    return v0 + (v1 - v0) * alpha


def _interpolate_satellite_position(
    series: list[tuple[datetime, dict]],
    target_epoch: datetime,
) -> dict | None:
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


def _normalize_time_grid(time_grid: list) -> list[datetime]:
    normalized_time_grid = []

    for t in time_grid or []:
        dt = _ensure_utc_datetime(t)
        if dt is not None:
            normalized_time_grid.append(dt)

    normalized_time_grid.sort()

    print(
        f"[ADV-GEO] normalized_time_grid_count={len(normalized_time_grid)}",
        flush=True,
    )
    if normalized_time_grid:
        print(
            f"[ADV-GEO] normalized_time_grid_first={_epoch_to_iso(normalized_time_grid[0])} "
            f"last={_epoch_to_iso(normalized_time_grid[-1])}",
            flush=True,
        )

    return normalized_time_grid


def _build_sp3_geometry_cache(
    normalized_time_grid: list[datetime],
    precise_products: dict,
    navigation_context: list[dict],
) -> dict:
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

    print(
        f"[ADV-GEO-SP3] sp3_path={sp3_path} "
        f"size={sp3_path.stat().st_size if sp3_path.exists() else 'missing'}",
        flush=True,
    )

    if normalized_time_grid:
        print(
            f"[ADV-GEO-SP3] target_grid={len(normalized_time_grid)} "
            f"first_target={_epoch_to_iso(normalized_time_grid[0])} "
            f"last_target={_epoch_to_iso(normalized_time_grid[-1])}",
            flush=True,
        )

    sp3_epochs, sat_series = _read_sp3_positions(sp3_path)

    if sp3_epochs:
        print(
            f"[ADV-GEO-SP3] sp3_epochs={len(sp3_epochs)} "
            f"first_sp3={_epoch_to_iso(sp3_epochs[0])} "
            f"last_sp3={_epoch_to_iso(sp3_epochs[-1])}",
            flush=True,
        )
    else:
        print("[ADV-GEO-SP3] sp3_epochs=0", flush=True)

    if not sp3_epochs or not sat_series:
        print(
            f"[ADV-GEO-SP3] sp3 sem posições úteis "
            f"epochs={len(sp3_epochs)} satellites={len(sat_series)}",
            flush=True,
        )
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

    for idx_epoch, target_epoch in enumerate(normalized_time_grid):
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

        if idx_epoch < 3:
            print(
                f"[ADV-GEO-SP3] epoch_sample target={_epoch_to_iso(target_epoch)} "
                f"sat_count={len(sat_positions)} sats={list(sorted(sat_positions.keys()))[:12]}",
                flush=True,
            )

        epochs_output.append(
            {
                "epoch": _epoch_to_iso(target_epoch),
                "satellites": sat_positions,
            }
        )

    print(
        f"[ADV-GEO-SP3] epochs_with_geometry={epochs_with_geometry} "
        f"epochs_requested={len(normalized_time_grid)} "
        f"satellite_count={len(satellite_codes)}",
        flush=True,
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


def _looks_like_navigation_filename(path: Path) -> bool:
    name = path.name.lower()

    if name.startswith("brdc"):
        return True

    nav_suffixes = (
        ".nav",
        ".n",
        ".g",
        ".p",
        ".rnx",
        ".26n",
        ".25n",
        ".24n",
        ".23n",
        ".26g",
        ".25g",
        ".24g",
        ".23g",
        ".26p",
        ".25p",
        ".24p",
        ".23p",
    )

    if name.endswith(nav_suffixes):
        return True

    return False


def _collect_nav_candidates(navigation_context: list[dict]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_candidate(path_value) -> None:
        if not path_value:
            return

        path = Path(str(path_value))
        if not path.exists() or not path.is_file():
            return

        if not _looks_like_navigation_filename(path):
            return

        key_str = str(path.resolve())
        if key_str in seen:
            return

        seen.add(key_str)
        candidates.append(path)

    for item in navigation_context or []:
        if not isinstance(item, dict):
            continue

        for key in (
            "nav_path",
            "navigation_file",
            "navigation_path",
            "broadcast_nav_path",
            "brdc_path",
        ):
            add_candidate(item.get(key))

        for key in (
            "obs_path",
            "observation_path",
            "rinex_path",
            "copied_path",
            "original_path",
            "path",
        ):
            value = item.get(key)
            if not value:
                continue

            detected = detect_navigation_file_near_observation(str(value))
            if detected:
                add_candidate(detected)

    print(
        f"[ADV-GEO] nav_candidates={len(candidates)} -> {[str(p) for p in candidates]}",
        flush=True,
    )

    for item in navigation_context or []:
        if isinstance(item, dict):
            print(f"[ADV-GEO] nav_context_item={item}", flush=True)

    return candidates


def _build_epoch_satellite_map_for_nav(
    normalized_time_grid: list[datetime],
) -> dict[str, list[str]]:
    gps_sats = [f"G{i:02d}" for i in range(1, 33)]
    return {_epoch_to_iso(epoch): gps_sats[:] for epoch in normalized_time_grid}


def _build_nav_geometry_cache(
    normalized_time_grid: list[datetime],
    precise_products: dict,
    navigation_context: list[dict],
) -> dict:
    nav_candidates = _collect_nav_candidates(navigation_context)

    if not nav_candidates:
        return {
            "epochs_requested": len(normalized_time_grid),
            "epochs_with_geometry": 0,
            "status": "nav_not_found",
            "source": "broadcast_nav",
            "products": precise_products,
            "navigation_context_count": len(navigation_context or []),
            "epochs": [],
        }

    epoch_satellite_map = _build_epoch_satellite_map_for_nav(normalized_time_grid)
    errors: list[str] = []

    for nav_path in nav_candidates:
        try:
            print(f"[ADV-GEO] tentando fallback NAV={nav_path}", flush=True)

            nav_cache = build_satellite_position_cache_from_nav(
                nav_file_path=str(nav_path),
                epoch_satellite_map=epoch_satellite_map,
            )

            if not isinstance(nav_cache, dict) or not nav_cache:
                errors.append(f"NAV vazio: {nav_path}")
                continue

            epochs_output = []
            epochs_with_geometry = 0
            satellite_catalog: set[str] = set()

            for idx_epoch, epoch_dt in enumerate(normalized_time_grid):
                epoch_iso = _epoch_to_iso(epoch_dt)
                sat_positions = nav_cache.get(epoch_iso, {}) or {}

                normalized_sat_positions = {}
                for sat, payload in sat_positions.items():
                    if not isinstance(payload, dict):
                        continue

                    x = _safe_float(payload.get("x"))
                    y = _safe_float(payload.get("y"))
                    z = _safe_float(payload.get("z"))

                    if x is None or y is None or z is None:
                        continue

                    normalized_sat_positions[sat] = {
                        "x": x,
                        "y": y,
                        "z": z,
                        "clock_us": _safe_float(payload.get("clock_us")),
                        "interpolated": False,
                    }
                    satellite_catalog.add(sat)

                if normalized_sat_positions:
                    epochs_with_geometry += 1

                if idx_epoch < 3:
                    print(
                        f"[ADV-GEO-NAV] epoch_sample target={epoch_iso} "
                        f"sat_count={len(normalized_sat_positions)} "
                        f"sats={list(sorted(normalized_sat_positions.keys()))[:12]}",
                        flush=True,
                    )

                epochs_output.append(
                    {
                        "epoch": epoch_iso,
                        "satellites": normalized_sat_positions,
                    }
                )

            print(
                f"[ADV-GEO] NAV result epochs_with_geometry={epochs_with_geometry} "
                f"satellite_count={len(satellite_catalog)} path={nav_path}",
                flush=True,
            )

            if epochs_with_geometry > 0:
                return {
                    "epochs_requested": len(normalized_time_grid),
                    "epochs_with_geometry": epochs_with_geometry,
                    "status": "completed",
                    "source": "broadcast_nav_fallback",
                    "nav_path": str(nav_path),
                    "products": precise_products,
                    "navigation_context_count": len(navigation_context or []),
                    "satellite_count": len(satellite_catalog),
                    "epochs": epochs_output,
                }

            errors.append(f"NAV sem geometria útil: {nav_path}")

        except Exception as exc:
            errors.append(f"{nav_path} :: {type(exc).__name__}: {exc}")

    return {
        "epochs_requested": len(normalized_time_grid),
        "epochs_with_geometry": 0,
        "status": "nav_geometry_failed",
        "source": "broadcast_nav",
        "products": precise_products,
        "navigation_context_count": len(navigation_context or []),
        "nav_candidates": [str(p) for p in nav_candidates],
        "errors": errors,
        "epochs": [],
    }


def build_precise_geometry_cache(
    time_grid: list,
    precise_products: dict,
    navigation_context: list[dict],
) -> dict:
    normalized_time_grid = _normalize_time_grid(time_grid)

    if not normalized_time_grid:
        return {
            "epochs_requested": 0,
            "epochs_with_geometry": 0,
            "status": "empty_time_grid",
            "products": precise_products,
            "navigation_context_count": len(navigation_context or []),
            "epochs": [],
        }

    sp3_result = _build_sp3_geometry_cache(
        normalized_time_grid=normalized_time_grid,
        precise_products=precise_products,
        navigation_context=navigation_context,
    )

    if sp3_result.get("epochs_with_geometry", 0) > 0:
        print("[ADV-GEO] usando geometria via SP3", flush=True)
        return sp3_result

    print(
        f"[ADV-GEO] SP3 indisponível/status={sp3_result.get('status')} -> tentando fallback NAV",
        flush=True,
    )

    nav_result = _build_nav_geometry_cache(
        normalized_time_grid=normalized_time_grid,
        precise_products=precise_products,
        navigation_context=navigation_context,
    )

    if nav_result.get("epochs_with_geometry", 0) > 0:
        nav_result["fallback_from_sp3_status"] = sp3_result.get("status")
        nav_result["sp3_attempt"] = {
            "status": sp3_result.get("status"),
            "sp3_path": sp3_result.get("sp3_path"),
        }
        print("[ADV-GEO] fallback NAV bem-sucedido", flush=True)
        return nav_result

    return {
        "epochs_requested": len(normalized_time_grid),
        "epochs_with_geometry": 0,
        "status": "geometry_unavailable",
        "source": "sp3_then_nav",
        "products": precise_products,
        "navigation_context_count": len(navigation_context or []),
        "sp3_attempt": {
            "status": sp3_result.get("status"),
            "sp3_path": sp3_result.get("sp3_path"),
        },
        "nav_attempt": {
            "status": nav_result.get("status"),
            "nav_path": nav_result.get("nav_path"),
            "errors": nav_result.get("errors", []),
            "nav_candidates": nav_result.get("nav_candidates", []),
        },
        "epochs": [],
    }
