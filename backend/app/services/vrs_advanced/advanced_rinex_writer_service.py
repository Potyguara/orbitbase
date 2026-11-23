from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re
from typing import Any

HEADER_LABEL_COL = 60

DEFAULT_OBS_TYPES = ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_advanced_debug_seed(output_path: str, advanced_seed: dict) -> str | None:
    """Grava o seed completo só quando ORBITBASE_ADV_DUMP_SEEDS=1 (debug)."""
    from .advanced_json_io import debug_seed_dumps_enabled, dump_json_file

    if not debug_seed_dumps_enabled():
        return None
    return dump_json_file(output_path, advanced_seed, indent=None)


def _to_datetime(value: Any) -> datetime | None:
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _header_line(left: str, label: str) -> str:
    left = (left or "")[:60]
    label = (label or "")[:20]
    return f"{left:<60}{label:<20}\n"


def _obs_field(value: Any) -> str:
    if value is None or value == "":
        return " " * 16

    try:
        return f"{float(value):14.3f}  "
    except Exception:
        return " " * 16


def _normalize_satellite_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if not text:
        return ""

    if re.fullmatch(r"[A-Z]\d{2}", text):
        return text

    m = re.fullmatch(r"(\d{1,2})([A-Z])", text)
    if m:
        num, sys = m.groups()
        return f"{sys}{int(num):02d}"

    m = re.fullmatch(r"([A-Z])(\d{1,2})", text)
    if m:
        sys, num = m.groups()
        return f"{sys}{int(num):02d}"

    if re.fullmatch(r"\d{1,2}", text):
        return f"G{int(text):02d}"

    return ""


def _extract_epochs_from_seed(advanced_seed: dict) -> list[dict]:
    epochs = advanced_seed.get("epochs")
    return epochs if isinstance(epochs, list) else []


def _extract_epoch_dt(epoch: dict) -> datetime | None:
    for key in ("epoch_utc", "epoch", "time", "timestamp", "datetime"):
        if key in epoch:
            dt = _to_datetime(epoch.get(key))
            if dt is not None:
                return dt
    return None


def _extract_sat_items(epoch: dict) -> list[dict]:
    if isinstance(epoch.get("observations"), list):
        return epoch["observations"]
    if isinstance(epoch.get("satellites"), list):
        return epoch["satellites"]
    return []


def _extract_sat_code(sat_item: dict) -> str:
    for key in ("satellite", "sat", "prn"):
        value = sat_item.get(key)
        if value:
            sat = _normalize_satellite_code(value)
            if sat:
                return sat
    return ""


def _extract_obs_map(sat_item: dict) -> dict[str, float | None]:
    candidates = []

    raw_obs = sat_item.get("observations")
    if isinstance(raw_obs, dict):
        candidates.append(raw_obs)

    raw_obs_alt = sat_item.get("obs")
    if isinstance(raw_obs_alt, dict):
        candidates.append(raw_obs_alt)

    top_level = {}
    for k, v in sat_item.items():
        kk = str(k).strip().upper()
        if kk in {
            "C1",
            "P1",
            "L1",
            "D1",
            "S1",
            "C2",
            "P2",
            "L2",
            "D2",
            "S2",
            "C5",
            "L5",
            "D5",
            "S5",
            "C1C",
            "L1C",
            "D1C",
            "S1C",
            "C2W",
            "L2W",
            "D2W",
            "S2W",
            "C5Q",
            "L5Q",
            "D5Q",
            "S5Q",
        }:
            top_level[kk] = v

    if top_level:
        candidates.append(top_level)

    merged = {}
    for source in candidates:
        for k, v in source.items():
            kk = str(k).strip().upper()
            if not kk:
                continue
            merged[kk] = None if v in ("", None) else _safe_float(v, default=0.0)

    return merged


def _read_reference_obs_types(reference_rinex_path: str | None) -> list[str]:
    if not reference_rinex_path:
        return []

    path = Path(reference_rinex_path)
    if not path.exists():
        return []

    obs_types: list[str] = []

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            label = line[60:80].strip()

            if label == "# / TYPES OF OBSERV":
                left = line[:60]
                parts = left.split()
                if not parts:
                    continue

                codes = parts[1:] if parts[0].isdigit() else parts
                for code in codes:
                    code = code.strip().upper()
                    if code and code not in obs_types:
                        obs_types.append(code)

            elif label == "END OF HEADER":
                break

    return obs_types


