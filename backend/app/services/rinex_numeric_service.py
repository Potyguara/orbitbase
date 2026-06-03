from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import math

from app.services.rinex_epochs_service import try_parse_epoch_from_line
from app.services.rinex_observation_blocks_service import normalize_satellite_id

FIELD_WIDTH = 16
VALUE_WIDTH = 14

SUPPORTED_OBS_PRIORITY = ["C1", "P1", "L1", "C2", "P2", "L2", "C5", "S1", "S2", "S5"]


def _read_lines(file_path: str) -> list[str]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()


def _header_end_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if "END OF HEADER" in line:
            return idx + 1
    raise ValueError("END OF HEADER não encontrado.")


def _is_blank_line(line: str) -> bool:
    return not line or not line.strip()


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_epoch_key(epoch_value) -> str | None:
    if epoch_value is None:
        return None

    if isinstance(epoch_value, datetime):
        return epoch_value.isoformat()

    text = str(epoch_value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text).isoformat()
    except Exception:
        return text


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


_DEBUG_LOOKUP_SATXYZ_MISS_COUNT = 0


def _lookup_sat_xyz(
    sat_xyz_cache: dict | None,
    epoch_iso: str,
    satellite_id: str,
) -> dict | None:
    global _DEBUG_LOOKUP_SATXYZ_MISS_COUNT

    if not sat_xyz_cache:
        return None

    bucket = sat_xyz_cache.get(epoch_iso)
    if not isinstance(bucket, dict):
        if _DEBUG_LOOKUP_SATXYZ_MISS_COUNT < 20:
            print(
                f"[DEBUG-LOOKUP-SATXYZ] epoch sem bucket | epoch={epoch_iso} | sat={satellite_id}",
                flush=True,
            )
            _DEBUG_LOOKUP_SATXYZ_MISS_COUNT += 1
        return None

    sat_norm = _normalize_satellite_id(satellite_id)
    value = bucket.get(sat_norm)

    if value is None and _DEBUG_LOOKUP_SATXYZ_MISS_COUNT < 40:
        sample_keys = sorted(bucket.keys())[:10]
        gps_keys_sample = [k for k in sample_keys if k.startswith("G")]
        print(
            f"[DEBUG-LOOKUP-SATXYZ] satélite não encontrado no bucket | "
            f"epoch={epoch_iso} | sat_req={satellite_id} | sat_norm={sat_norm} | "
            f"sample_keys={sample_keys} | gps_keys_sample={gps_keys_sample}",
            flush=True,
        )
        _DEBUG_LOOKUP_SATXYZ_MISS_COUNT += 1

    return value


def _norm_epoch_key(value) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    text = text.replace("Z", "")
    text = text.replace(" ", "T")

    try:
        dt = datetime.fromisoformat(text)
        return dt.replace(microsecond=0).isoformat()
    except Exception:
        pass

    known_formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%y %m %d %H %M %S",
        "%y %m %d %H %M %S.%f",
    ]

    for fmt in known_formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(microsecond=0).isoformat()
        except Exception:
            continue

    return text


def _parse_header_observation_types(lines: list[str]) -> list[str]:
    obs_types: list[str] = []

    for line in lines:
        if "END OF HEADER" in line:
            break

        if "# / TYPES OF OBSERV" in line:
            raw = line[:60]
            parts = raw.split()
            if not parts:
                continue

            if parts[0].isdigit():
                items = parts[1:]
            else:
                items = parts

            obs_types.extend(items)

    seen = set()
    ordered = []
    for item in obs_types:
        if item not in seen:
            seen.add(item)
            ordered.append(item)

    return ordered


def _parse_epoch_header(line: str) -> tuple[datetime | None, int | None, list[str]]:
    epoch_dt = try_parse_epoch_from_line(line)
    if not epoch_dt:
        return None, None, []

    sat_count = None
    try:
        raw_count = line[29:32].strip()
        if raw_count:
            sat_count = int(raw_count)
    except Exception:
        sat_count = None

    satellite_ids: list[str] = []

    if sat_count and sat_count > 0:
        first_line_sat_chunk = line[32:68]
        for i in range(0, len(first_line_sat_chunk), 3):
            token = first_line_sat_chunk[i : i + 3]
            sat = normalize_satellite_id(token)
            if sat:
                satellite_ids.append(sat)

    return epoch_dt, sat_count, satellite_ids


