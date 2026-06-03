from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

from app.services.hatanaka_service import convert_hatanaka_to_rinex
from app.services.rinex_service import parse_rinex_observation_metadata
from app.services.coordinates_service import (
    parse_approx_xyz,
    ecef_to_geodetic,
    geodetic_distance,
)
from app.services.vrs_window_service import get_common_time_window
from zoneinfo import ZoneInfo

LOCAL_TIMEZONE = "America/Belem"
MIN_VALID_BASES = 3


def _load_selected_station_codes_from_job(job) -> list[str]:
    raw = getattr(job, "selected_station_codes_json", None)
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    cleaned = []
    seen = set()

    for item in parsed:
        if item is None:
            continue

        code = str(item).strip().upper()
        if not code or code in seen:
            continue

        seen.add(code)
        cleaned.append(code)

    return cleaned


def _resolve_rbmc_station_observation_path(
    station_code: str,
    session_date: str,
) -> Path | None:
    storage_root = Path(__file__).resolve().parents[2] / "storage"
    station_root = (
        storage_root / "rbmc" / station_code.lower() / session_date / "converted"
    )

    if not station_root.exists():
        return None

    candidates = []
    for path in station_root.rglob("*"):
        if not path.is_file():
            continue

        lower = path.name.lower()
        if (
            lower.endswith(".obs")
            or lower.endswith(".rnx")
            or lower.endswith(".o")
            or lower.endswith(".d")
            or lower.endswith(".26o")
            or lower.endswith(".26d")
            or lower.endswith(".25o")
            or lower.endswith(".25d")
        ):
            candidates.append(path)

    if not candidates:
        return None

    candidates.sort(key=lambda p: p.name.lower())
    return candidates[0]


def _resolve_candidate_base_paths(job) -> list[dict]:
    selected_station_codes = _load_selected_station_codes_from_job(job)
    candidates: list[dict] = []

    for code in selected_station_codes:
        path = _resolve_rbmc_station_observation_path(code, job.session_date)
        if path is None:
            continue

        candidates.append(
            {
                "station_code": code,
                "label": f"rbmc_{code}",
                "path": path,
                "source_type": "rbmc_map_selection",
            }
        )

    return candidates


def _extract_station_meta(
    file_path: Path, label: str, station_code: str | None = None
) -> dict:
    metadata = parse_rinex_observation_metadata(str(file_path))

    xyz_text = metadata.get("approx_position_text")
    x, y, z = parse_approx_xyz(xyz_text)
    lat, lon, h = ecef_to_geodetic(x, y, z)

    return {
        "label": label,
        "station_code": station_code,
        "file_path": str(file_path),
        "file_name": file_path.name,
        "marker_name": metadata.get("marker_name"),
        "rinex_version": metadata.get("rinex_version"),
        "approx_position_text": xyz_text,
        "xyz": {"x": x, "y": y, "z": z},
        "geodetic": {"lat": lat, "lon": lon, "height": h},
        "interval_seconds": metadata.get("interval_seconds"),
        "first_epoch_text": metadata.get("first_epoch_text"),
        "last_epoch_text": metadata.get("last_epoch_text"),
        "line_count": metadata.get("line_count"),
        "data_line_count": metadata.get("data_line_count"),
    }


def _score_candidate_base(
    base_meta: dict,
    target_lat: float,
    target_lon: float,
    requested_start_utc: datetime,
    requested_end_utc: datetime,
) -> dict:
    distance_m = geodetic_distance(
        target_lat,
        target_lon,
        base_meta["geodetic"]["lat"],
        base_meta["geodetic"]["lon"],
    )

    first_text = base_meta.get("first_epoch_text")
    last_text = base_meta.get("last_epoch_text")

    coverage_ok = True
    try:
        from app.services.vrs_window_service import get_file_time_window

        window = get_file_time_window(base_meta["file_path"])
        first_dt = window["start"]
        last_dt = window["end"]

        if first_dt and requested_start_utc < first_dt:
            coverage_ok = False
        if last_dt and requested_end_utc > last_dt:
            coverage_ok = False
    except Exception:
        coverage_ok = False

    interval = base_meta.get("interval_seconds") or 999999.0

    score = 0.0
    score += 1000.0 if coverage_ok else 0.0
    score += max(0.0, 500000.0 / max(distance_m, 1.0))
    score += max(0.0, 100.0 / max(interval, 1.0))

    return {
        "distance_m": distance_m,
        "coverage_ok": coverage_ok,
        "interval_seconds": interval,
        "score": score,
        "first_epoch_text": first_text,
        "last_epoch_text": last_text,
    }


def _select_best_base_set(
    candidate_metas: list[dict],
    target_lat: float,
    target_lon: float,
    requested_start_utc: datetime,
    requested_end_utc: datetime,
) -> tuple[list[dict], list[dict]]:
    scored = []

    for meta in candidate_metas:
        metrics = _score_candidate_base(
            meta,
            target_lat,
            target_lon,
            requested_start_utc,
            requested_end_utc,
        )
        enriched = dict(meta)
        enriched.update(metrics)
        scored.append(enriched)

    valid = [item for item in scored if item.get("coverage_ok")]
    invalid = [item for item in scored if not item.get("coverage_ok")]

    valid.sort(
        key=lambda item: (
            -item["score"],
            item["distance_m"],
            item["interval_seconds"],
        )
    )

    return valid, invalid