def _filter_supported_obs_types(obs_types: list[str]) -> list[str]:
    return [obs for obs in DEFAULT_OBS_TYPES if obs in obs_types]


def _discover_obs_types(
    epoch_records: list[tuple[datetime, list[tuple[str, dict[str, float | None]]]]],
) -> list[str]:
    found: set[str] = set()

    for _, sat_records in epoch_records:
        for _, obs_map in sat_records:
            for code in obs_map.keys():
                found.add(code.upper())

    return [c for c in DEFAULT_OBS_TYPES if c in found]


def _sanitize_obs_map_for_rinex_211(
    obs_map: dict[str, float | None],
) -> dict[str, float | None]:
    cleaned: dict[str, float | None] = {}

    for obs in DEFAULT_OBS_TYPES:
        if obs in obs_map:
            cleaned[obs] = obs_map.get(obs)

    return cleaned


def _has_any(obs_map: dict[str, float | None], names: tuple[str, ...]) -> bool:
    return any(obs_map.get(name) is not None for name in names)


def _is_code_only_usable(obs_map: dict[str, float | None]) -> bool:
    has_l1_code = _has_any(obs_map, ("C1", "P1"))
    has_l2_code = _has_any(obs_map, ("C2", "P2"))
    return has_l1_code or has_l2_code


def _is_dual_frequency_phase_usable(obs_map: dict[str, float | None]) -> bool:
    has_l1_code = _has_any(obs_map, ("C1", "P1"))
    has_l2_code = _has_any(obs_map, ("C2", "P2"))
    has_l1_phase = obs_map.get("L1") is not None
    has_l2_phase = obs_map.get("L2") is not None
    return has_l1_code and has_l2_code and has_l1_phase and has_l2_phase


def _is_satellite_usable_for_writer(
    sat_code: str,
    obs_map: dict[str, float | None],
    require_phase_observables: bool,
    phase_systems: tuple[str, ...] = ("G",),
) -> bool:
    sat_code = _normalize_satellite_code(sat_code)
    if not sat_code:
        return False

    system = sat_code[:1]
    if system not in {"G", "R", "E", "C", "J", "I", "S"}:
        return False

    if require_phase_observables:
        if system not in phase_systems:
            return False
        return _is_dual_frequency_phase_usable(obs_map)

    return _is_code_only_usable(obs_map)


def _build_epoch_header_lines(epoch_dt: datetime, sat_codes: list[str]) -> list[str]:
    yy = epoch_dt.year % 100
    mm = epoch_dt.month
    dd = epoch_dt.day
    hh = epoch_dt.hour
    mi = epoch_dt.minute
    ss = epoch_dt.second + epoch_dt.microsecond / 1_000_000.0

    nsat = len(sat_codes)
    first_chunk = sat_codes[:12]

    first_line = (
        f" {yy:02d} {mm:02d} {dd:02d} {hh:02d} {mi:02d} {ss:10.7f}  0{nsat:3d}"
        + "".join(f"{sat:>3}" for sat in first_chunk)
    )
    lines = [f"{first_line:<80}\n"]

    remaining = sat_codes[12:]
    while remaining:
        chunk = remaining[:12]
        remaining = remaining[12:]
        cont_line = " " * 32 + "".join(f"{sat:>3}" for sat in chunk)
        lines.append(f"{cont_line:<80}\n")

    return lines


def _build_obs_lines(
    obs_map: dict[str, float | None],
    obs_types: list[str],
) -> list[str]:
    values = [_obs_field(obs_map.get(obs)) for obs in obs_types]
    lines: list[str] = []

    for i in range(0, len(values), 5):
        chunk = values[i : i + 5]
        lines.append(f"{''.join(chunk):<80}\n")

    return lines