def _parse_continuation_satellite_ids(line: str) -> list[str]:
    """
    Em RINEX 2.xx, continuação da lista de satélites fica a partir da coluna 33.
    Não deve ler do início da linha, senão confunde linha de observação com continuação.
    """
    sats = []
    chunk = line[32:80]

    for i in range(0, len(chunk), 3):
        token = chunk[i : i + 3]
        sat = normalize_satellite_id(token)
        if sat:
            sats.append(sat)

    return sats


def _looks_like_epoch_header(line: str) -> bool:
    epoch_dt, _, _ = _parse_epoch_header(line)
    return epoch_dt is not None


def _looks_like_satellite_continuation(line: str) -> bool:
    """
    Continuação da lista de satélites em RINEX 2 geralmente tem as 32 primeiras colunas vazias.
    """
    if not line:
        return False

    if line[:32].strip():
        return False

    sats = _parse_continuation_satellite_ids(line)
    return len(sats) > 0


def _collect_epoch_block(
    lines: list[str],
    start_idx: int,
    obs_type_count: int,
) -> tuple[dict | None, int]:
    total = len(lines)
    line = lines[start_idx]

    epoch_dt, sat_count, satellite_ids = _parse_epoch_header(line)
    if not epoch_dt:
        return None, start_idx + 1

    i = start_idx + 1

    if sat_count is None:
        sat_count = 0

    continuation_lines = []

    while len(satellite_ids) < sat_count and i < total:
        next_line = lines[i]

        if _looks_like_epoch_header(next_line):
            break

        if _is_blank_line(next_line):
            i += 1
            continue

        if not _looks_like_satellite_continuation(next_line):
            break

        continuation_sats = _parse_continuation_satellite_ids(next_line)
        if not continuation_sats:
            break

        continuation_lines.append(next_line.rstrip("\n"))
        satellite_ids.extend(continuation_sats)
        i += 1

    satellite_ids = satellite_ids[:sat_count]

    observation_line_count_per_sat = max(1, math.ceil(obs_type_count / 5))

    while i < total and _is_blank_line(lines[i]):
        i += 1

    satellite_records = []

    for sat in satellite_ids:
        sat_lines = []

        for _ in range(observation_line_count_per_sat):
            if i >= total:
                break

            next_line = lines[i]

            if _looks_like_epoch_header(next_line):
                break

            sat_lines.append(next_line.rstrip("\n"))
            i += 1

        satellite_records.append(
            {
                "satellite": sat,
                "raw_lines": sat_lines,
            }
        )

    return {
        "epoch": epoch_dt,
        "epoch_iso": epoch_dt.isoformat(),
        "satellite_count_header": sat_count,
        "satellite_ids": satellite_ids,
        "observation_line_count_per_sat": observation_line_count_per_sat,
        "raw_epoch_header": line.rstrip("\n"),
        "continuation_lines": continuation_lines,
        "satellite_records_raw": satellite_records,
    }, i


def _parse_observation_values_from_lines(
    sat_lines: list[str],
    obs_types: list[str],
) -> dict[str, float | None]:
    if not sat_lines:
        return {obs: None for obs in obs_types}

    merged = "".join((line[:80]).ljust(80) for line in sat_lines)

    values: list[float | None] = []
    obs_count = len(obs_types)

    for idx in range(obs_count):
        start = idx * FIELD_WIDTH
        end = start + FIELD_WIDTH
        field = merged[start:end]

        if not field:
            values.append(None)
            continue

        raw_value = field[:VALUE_WIDTH].strip()
        if not raw_value:
            values.append(None)
            continue

        try:
            values.append(float(raw_value.replace("D", "E")))
        except Exception:
            values.append(None)

    mapped = {}
    for obs_idx, obs_name in enumerate(obs_types):
        mapped[obs_name] = values[obs_idx] if obs_idx < len(values) else None

    return mapped