def _build_requested_window_utc(job) -> tuple[datetime, datetime]:
    if not job.session_date or not job.start_time or not job.end_time:
        raise ValueError(
            "Data da sessão, hora inicial e hora final são obrigatórias para validação."
        )

    tz = ZoneInfo(LOCAL_TIMEZONE)

    start_local = datetime.fromisoformat(f"{job.session_date}T{job.start_time}:00")
    end_local = datetime.fromisoformat(f"{job.session_date}T{job.end_time}:00")

    if end_local <= start_local:
        raise ValueError("A hora final deve ser maior que a hora inicial.")

    start_local = start_local.replace(tzinfo=tz)
    end_local = end_local.replace(tzinfo=tz)

    requested_duration_min = int((end_local - start_local).total_seconds() / 60)

    if job.occupation_minutes is not None and requested_duration_min < int(
        job.occupation_minutes
    ):
        raise ValueError(
            "A janela informada é menor que o tempo de ocupação solicitado."
        )

    if job.requested_minutes and requested_duration_min < int(job.requested_minutes):
        raise ValueError("A janela informada é menor que os minutos solicitados.")

    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    return start_utc, end_utc


def validate_network(job) -> tuple[str, str]:
    try:
        if (
            job.target_lat is None
            or job.target_lon is None
            or job.target_height is None
        ):
            raise ValueError(
                "Coordenadas alvo VRS incompletas. Informe latitude, longitude e altitude."
            )

        requested_start_utc, requested_end_utc = _build_requested_window_utc(job)

        candidate_paths = _resolve_candidate_base_paths(job)
        if not candidate_paths:
            raise ValueError(
                "Nenhuma base RBMC foi localizada para as estações selecionadas."
            )

        candidate_metas = []
        discarded_at_read = []

        for item in candidate_paths:
            try:
                converted_path = Path(convert_hatanaka_to_rinex(str(item["path"])))
                meta = _extract_station_meta(
                    converted_path,
                    label=item["label"],
                    station_code=item.get("station_code"),
                )
                meta["source_type"] = item.get("source_type")
                meta["original_path"] = str(item["path"])
                meta["converted_path"] = str(converted_path)
                candidate_metas.append(meta)
            except Exception as exc:
                discarded_at_read.append(
                    f"{item.get('station_code') or item.get('label')}: {exc}"
                )

        if not candidate_metas:
            raise ValueError(
                "Nenhuma base pôde ser lida após download/conversão. "
                + " | ".join(discarded_at_read[:10])
            )

        valid_bases, invalid_bases = _select_best_base_set(
            candidate_metas,
            job.target_lat,
            job.target_lon,
            requested_start_utc,
            requested_end_utc,
        )

        if len(valid_bases) < MIN_VALID_BASES:
            invalid_msgs = []
            for item in invalid_bases[:10]:
                invalid_msgs.append(
                    f"{item.get('station_code') or item.get('file_name')} "
                    f"[first={item.get('first_epoch_text')}, last={item.get('last_epoch_text')}]"
                )

            raise ValueError(
                f"Não foi possível obter ao menos {MIN_VALID_BASES} bases RBMC válidas para a data da sessão. "
                f"Válidas: {len(valid_bases)}. "
                f"Descartadas: {len(invalid_bases) + len(discarded_at_read)}. "
                + (" | ".join(invalid_msgs) if invalid_msgs else "")
            )

        selected_bases = valid_bases[: max(MIN_VALID_BASES, min(len(valid_bases), 6))]
        base_paths = [item["converted_path"] for item in selected_bases]

        common_window = get_common_time_window(base_paths)

        common_start = common_window["common_start"]
        common_end = common_window["common_end"]
        duration_seconds = common_window["duration_seconds"]
        duration_minutes = int(duration_seconds / 60)

        effective_start_utc = max(common_start, requested_start_utc)
        effective_end_utc = min(common_end, requested_end_utc)

        if effective_end_utc <= effective_start_utc:
            raise ValueError(
                "As bases válidas existem, mas não possuem interseção temporal útil para a janela solicitada."
            )

        effective_duration_minutes = int(
            (effective_end_utc - effective_start_utc).total_seconds() / 60
        )

        if job.occupation_minutes is not None and effective_duration_minutes < int(
            job.occupation_minutes
        ):
            raise ValueError(
                "A janela efetiva disponível entre as bases válidas é menor que o tempo de ocupação solicitado."
            )

        if job.requested_minutes and effective_duration_minutes < int(
            job.requested_minutes
        ):
            raise ValueError(
                "A janela efetiva disponível entre as bases válidas é menor que os minutos solicitados."
            )

        selected_codes = []
        for base in selected_bases:
            code = (
                base.get("station_code")
                or base.get("marker_name")
                or base.get("file_name")
            )
            selected_codes.append(str(code))

        discarded_codes = []
        for base in invalid_bases:
            code = (
                base.get("station_code")
                or base.get("marker_name")
                or base.get("file_name")
            )
            discarded_codes.append(str(code))

        if discarded_at_read:
            discarded_codes.extend(discarded_at_read)

        if discarded_codes:
            summary = (
                "Rede validada com ressalvas. "
                f"Bases válidas usadas: {', '.join(selected_codes)}. "
                f"Janela comum UTC: {common_start.isoformat()} até {common_end.isoformat()}. "
                f"Duração comum: {duration_minutes} min. "
                f"Janela efetiva: {effective_start_utc.isoformat()} até {effective_end_utc.isoformat()} "
                f"({effective_duration_minutes} min). "
                f"Descartadas automaticamente: {', '.join(discarded_codes[:12])}."
            )
        else:
            summary = (
                "Rede validada com sucesso. "
                f"Bases válidas usadas: {', '.join(selected_codes)}. "
                f"Janela comum UTC: {common_start.isoformat()} até {common_end.isoformat()}. "
                f"Duração comum: {duration_minutes} min. "
                f"Janela efetiva: {effective_start_utc.isoformat()} até {effective_end_utc.isoformat()} "
                f"({effective_duration_minutes} min)."
            )

        return "completed", summary

    except Exception as exc:
        return "failed", str(exc)
