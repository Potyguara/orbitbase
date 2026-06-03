from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from app.services.rinex_epochs_service import try_parse_epoch_from_line


EPOCH_V2_RE = re.compile(
    r"^\s*(\d{2}|\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([0-9]+\.[0-9]+)"
)


def _read_lines(file_path: str) -> list[str]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()


def _skip_header(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if "END OF HEADER" in line:
            return idx + 1
    raise ValueError("END OF HEADER não encontrado.")


def normalize_satellite_id(token: str) -> str | None:
    if not token:
        return None

    raw = token.strip().upper().replace(" ", "")
    if not raw or len(raw) < 2:
        return None

    system = raw[0]
    number_part = raw[1:]

    if not system.isalpha():
        return None

    try:
        prn = int(number_part)
    except Exception:
        return None

    return f"{system}{prn:02d}"


def _parse_epoch_header_v2(line: str) -> tuple[datetime | None, int | None]:
    epoch_dt = try_parse_epoch_from_line(line)
    if not epoch_dt:
        return None, None

    sat_count = None

    try:
        raw_count = line[29:32].strip()
        if raw_count:
            sat_count = int(raw_count)
    except Exception:
        sat_count = None

    if sat_count is None:
        parts = line.split()
        candidates = []
        for token in parts:
            try:
                candidates.append(int(token))
            except Exception:
                pass
        if candidates:
            sat_count = candidates[-1]

    return epoch_dt, sat_count


def _extract_satellite_ids_from_epoch_header(
    line: str, sat_count: int | None
) -> list[str]:
    if sat_count is None or sat_count <= 0:
        return []

    tail = line[32:]
    sats = []

    for i in range(0, len(tail), 3):
        token = tail[i : i + 3]
        normalized = normalize_satellite_id(token)
        if normalized:
            sats.append(normalized)

    return sats[:sat_count]


def iter_observation_blocks(file_path: str):
    lines = _read_lines(file_path)
    start_idx = _skip_header(lines)

    i = start_idx
    total = len(lines)

    while i < total:
        line = lines[i]
        epoch_dt, sat_count = _parse_epoch_header_v2(line)

        if not epoch_dt:
            i += 1
            continue

        satellite_ids = _extract_satellite_ids_from_epoch_header(line, sat_count)
        block_lines = [line.rstrip("\n")]

        j = i + 1
        next_epoch_idx = None

        while j < total:
            maybe_next_epoch, _ = _parse_epoch_header_v2(lines[j])
            if maybe_next_epoch:
                next_epoch_idx = j
                break
            block_lines.append(lines[j].rstrip("\n"))
            j += 1

        yield {
            "epoch": epoch_dt,
            "epoch_iso": epoch_dt.isoformat(),
            "satellite_count_header": sat_count,
            "satellite_ids": satellite_ids,
            "block_line_count": len(block_lines),
            "line_number_start": i + 1,
            "raw_epoch_header": line.rstrip("\n"),
        }

        i = next_epoch_idx if next_epoch_idx is not None else total


def build_epoch_index(
    file_path: str, start_dt: datetime, end_dt: datetime
) -> dict[str, dict]:
    result = {}

    for block in iter_observation_blocks(file_path):
        epoch = block["epoch"]
        if start_dt <= epoch <= end_dt:
            result[block["epoch_iso"]] = {
                "satellite_count_header": block["satellite_count_header"],
                "satellite_ids": block["satellite_ids"],
                "block_line_count": block["block_line_count"],
                "line_number_start": block["line_number_start"],
                "raw_epoch_header": block["raw_epoch_header"],
            }

    return result


def build_network_only_synchronized_observation_blocks(
    base1_path: str,
    base2_path: str,
    base3_path: str,
    start_dt: datetime,
    end_dt: datetime,
) -> dict:
    base1_idx = build_epoch_index(base1_path, start_dt, end_dt)
    base2_idx = build_epoch_index(base2_path, start_dt, end_dt)
    base3_idx = build_epoch_index(base3_path, start_dt, end_dt)

    common_epochs = sorted(
        set(base1_idx.keys()) & set(base2_idx.keys()) & set(base3_idx.keys())
    )

    blocks = []

    for epoch_iso in common_epochs:
        base1_sats = set(base1_idx[epoch_iso]["satellite_ids"])
        base2_sats = set(base2_idx[epoch_iso]["satellite_ids"])
        base3_sats = set(base3_idx[epoch_iso]["satellite_ids"])

        common_sats = sorted(base1_sats & base2_sats & base3_sats)
        union_sats = sorted(base1_sats | base2_sats | base3_sats)

        blocks.append(
            {
                "epoch": epoch_iso,
                "common_satellite_count": len(common_sats),
                "common_satellites": common_sats,
                "union_satellite_count": len(union_sats),
                "base_1": base1_idx[epoch_iso],
                "base_2": base2_idx[epoch_iso],
                "base_3": base3_idx[epoch_iso],
            }
        )

    summary = {
        "processing_mode": "network_only",
        "window": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        },
        "file_epoch_counts": {
            "base_1": len(base1_idx),
            "base_2": len(base2_idx),
            "base_3": len(base3_idx),
        },
        "common_epoch_count": len(common_epochs),
        "epochs_with_common_satellites": sum(
            1 for block in blocks if block["common_satellite_count"] > 0
        ),
        "max_common_satellite_count": max(
            (block["common_satellite_count"] for block in blocks),
            default=0,
        ),
        "sample_blocks": blocks[:20],
    }

    return {
        "summary": summary,
        "blocks": blocks,
    }