def extract_numeric_observations_in_window(
    file_path: str,
    start_dt: datetime,
    end_dt: datetime,
    max_epochs: int | None = None,
    sat_xyz_cache: dict | None = None,
) -> dict:
    lines = _read_lines(file_path)
    obs_types = _parse_header_observation_types(lines)
    data_start = _header_end_index(lines)

    epochs = []
    i = data_start
    total = len(lines)

    while i < total:
        if max_epochs is not None and len(epochs) >= max_epochs:
            break

        block, next_idx = _collect_epoch_block(lines, i, len(obs_types))

        if block is None:
            i = next_idx
            continue

        epoch_dt = block["epoch"]

        if not (start_dt <= epoch_dt <= end_dt):
            i = next_idx
            continue

        parsed_satellite_records = []

        for sat_record in block["satellite_records_raw"]:
            satellite_id = sat_record["satellite"]

            observations = _parse_observation_values_from_lines(
                sat_record["raw_lines"],
                obs_types,
            )

            non_null_count = sum(1 for v in observations.values() if v is not None)

            sat_xyz = _lookup_sat_xyz(
                sat_xyz_cache=sat_xyz_cache,
                epoch_iso=block["epoch_iso"],
                satellite_id=satellite_id,
            )

            parsed_satellite_records.append(
                {
                    "satellite": satellite_id,
                    "available_observation_count": non_null_count,
                    "observations": observations,
                    "sat_xyz": sat_xyz,
                    "debug": {
                        "raw_lines": sat_record["raw_lines"],
                        "sat_xyz_found": sat_xyz is not None,
                    },
                }
            )

        satellite_count_with_values = sum(
            1
            for sat in parsed_satellite_records
            if sat["available_observation_count"] > 0
        )

        sample_satellites = parsed_satellite_records[:5]

        epochs.append(
            {
                "epoch": block["epoch_iso"],
                "satellite_count_header": block["satellite_count_header"],
                "satellite_ids": block["satellite_ids"],
                "raw_epoch_header": block["raw_epoch_header"],
                "observation_line_count_per_sat": block[
                    "observation_line_count_per_sat"
                ],
                "satellite_count_with_values": satellite_count_with_values,
                "sample_satellites": sample_satellites,
                "satellite_records": parsed_satellite_records,
            }
        )

        i = next_idx

    first_epoch = epochs[0] if epochs else None
    sat_records = first_epoch.get("satellite_records", []) if first_epoch else []

    first_sat = sat_records[0] if sat_records else None
    first_gps_sat = next(
        (
            sat
            for sat in sat_records
            if str(sat.get("satellite", "")).strip().upper().startswith("G")
        ),
        None,
    )

    gps_sats_total = sum(
        1
        for sat in sat_records
        if str(sat.get("satellite", "")).strip().upper().startswith("G")
    )

    gps_sats_with_xyz = sum(
        1
        for sat in sat_records
        if str(sat.get("satellite", "")).strip().upper().startswith("G")
        and sat.get("sat_xyz") is not None
    )

    print("[DEBUG-SATXYZ-INJECT] ===== INSPEÇÃO FINAL =====", flush=True)
    print(f"[DEBUG-SATXYZ-INJECT] file={file_path}", flush=True)
    print(
        f"[DEBUG-SATXYZ-INJECT] sat_xyz_cache informado? {sat_xyz_cache is not None}",
        flush=True,
    )
    print(f"[DEBUG-SATXYZ-INJECT] epochs extraídas={len(epochs)}", flush=True)

    if first_epoch:
        print(
            f"[DEBUG-SATXYZ-INJECT] first_epoch={first_epoch.get('epoch')}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_epoch satellite_records={len(sat_records)}",
            flush=True,
        )

    if first_sat:
        print(
            f"[DEBUG-SATXYZ-INJECT] first_sat keys={list(first_sat.keys())}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_sat sat={first_sat.get('satellite')}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_sat has sat_xyz? {first_sat.get('sat_xyz') is not None}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_sat sat_xyz={first_sat.get('sat_xyz')}",
            flush=True,
        )

    print(
        f"[DEBUG-SATXYZ-INJECT] gps_sats_total_first_epoch={gps_sats_total}",
        flush=True,
    )
    print(
        f"[DEBUG-SATXYZ-INJECT] gps_sats_with_xyz_first_epoch={gps_sats_with_xyz}",
        flush=True,
    )

    if first_gps_sat:
        print(
            f"[DEBUG-SATXYZ-INJECT] first_gps_sat keys={list(first_gps_sat.keys())}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_gps_sat sat={first_gps_sat.get('satellite')}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_gps_sat has sat_xyz? {first_gps_sat.get('sat_xyz') is not None}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_gps_sat sat_xyz={first_gps_sat.get('sat_xyz')}",
            flush=True,
        )
        print(
            f"[DEBUG-SATXYZ-INJECT] first_gps_sat obs keys={list((first_gps_sat.get('observations') or {}).keys())}",
            flush=True,
        )
    else:
        print(
            "[DEBUG-SATXYZ-INJECT] nenhum satélite GPS encontrado na primeira época",
            flush=True,
        )

    print("[DEBUG-SATXYZ-INJECT] ===== FIM =====", flush=True)

    return {
        "file_path": file_path,
        "observation_types": obs_types,
        "epoch_count_extracted": len(epochs),
        "epochs": epochs,
    }