def write_advanced_rinex_from_seed(
    output_path: str,
    advanced_seed: dict,
    reference_rinex_path: str | None = None,
    marker_name: str = "ADVBASE",
    receiver_brand: str = "ORBITBASE",
    receiver_model: str = "VIRTUAL_GNSS_ADV",
    receiver_version: str = "0.1-alpha",
    antenna_model: str = "VIRTUAL_ADV_ANT",
    approx_position_xyz: tuple[float, float, float] | None = None,
    antenna_delta_hen: tuple[float, float, float] | None = None,
    interval_seconds: float | None = None,
    require_phase_observables: bool = True,
    output_systems: list[str] | tuple[str, ...] | set[str] | None = None,
    forced_obs_types: list[str] | None = None,
    phase_systems: tuple[str, ...] = ("G",),
    receiver_number: str = "VRS",
) -> str:
    phase_systems = tuple(str(x).strip().upper() for x in phase_systems)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    raw_epochs = _extract_epochs_from_seed(advanced_seed)

    if output_systems is None:
        output_systems = ["G"] if require_phase_observables else ["G", "R"]

    output_systems = {
        str(sys).strip().upper() for sys in output_systems if str(sys).strip()
    }

    if not output_systems:
        output_systems = {"G"} if require_phase_observables else {"G", "R"}

    if not raw_epochs:
        raise ValueError("advanced_seed não possui épocas para escrita do RINEX.")

    epoch_records: list[tuple[datetime, list[tuple[str, dict[str, float | None]]]]] = []

    for epoch in raw_epochs:
        epoch_dt = _extract_epoch_dt(epoch)
        if epoch_dt is None:
            continue

        sat_records: list[tuple[str, dict[str, float | None]]] = []

        for sat_item in _extract_sat_items(epoch):
            sat_code = _extract_sat_code(sat_item)
            if not sat_code:
                continue

            if sat_code[:1] not in output_systems:
                continue

            obs_map = _extract_obs_map(sat_item)
            if not obs_map:
                continue

            obs_map = _sanitize_obs_map_for_rinex_211(obs_map)

            if not _is_satellite_usable_for_writer(
                sat_code=sat_code,
                obs_map=obs_map,
                require_phase_observables=require_phase_observables,
                phase_systems=phase_systems,
            ):
                continue

            sat_records.append((sat_code, obs_map))

        sat_records.sort(key=lambda x: x[0])

        if sat_records:
            epoch_records.append((epoch_dt, sat_records))

    if not epoch_records:
        if require_phase_observables:
            raise ValueError(
                "advanced_seed não possui satélites GPS com observáveis dual-frequency utilizáveis."
            )
        raise ValueError(
            "advanced_seed não possui satélites com observáveis de código utilizáveis."
        )

    if forced_obs_types:
        obs_types = [
            str(obs).strip().upper()
            for obs in forced_obs_types
            if str(obs).strip().upper() in DEFAULT_OBS_TYPES
        ]
    else:
        reference_obs_types = _filter_supported_obs_types(
            _read_reference_obs_types(reference_rinex_path)
        )

        if reference_obs_types:
            obs_types = [obs for obs in DEFAULT_OBS_TYPES if obs in reference_obs_types]
        else:
            discovered = _discover_obs_types(epoch_records)
            obs_types = discovered if discovered else DEFAULT_OBS_TYPES[:]

        essential_obs = ["C1", "P1", "C2", "P2"]
        if require_phase_observables:
            essential_obs = ["C1", "P1", "L1", "C2", "P2", "L2"]

        for obs in essential_obs:
            if obs not in obs_types:
                obs_types.append(obs)

        obs_types = [obs for obs in DEFAULT_OBS_TYPES if obs in obs_types]

    filtered_epoch_records: list[
        tuple[datetime, list[tuple[str, dict[str, float | None]]]]
    ] = []

    for epoch_dt, sat_records in epoch_records:
        new_sat_records = []

        for sat_code, obs_map in sat_records:
            filtered_map = {k: obs_map.get(k) for k in obs_types}

            if _is_satellite_usable_for_writer(
                sat_code=sat_code,
                obs_map=filtered_map,
                require_phase_observables=require_phase_observables,
                phase_systems=phase_systems,
            ):
                new_sat_records.append((sat_code, filtered_map))

        if new_sat_records:
            filtered_epoch_records.append((epoch_dt, new_sat_records))

    epoch_records = filtered_epoch_records

    if not epoch_records:
        if require_phase_observables:
            raise ValueError(
                "Nenhuma época permaneceu após o filtro final dual-frequency do writer."
            )
        raise ValueError(
            "Nenhuma época permaneceu após o filtro final code-only do writer."
        )

    if approx_position_xyz is not None:
        approx_x, approx_y, approx_z = approx_position_xyz
    else:
        approx_x = _safe_float(advanced_seed.get("approx_x"))
        approx_y = _safe_float(advanced_seed.get("approx_y"))
        approx_z = _safe_float(advanced_seed.get("approx_z"))

    if antenna_delta_hen is not None:
        ant_h, ant_e, ant_n = antenna_delta_hen
    else:
        ant_h = _safe_float(advanced_seed.get("antenna_h"))
        ant_e = _safe_float(advanced_seed.get("antenna_e"))
        ant_n = _safe_float(advanced_seed.get("antenna_n"))

    first_dt = epoch_records[0][0]
    last_dt = epoch_records[-1][0]

    if interval_seconds is None:
        interval_seconds = _safe_float(advanced_seed.get("interval_seconds"), 1.0)

    if require_phase_observables:
        present = {code[:1] for _, recs in epoch_records for code, _ in recs}
        rinex_system = "M" if len(present) > 1 else (next(iter(present)) if present else "G")
    else:
        rinex_system = "M" if len(output_systems) > 1 else next(iter(output_systems))

    print(f"[ADV-WRITER] epoch_records={len(epoch_records)}", flush=True)
    print(f"[ADV-WRITER] obs_types={obs_types}", flush=True)
    print(
        f"[ADV-WRITER] require_phase_observables={require_phase_observables}",
        flush=True,
    )
    print(
        f"[ADV-WRITER] output_systems={sorted(output_systems)}",
        flush=True,
    )
    print(
        f"[ADV-WRITER] first_epoch={first_dt.isoformat()} last_epoch={last_dt.isoformat()}",
        flush=True,
    )

    lines: list[str] = []

    lines.append(
        _header_line(
            f"     2.11           OBSERVATION DATA    {rinex_system}",
            "RINEX VERSION / TYPE",
        )
    )
    lines.append(
        _header_line(
            "OrbitBase           OrbitBase            "
            + datetime.utcnow().strftime("%d-%b-%y %H:%M").upper(),
            "PGM / RUN BY / DATE",
        )
    )
    lines.append(_header_line(marker_name[:60], "MARKER NAME"))
    lines.append(_header_line("", "MARKER NUMBER"))
    lines.append(
        _header_line("OrbitBase           Biome Projetos", "OBSERVER / AGENCY")
    )
    lines.append(
        _header_line(
            f"{receiver_number[:20]:<20}{receiver_model[:20]:<20}{receiver_version[:20]:<20}",
            "REC # / TYPE / VERS",
        )
    )
    lines.append(
        _header_line(
            f"{'VRS':<20}{antenna_model[:20]:<20}",
            "ANT # / TYPE",
        )
    )
    lines.append(
        _header_line(
            f"{approx_x:14.4f}{approx_y:14.4f}{approx_z:14.4f}",
            "APPROX POSITION XYZ",
        )
    )
    lines.append(
        _header_line(
            f"{ant_h:14.4f}{ant_e:14.4f}{ant_n:14.4f}",
            "ANTENNA: DELTA H/E/N",
        )
    )
    lines.append(_header_line("     1     1", "WAVELENGTH FACT L1/2"))

    total_obs = len(obs_types)
    for i in range(0, total_obs, 9):
        chunk = obs_types[i : i + 9]
        prefix = f"{total_obs:6d}" if i == 0 else " " * 6
        left = prefix + "".join(f"{obs:>6}" for obs in chunk)
        lines.append(_header_line(left, "# / TYPES OF OBSERV"))

    lines.append(_header_line(f"{interval_seconds:10.3f}", "INTERVAL"))
    lines.append(_header_line("    18", "LEAP SECONDS"))
    lines.append(
        _header_line(
            (
                "VIRTUAL REFERENCE STATION - OrbitBase"
                if require_phase_observables
                else "VIRTUAL REFERENCE STATION - OrbitBase"
            ),
            "COMMENT",
        )
    )
    lines.append(
        _header_line(
            f"{first_dt.year:6d}{first_dt.month:6d}{first_dt.day:6d}{first_dt.hour:6d}{first_dt.minute:6d}{(first_dt.second + first_dt.microsecond / 1_000_000.0):13.7f}     GPS",
            "TIME OF FIRST OBS",
        )
    )
    lines.append(
        _header_line(
            f"{last_dt.year:6d}{last_dt.month:6d}{last_dt.day:6d}{last_dt.hour:6d}{last_dt.minute:6d}{(last_dt.second + last_dt.microsecond / 1_000_000.0):13.7f}     GPS",
            "TIME OF LAST OBS",
        )
    )
    lines.append(_header_line("", "END OF HEADER"))

    for epoch_dt, sat_records in epoch_records:
        sat_codes = [sat for sat, _ in sat_records]
        lines.extend(_build_epoch_header_lines(epoch_dt, sat_codes))

        for _, obs_map in sat_records:
            lines.extend(_build_obs_lines(obs_map, obs_types))

    path.write_text("".join(lines), encoding="utf-8")
    return str(path)
