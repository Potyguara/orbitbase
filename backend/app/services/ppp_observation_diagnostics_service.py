from __future__ import annotations

from datetime import datetime
from pathlib import Path
from collections import defaultdict
import json
import math


GPS_ONLY_OBS = ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _extract_epoch_datetime(epoch_iso: str) -> datetime:
    return datetime.fromisoformat(epoch_iso)


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


def _estimate_interval_seconds(epochs: list[dict]) -> float:
    if len(epochs) < 2:
        return 15.0

    try:
        dt1 = _extract_epoch_datetime(epochs[0]["epoch"])
        dt2 = _extract_epoch_datetime(epochs[1]["epoch"])
        delta = (dt2 - dt1).total_seconds()
        if delta > 0:
            return float(delta)
    except Exception:
        pass

    return 15.0


def _rms(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if not usable:
        return 0.0
    return math.sqrt(sum(v * v for v in usable) / len(usable))


def _mean(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if not usable:
        return 0.0
    return sum(usable) / len(usable)


def _std(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if len(usable) < 2:
        return 0.0
    m = _mean(usable)
    return math.sqrt(sum((v - m) ** 2 for v in usable) / len(usable))


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


def _priority_count(obs: dict, priority_obs: list[str]) -> int:
    return sum(
        1
        for obs_name in priority_obs
        if _safe_float((obs or {}).get(obs_name)) is not None
    )


def _continuous_runs(
    indexes: list[int], max_gap_epochs: int = 1
) -> list[tuple[int, int]]:
    if not indexes:
        return []

    runs = []
    start = indexes[0]
    prev = indexes[0]

    for idx in indexes[1:]:
        if idx - prev <= max_gap_epochs:
            prev = idx
            continue

        runs.append((start, prev))
        start = idx
        prev = idx

    runs.append((start, prev))
    return runs


def _longest_run_length(indexes: list[int], max_gap_epochs: int = 1) -> int:
    runs = _continuous_runs(indexes, max_gap_epochs=max_gap_epochs)
    if not runs:
        return 0
    return max((end - start + 1) for start, end in runs)


def _largest_gap_epochs(indexes: list[int]) -> int:
    if len(indexes) < 2:
        return 0

    largest = 0
    for i in range(1, len(indexes)):
        gap = indexes[i] - indexes[i - 1] - 1
        if gap > largest:
            largest = gap
    return largest


def _first_differences(series: list[tuple[int, float]]) -> list[float]:
    if len(series) < 2:
        return []

    diffs = []
    for i in range(1, len(series)):
        prev_idx, prev_val = series[i - 1]
        curr_idx, curr_val = series[i]
        if curr_idx == prev_idx + 1:
            diffs.append(curr_val - prev_val)
    return diffs


def _collect_series_by_obs(seed: dict) -> dict[str, dict[str, list[tuple[int, float]]]]:
    """
    Retorna:
      {
        "G01": {
          "C1": [(epoch_idx, value), ...],
          "L1": [...],
          ...
        },
        ...
      }
    """
    sat_obs_series: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    epochs = seed.get("epochs", []) or []
    for epoch_idx, epoch in enumerate(epochs):
        sats = epoch.get("satellites", []) or []
        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if _get_satellite_system(sat_id) != "G":
                continue

            obs = sat.get("observations", {}) or {}
            for obs_name in GPS_ONLY_OBS:
                value = _safe_float(obs.get(obs_name))
                if value is not None:
                    sat_obs_series[sat_id][obs_name].append((epoch_idx, value))

    return sat_obs_series


def _collect_satellite_epoch_indexes(seed: dict) -> dict[str, list[int]]:
    sat_epochs: dict[str, list[int]] = defaultdict(list)

    epochs = seed.get("epochs", []) or []
    for epoch_idx, epoch in enumerate(epochs):
        sats = epoch.get("satellites", []) or []
        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if _get_satellite_system(sat_id) != "G":
                continue
            sat_epochs[sat_id].append(epoch_idx)

    return sat_epochs


def _find_best_global_continuous_block(
    seed: dict,
    min_satellites_per_epoch: int = 5,
    max_gap_factor: float = 2.0,
) -> dict:
    epochs = seed.get("epochs", []) or []
    if not epochs:
        return {
            "start_epoch": None,
            "end_epoch": None,
            "start_index": -1,
            "end_index": -1,
            "length_epochs": 0,
            "length_minutes": 0.0,
            "avg_satellites": 0.0,
            "reason": "Sem épocas.",
        }

    interval_seconds = _estimate_interval_seconds(epochs)
    max_gap_seconds = interval_seconds * max_gap_factor

    valid_indexes = []
    sat_counts = []

    for idx, epoch in enumerate(epochs):
        sat_count = len(epoch.get("satellites", []) or [])
        sat_counts.append(sat_count)
        if sat_count >= min_satellites_per_epoch:
            valid_indexes.append(idx)

    if not valid_indexes:
        return {
            "start_epoch": None,
            "end_epoch": None,
            "start_index": -1,
            "end_index": -1,
            "length_epochs": 0,
            "length_minutes": 0.0,
            "avg_satellites": 0.0,
            "reason": "Nenhuma época com satélites suficientes.",
        }

    best_start = valid_indexes[0]
    best_end = valid_indexes[0]
    cur_start = valid_indexes[0]
    cur_end = valid_indexes[0]

    for i in range(1, len(valid_indexes)):
        prev_idx = valid_indexes[i - 1]
        curr_idx = valid_indexes[i]

        prev_dt = _extract_epoch_datetime(epochs[prev_idx]["epoch"])
        curr_dt = _extract_epoch_datetime(epochs[curr_idx]["epoch"])
        dt_gap = (curr_dt - prev_dt).total_seconds()

        if dt_gap <= max_gap_seconds:
            cur_end = curr_idx
        else:
            if (cur_end - cur_start) > (best_end - best_start):
                best_start, best_end = cur_start, cur_end
            cur_start = curr_idx
            cur_end = curr_idx

    if (cur_end - cur_start) > (best_end - best_start):
        best_start, best_end = cur_start, cur_end

    block_sat_counts = sat_counts[best_start : best_end + 1]
    length_epochs = best_end - best_start + 1
    length_minutes = (length_epochs * interval_seconds) / 60.0

    return {
        "start_epoch": epochs[best_start]["epoch"],
        "end_epoch": epochs[best_end]["epoch"],
        "start_index": best_start,
        "end_index": best_end,
        "length_epochs": length_epochs,
        "length_minutes": round(length_minutes, 3),
        "avg_satellites": round(_mean(block_sat_counts), 3),
        "reason": "Melhor bloco contínuo global com base em satélites por época.",
    }


def generate_ppp_observation_diagnostics(
    seed: dict,
    priority_obs: list[str] | None = None,
    min_satellites_per_epoch: int = 5,
) -> dict:
    priority_obs = priority_obs or ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]

    epochs = seed.get("epochs", []) or []
    interval_seconds = _estimate_interval_seconds(epochs)
    total_epochs = len(epochs)

    sat_epoch_indexes = _collect_satellite_epoch_indexes(seed)
    sat_obs_series = _collect_series_by_obs(seed)

    satellites_report = []

    for sat_id in sorted(sat_epoch_indexes.keys()):
        indexes = sat_epoch_indexes[sat_id]
        presence_ratio = (len(indexes) / total_epochs) if total_epochs > 0 else 0.0
        longest_arc_epochs = _longest_run_length(indexes, max_gap_epochs=1)
        longest_arc_minutes = (longest_arc_epochs * interval_seconds) / 60.0
        gap_count = max(0, len(_continuous_runs(indexes, max_gap_epochs=1)) - 1)
        max_gap_epochs = _largest_gap_epochs(indexes)
        max_gap_seconds = max_gap_epochs * interval_seconds

        series_by_obs = sat_obs_series.get(sat_id, {})

        obs_counts = {}
        jump_rms = {}
        jump_std = {}

        for obs_name in GPS_ONLY_OBS:
            series = series_by_obs.get(obs_name, [])
            obs_counts[obs_name] = len(series)

            diffs = _first_differences(series)
            jump_rms[obs_name] = round(_rms(diffs), 6) if diffs else 0.0
            jump_std[obs_name] = round(_std(diffs), 6) if diffs else 0.0

        # indicador simples de coerência código/fase
        code_jump = _mean(
            [
                jump_rms.get("C1", 0.0),
                jump_rms.get("P1", 0.0),
                jump_rms.get("C2", 0.0),
                jump_rms.get("P2", 0.0),
            ]
        )
        phase_jump = _mean(
            [
                jump_rms.get("L1", 0.0),
                jump_rms.get("L2", 0.0),
            ]
        )

        if phase_jump <= 0:
            code_phase_consistency_score = 0.0
        else:
            code_phase_consistency_score = round(code_jump / phase_jump, 6)

        satellites_report.append(
            {
                "satellite": sat_id,
                "presence_epochs": len(indexes),
                "presence_ratio": round(presence_ratio, 6),
                "longest_arc_epochs": int(longest_arc_epochs),
                "longest_arc_minutes": round(longest_arc_minutes, 3),
                "gap_count": int(gap_count),
                "max_gap_epochs": int(max_gap_epochs),
                "max_gap_seconds": round(max_gap_seconds, 3),
                "epochs_with_C1": obs_counts.get("C1", 0),
                "epochs_with_P1": obs_counts.get("P1", 0),
                "epochs_with_L1": obs_counts.get("L1", 0),
                "epochs_with_C2": obs_counts.get("C2", 0),
                "epochs_with_P2": obs_counts.get("P2", 0),
                "epochs_with_L2": obs_counts.get("L2", 0),
                "epochs_with_S1": obs_counts.get("S1", 0),
                "epochs_with_S2": obs_counts.get("S2", 0),
                "c1_jump_rms": jump_rms.get("C1", 0.0),
                "p1_jump_rms": jump_rms.get("P1", 0.0),
                "l1_jump_rms": jump_rms.get("L1", 0.0),
                "c2_jump_rms": jump_rms.get("C2", 0.0),
                "p2_jump_rms": jump_rms.get("P2", 0.0),
                "l2_jump_rms": jump_rms.get("L2", 0.0),
                "c1_jump_std": jump_std.get("C1", 0.0),
                "l1_jump_std": jump_std.get("L1", 0.0),
                "l2_jump_std": jump_std.get("L2", 0.0),
                "code_phase_consistency_score": code_phase_consistency_score,
            }
        )

    best_block = _find_best_global_continuous_block(
        seed,
        min_satellites_per_epoch=min_satellites_per_epoch,
        max_gap_factor=2.0,
    )

    epochs_with_dual_frequency = 0
    epoch_satellite_counts = []

    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        epoch_satellite_counts.append(len(sats))

        dual_ok = 0
        for sat in sats:
            obs = sat.get("observations", {}) or {}
            if _has_required_dual_frequency(obs):
                prio_count = _priority_count(obs, priority_obs)
                if prio_count >= 4:
                    dual_ok += 1

        if dual_ok >= min_satellites_per_epoch:
            epochs_with_dual_frequency += 1

    summary = {
        "processing_mode": seed.get("processing_mode"),
        "method": seed.get("method"),
        "generated_epoch_count": total_epochs,
        "interval_seconds": round(interval_seconds, 3),
        "avg_satellites_per_epoch": (
            round(_mean(epoch_satellite_counts), 6) if epoch_satellite_counts else 0.0
        ),
        "min_satellites_per_epoch": min_satellites_per_epoch,
        "epochs_with_dual_frequency_support": int(epochs_with_dual_frequency),
        "epochs_with_dual_frequency_support_ratio": round(
            (epochs_with_dual_frequency / total_epochs) if total_epochs > 0 else 0.0,
            6,
        ),
        "best_continuous_block": best_block,
        "satellite_count": len(satellites_report),
    }

    worst_by_presence = sorted(
        satellites_report,
        key=lambda item: (item["presence_ratio"], item["longest_arc_epochs"]),
    )[:10]

    worst_by_l1_jump = sorted(
        satellites_report,
        key=lambda item: item["l1_jump_rms"],
        reverse=True,
    )[:10]

    worst_by_l2_jump = sorted(
        satellites_report,
        key=lambda item: item["l2_jump_rms"],
        reverse=True,
    )[:10]

    worst_by_code_phase = sorted(
        satellites_report,
        key=lambda item: item["code_phase_consistency_score"],
        reverse=True,
    )[:10]

    return {
        "summary": summary,
        "satellites": satellites_report,
        "rankings": {
            "worst_by_presence": worst_by_presence,
            "worst_by_l1_jump": worst_by_l1_jump,
            "worst_by_l2_jump": worst_by_l2_jump,
            "worst_by_code_phase_consistency": worst_by_code_phase,
        },
    }


def write_ppp_observation_diagnostics(
    output_path: str | Path,
    diagnostics: dict,
) -> str:
    path = Path(output_path)
    path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)