def summarize_numeric_observations(numeric_data: dict) -> dict:
    obs_types = numeric_data.get("observation_types", [])
    epochs = numeric_data.get("epochs", [])

    total_satellite_records = 0
    total_non_null_values = 0
    per_obs_non_null = {obs: 0 for obs in obs_types}

    for epoch in epochs:
        for sat in epoch.get("satellite_records", []):
            total_satellite_records += 1
            observations = sat.get("observations", {})
            for obs_name, value in observations.items():
                if value is not None:
                    total_non_null_values += 1
                    per_obs_non_null[obs_name] = per_obs_non_null.get(obs_name, 0) + 1

    sample_epochs = []
    for epoch in epochs[:10]:
        sample_epochs.append(
            {
                "epoch": epoch.get("epoch"),
                "satellite_count_header": epoch.get("satellite_count_header"),
                "satellite_ids": epoch.get("satellite_ids", [])[:12],
                "raw_epoch_header": epoch.get("raw_epoch_header"),
                "observation_line_count_per_sat": epoch.get(
                    "observation_line_count_per_sat"
                ),
                "satellite_count_with_values": epoch.get("satellite_count_with_values"),
                "sample_satellites": epoch.get("sample_satellites", []),
            }
        )

    return {
        "file_path": numeric_data.get("file_path"),
        "observation_types": obs_types,
        "epoch_count_extracted": len(epochs),
        "total_satellite_records": total_satellite_records,
        "total_non_null_values": total_non_null_values,
        "per_observation_non_null": per_obs_non_null,
        "sample_epochs": sample_epochs,
    }


def _index_epochs(numeric_data: dict) -> dict[str, dict]:
    return {epoch["epoch"]: epoch for epoch in numeric_data.get("epochs", [])}


def _index_satellite_records(epoch_record: dict) -> dict[str, dict]:
    sats = epoch_record.get("satellite_records", []) or []
    return {sat["satellite"]: sat for sat in sats}


def _sat_has_min_observations(sat_record: dict, min_obs: int = 1) -> bool:
    if not sat_record:
        return False

    available = sat_record.get("available_observation_count", 0) or 0
    if available >= min_obs:
        return True

    observations = sat_record.get("observations", {}) or {}
    non_null = sum(1 for v in observations.values() if v is not None)
    return non_null >= min_obs


