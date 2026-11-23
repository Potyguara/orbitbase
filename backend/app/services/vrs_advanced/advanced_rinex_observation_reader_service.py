from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _safe_float(text: str) -> float | None:
    value = (text or "").strip().replace("D", "E")
    if not value:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_satellite_code(code: str) -> str:
    sat = (code or "").strip().upper()
    if not sat:
        return ""

    if len(sat) == 2 and sat[0].isalpha() and sat[1].isdigit():
        return f"{sat[0]}0{sat[1]}"

    if len(sat) == 3 and sat[0].isalpha():
        return sat

    return sat


def _parse_rinex2_epoch_line(line: str) -> tuple[datetime | None, int, int, list[str]]:
    """
    Exemplo RINEX 2:
     26  3 10 12  2 23.0000000  0 10G05G07G12...
    """
    try:
        yy = int(line[1:3])
        year = 2000 + yy if yy < 80 else 1900 + yy
        month = int(line[4:6])
        day = int(line[7:9])
        hour = int(line[10:12])
        minute = int(line[13:15])
        sec = float(line[15:26])

        flag = int((line[28:29] or "0").strip() or "0")
        nsat = int((line[29:32] or "0").strip() or "0")

        sec_int = int(sec)
        micro = int(round((sec - sec_int) * 1_000_000))
        if micro >= 1_000_000:
            sec_int += 1
            micro -= 1_000_000

        epoch_dt = datetime(
            year,
            month,
            day,
            hour,
            minute,
            sec_int,
            micro,
            tzinfo=timezone.utc,
        )

        sats: list[str] = []
        first_sat_block = line[32:68]
        for i in range(0, len(first_sat_block), 3):
            sat = _normalize_satellite_code(first_sat_block[i : i + 3])
            if sat:
                sats.append(sat)

        return epoch_dt, flag, nsat, sats[:nsat]
    except Exception:
        return None, 0, 0, []


def _parse_additional_sat_lines(
    lines: list[str],
    start_idx: int,
    already: list[str],
    total: int,
) -> tuple[list[str], int]:
    idx = start_idx
    sats = list(already)

    while len(sats) < total and idx < len(lines):
        line = lines[idx]
        # a lista de satélites das linhas de continuação fica sempre nas
        # colunas 33-68; linhas mais curtas (última linha da lista) também
        block = line[32:68]

        for i in range(0, len(block), 3):
            sat = _normalize_satellite_code(block[i : i + 3])
            if sat:
                sats.append(sat)
                if len(sats) >= total:
                    break

        idx += 1

    return sats[:total], idx


def _parse_obs_types_from_header(header_lines: list[str]) -> list[str]:
    """
    RINEX 2.x:
    cols 1-6  : number of obs types on first line
    cols 7-60 : up to 9 obs types per line, each 6 chars
    continuation lines repeat the same label
    """
    obs_types: list[str] = []
    expected_count: int | None = None

    for line in header_lines:
        label = line[60:80].strip()
        if label != "# / TYPES OF OBSERV":
            continue

        count_text = line[:6].strip()
        if count_text.isdigit() and expected_count is None:
            expected_count = int(count_text)

        fields = line[6:60]
        for i in range(0, len(fields), 6):
            code = fields[i : i + 6].strip().upper()
            if code:
                obs_types.append(code)

    if expected_count is not None:
        obs_types = obs_types[:expected_count]

    return obs_types


def _parse_antenna_delta_hen(header_lines: list[str]) -> tuple[float, float, float]:
    """ANTENNA: DELTA H/E/N do cabeçalho (altura da antena da base sobre o marco)."""
    for line in header_lines:
        if line[60:80].strip() == "ANTENNA: DELTA H/E/N":
            values = [_safe_float(line[i : i + 14]) for i in (0, 14, 28)]
            return tuple(v or 0.0 for v in values)  # type: ignore[return-value]
    return (0.0, 0.0, 0.0)


def _obs_lines_per_satellite(num_obs_types: int) -> int:
    return (num_obs_types + 4) // 5


def _parse_rinex2_observation_block(
    obs_lines: list[str],
    obs_types: list[str],
) -> dict[str, float | None]:
    """
    Cada observável ocupa 16 chars:
    14 para valor + LLI + SNR.
    """
    joined = "".join(f"{line.rstrip(chr(10)):<80}"[:80] for line in obs_lines)

    values: dict[str, float | None] = {}
    for idx, obs_type in enumerate(obs_types):
        start = idx * 16
        field = joined[start : start + 16]
        value_text = field[:14] if field else ""
        values[obs_type] = _safe_float(value_text)

    return values


def extract_rinex_observations_for_window(
    rinex_path: str | Path,
    start_utc: datetime,
    end_utc: datetime,
    step_seconds: int = 1,
    station_code: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    path = Path(rinex_path)
    if not path.exists():
        raise FileNotFoundError(f"RINEX não encontrado: {path}")

    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    header_lines: list[str] = []
    data_start_idx = 0

    for i, line in enumerate(lines):
        header_lines.append(line)
        if "END OF HEADER" in line:
            data_start_idx = i + 1
            break

    obs_types = _parse_obs_types_from_header(header_lines)
    if not obs_types:
        raise ValueError(f"Não foi possível identificar TYPES OF OBSERV em {path.name}")

    obs_line_count = _obs_lines_per_satellite(len(obs_types))

    extracted_epochs: list[dict[str, Any]] = []
    idx = data_start_idx

    while idx < len(lines):
        line = lines[idx]
        idx += 1

        if not line.strip():
            continue

        epoch_dt, flag, nsat, sats = _parse_rinex2_epoch_line(line)
        if epoch_dt is None:
            continue

        if len(sats) < nsat:
            sats, idx = _parse_additional_sat_lines(
                lines=lines,
                start_idx=idx,
                already=sats,
                total=nsat,
            )

        if flag not in (0, 1):
            idx += len(sats) * obs_line_count
            continue

        sat_items: list[dict[str, Any]] = []

        for sat in sats:
            sat_obs_lines = lines[idx : idx + obs_line_count]
            idx += obs_line_count

            obs_map = _parse_rinex2_observation_block(sat_obs_lines, obs_types)

            sat_items.append(
                {
                    "satellite": sat,
                    "observations": obs_map,
                }
            )

        if epoch_dt < start_utc or epoch_dt > end_utc:
            continue

        extracted_epochs.append(
            {
                "epoch_utc": _dt_to_iso(epoch_dt),
                "satellites": sat_items,
            }
        )

    return {
        "station_code": station_code,
        "label": label,
        "rinex_path": str(path),
        "obs_types": obs_types,
        "antenna_delta_hen": list(_parse_antenna_delta_hen(header_lines)),
        "step_seconds": step_seconds,
        "window_start_utc": _dt_to_iso(start_utc),
        "window_end_utc": _dt_to_iso(end_utc),
        "epochs": extracted_epochs,
        "epoch_count": len(extracted_epochs),
    }


def build_base_numeric_data_from_candidates(
    candidate_metas: list[dict[str, Any]],
    start_utc: datetime,
    end_utc: datetime,
    step_seconds: int = 1,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for meta in candidate_metas:
        rinex_path = (
            meta.get("rinex_path")
            or meta.get("copied_path")
            or meta.get("original_path")
        )
        if not rinex_path:
            continue

        extracted = extract_rinex_observations_for_window(
            rinex_path=rinex_path,
            start_utc=start_utc,
            end_utc=end_utc,
            step_seconds=step_seconds,
            station_code=meta.get("station_code"),
            label=meta.get("label"),
        )

        result.append(extracted)

    return result