def _common_observation_types(
    base1_numeric: dict,
    base2_numeric: dict,
    base3_numeric: dict,
) -> list[str]:
    obs1 = set(base1_numeric.get("observation_types", []) or [])
    obs2 = set(base2_numeric.get("observation_types", []) or [])
    obs3 = set(base3_numeric.get("observation_types", []) or [])

    common = obs1 & obs2 & obs3
    ordered = [obs for obs in SUPPORTED_OBS_PRIORITY if obs in common]
    extras = sorted(common - set(ordered))
    return ordered + extras


def build_network_only_virtual_station_seed(
    base1_numeric: dict,
    base2_numeric: dict,
    base3_numeric: dict,
    max_epochs: int | None = None,
    min_bases_per_satellite: int = 2,
) -> dict:
    base1_epochs = _index_epochs(base1_numeric)
    base2_epochs = _index_epochs(base2_numeric)
    base3_epochs = _index_epochs(base3_numeric)

    common_epochs = sorted(
        set(base1_epochs.keys()) & set(base2_epochs.keys()) & set(base3_epochs.keys())
    )

    epoch_list = common_epochs if max_epochs is None else common_epochs[:max_epochs]

    seed_blocks = []

    for epoch in epoch_list:
        base1_sats = {s["satellite"] for s in base1_epochs[epoch]["satellite_records"]}
        base2_sats = {s["satellite"] for s in base2_epochs[epoch]["satellite_records"]}
        base3_sats = {s["satellite"] for s in base3_epochs[epoch]["satellite_records"]}

        all_sats = sorted(base1_sats | base2_sats | base3_sats)

        reusable_sats = []
        for sat in all_sats:
            present_count = sum(
                [
                    sat in base1_sats,
                    sat in base2_sats,
                    sat in base3_sats,
                ]
            )
            if present_count >= min_bases_per_satellite:
                reusable_sats.append(sat)

        seed_blocks.append(
            {
                "epoch": epoch,
                "candidate_satellites": all_sats,
                "reusable_satellites": reusable_sats,
                "reusable_satellite_count": len(reusable_sats),
            }
        )

    return {
        "processing_mode": "network_only",
        "common_epoch_count": len(common_epochs),
        "seed_block_count": len(seed_blocks),
        "min_bases_per_satellite": min_bases_per_satellite,
        "sample_seed_blocks": seed_blocks[:20],
    }


def _inverse_distance_weights(distances: list[float]) -> list[float]:
    cleaned = []
    for d in distances:
        if d is None or d <= 0:
            cleaned.append(0.001)
        else:
            cleaned.append(d)

    inv = [1.0 / d for d in cleaned]
    total = sum(inv)

    if total == 0:
        return [1 / len(cleaned)] * len(cleaned)

    return [v / total for v in inv]


def _sat_has_any_value(sat_record: dict) -> bool:
    observations = (sat_record or {}).get("observations", {}) or {}
    return any(v is not None for v in observations.values())


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
) -> dict:
    base1_epochs = _index_epochs(base1_numeric)
    base2_epochs = _index_epochs(base2_numeric)
    base3_epochs = _index_epochs(base3_numeric)

    common_epochs = sorted(
        set(base1_epochs.keys()) & set(base2_epochs.keys()) & set(base3_epochs.keys())
    )

    base_distances = {
        "base_1": base1_distance_m,
        "base_2": base2_distance_m,
        "base_3": base3_distance_m,
    }

    common_obs_types = _common_observation_types(
        base1_numeric,
        base2_numeric,
        base3_numeric,
    )

    epoch_list = common_epochs if max_epochs is None else common_epochs[:max_epochs]
    seed_epochs = []

    for epoch_iso in epoch_list:
        e1 = base1_epochs[epoch_iso]
        e2 = base2_epochs[epoch_iso]
        e3 = base3_epochs[epoch_iso]

        s1 = _index_satellite_records(e1)
        s2 = _index_satellite_records(e2)
        s3 = _index_satellite_records(e3)

        # ROBUSTO: união dos satélites, não interseção cega
        all_sats = sorted(set(s1.keys()) | set(s2.keys()) | set(s3.keys()))
        synthesized_satellites = []

        for sat in all_sats:
            sat1 = s1.get(sat)
            sat2 = s2.get(sat)
            sat3 = s3.get(sat)

            synthesized_observations = {}

            for obs_name in common_obs_types:
                candidates = []

                if sat1:
                    v1 = _safe_float((sat1.get("observations") or {}).get(obs_name))
                    if v1 is not None:
                        candidates.append(("base_1", v1, base_distances["base_1"]))

                if sat2:
                    v2 = _safe_float((sat2.get("observations") or {}).get(obs_name))
                    if v2 is not None:
                        candidates.append(("base_2", v2, base_distances["base_2"]))

                if sat3:
                    v3 = _safe_float((sat3.get("observations") or {}).get(obs_name))
                    if v3 is not None:
                        candidates.append(("base_3", v3, base_distances["base_3"]))

                if len(candidates) < min_bases_per_observation:
                    continue

                local_weights = _inverse_distance_weights(
                    [item[2] for item in candidates]
                )

                synthesized_value = 0.0
                for idx, (_, obs_value, _) in enumerate(candidates):
                    synthesized_value += obs_value * local_weights[idx]

                synthesized_observations[obs_name] = synthesized_value

            non_null_count = sum(
                1 for v in synthesized_observations.values() if v is not None
            )

            # satélite só entra se tiver observação útil
            if non_null_count > 0:
                synthesized_satellites.append(
                    {
                        "satellite": sat,
                        "available_observation_count": non_null_count,
                        "observations": synthesized_observations,
                        "present_in_bases": [
                            base_name
                            for base_name, sat_record in [
                                ("base_1", sat1),
                                ("base_2", sat2),
                                ("base_3", sat3),
                            ]
                            if sat_record is not None
                        ],
                    }
                )

        seed_epochs.append(
            {
                "epoch": epoch_iso,
                "candidate_satellite_count": len(all_sats),
                "generated_satellite_count": len(synthesized_satellites),
                "satellites": synthesized_satellites,
            }
        )

    generated_epoch_count = sum(
        1 for epoch in seed_epochs if (epoch.get("satellites") or [])
    )

    return {
        "processing_mode": "network_only",
        "method": "inverse_distance_weighting_v2_union_satellites",
        "target": target_meta,
        "weights_reference": {
            "base_1": base1_distance_m,
            "base_2": base2_distance_m,
            "base_3": base3_distance_m,
        },
        "common_observation_types": common_obs_types,
        "common_epoch_count": len(common_epochs),
        "generated_epoch_count": generated_epoch_count,
        "min_bases_per_observation": min_bases_per_observation,
        "epochs": seed_epochs,
    }


def summarize_virtual_observation_seed(seed: dict) -> dict:
    epochs = seed.get("epochs", []) or []
    obs_types = seed.get("common_observation_types", []) or []

    max_sat_count = 0
    epochs_with_satellites = 0
    per_obs_non_null = {obs: 0 for obs in obs_types}

    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        if sats:
            epochs_with_satellites += 1

        max_sat_count = max(max_sat_count, len(sats))

        for sat in sats:
            observations = sat.get("observations", {}) or {}
            for obs_name, value in observations.items():
                if value is not None:
                    per_obs_non_null[obs_name] = per_obs_non_null.get(obs_name, 0) + 1

    return {
        "processing_mode": seed.get("processing_mode"),
        "method": seed.get("method"),
        "common_epoch_count": seed.get("common_epoch_count", 0),
        "generated_epoch_count": seed.get("generated_epoch_count", 0),
        "epochs_with_satellites": epochs_with_satellites,
        "max_satellite_count_per_epoch": max_sat_count,
        "common_observation_types": obs_types,
        "per_observation_non_null": per_obs_non_null,
        "weights_reference": seed.get("weights_reference", {}),
        "min_bases_per_observation": seed.get("min_bases_per_observation"),
        "sample_epochs": epochs[:10],
    }
