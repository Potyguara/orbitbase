from __future__ import annotations

from datetime import datetime
import math
from collections import defaultdict

import json
from pathlib import Path


def write_ppp_observation_diagnostics(output_path: str, diagnostics: dict) -> str:
    path = Path(output_path)
    path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


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


def _extract_epoch_datetime(epoch_iso: str) -> datetime:
    return datetime.fromisoformat(epoch_iso)


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


def _rms(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if not usable:
        return 0.0
    return math.sqrt(sum(v * v for v in usable) / len(usable))


def _std(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if len(usable) < 2:
        return 0.0
    mean = sum(usable) / len(usable)
    return math.sqrt(sum((v - mean) ** 2 for v in usable) / len(usable))


def _compute_jump_series(values: list[float | None]) -> list[float]:
    jumps: list[float] = []
    prev = None

    for value in values:
        curr = _safe_float(value)
        if curr is None:
            prev = None
            continue

        if prev is not None:
            jumps.append(curr - prev)

        prev = curr

    return jumps


def _count_epoch_gaps(indexes: list[int]) -> tuple[int, int]:
    if not indexes:
        return 0, 0

    gap_count = 0
    max_gap = 0

    for i in range(1, len(indexes)):
        gap = indexes[i] - indexes[i - 1] - 1
        if gap > 0:
            gap_count += 1
            max_gap = max(max_gap, gap)

    return gap_count, max_gap


def _compute_longest_consecutive_arc(indexes: list[int]) -> int:
    if not indexes:
        return 0

    longest = 1
    current = 1

    for i in range(1, len(indexes)):
        if indexes[i] == indexes[i - 1] + 1:
            current += 1
        else:
            longest = max(longest, current)
            current = 1

    return max(longest, current)


def _build_satellite_epoch_series(seed: dict) -> dict[str, dict]:
    epochs = seed.get("epochs", []) or []
    sat_map: dict[str, dict] = defaultdict(
        lambda: {
            "epoch_indexes": [],
            "epochs": [],
            "C1": [],
            "P1": [],
            "L1": [],
            "C2": [],
            "P2": [],
            "L2": [],
            "S1": [],
            "S2": [],
        }
    )

    for epoch_idx, epoch in enumerate(epochs):
        sats = epoch.get("satellites", []) or []

        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if not sat_id or _get_satellite_system(sat_id) != "G":
                continue

            obs = sat.get("observations", {}) or {}
            entry = sat_map[sat_id]
            entry["epoch_indexes"].append(epoch_idx)
            entry["epochs"].append(epoch.get("epoch"))

            for obs_name in ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]:
                entry[obs_name].append(_safe_float(obs.get(obs_name)))

    return sat_map


def _score_code_phase_consistency(metrics: dict) -> float:
    c1 = metrics.get("c1_jump_rms", 0.0)
    l1 = metrics.get("l1_jump_rms", 0.0)
    l2 = metrics.get("l2_jump_rms", 0.0)

    denom = c1 + l1 + l2
    if denom <= 0:
        return 1.0

    ratio = c1 / denom
    score = 1.0 - ratio
    return max(0.0, min(1.0, score))


def analyze_ppp_satellite_observations(
    seed: dict,
    priority_obs: list[str] | None = None,
) -> list[dict]:
    priority_obs = priority_obs or ["C1", "P1", "L1", "C2", "P2", "L2"]

    epochs = seed.get("epochs", []) or []
    total_epochs = len(epochs)
    interval_seconds = _estimate_interval_seconds(epochs)

    if not epochs:
        return []

    sat_series = _build_satellite_epoch_series(seed)
    results: list[dict] = []

    for sat_id, data in sat_series.items():
        indexes = data["epoch_indexes"]
        presence_epochs = len(indexes)
        presence_ratio = presence_epochs / total_epochs if total_epochs > 0 else 0.0

        longest_arc_epochs = _compute_longest_consecutive_arc(indexes)
        longest_arc_minutes = (longest_arc_epochs * interval_seconds) / 60.0

        gap_count, max_gap_epochs = _count_epoch_gaps(indexes)
        max_gap_seconds = max_gap_epochs * interval_seconds

        c1_jumps = _compute_jump_series(data["C1"])
        p1_jumps = _compute_jump_series(data["P1"])
        l1_jumps = _compute_jump_series(data["L1"])
        c2_jumps = _compute_jump_series(data["C2"])
        p2_jumps = _compute_jump_series(data["P2"])
        l2_jumps = _compute_jump_series(data["L2"])

        metrics = {
            "satellite": sat_id,
            "presence_epochs": presence_epochs,
            "presence_ratio": round(presence_ratio, 6),
            "longest_arc_epochs": longest_arc_epochs,
            "longest_arc_minutes": round(longest_arc_minutes, 3),
            "gap_count": gap_count,
            "max_gap_epochs": max_gap_epochs,
            "max_gap_seconds": round(max_gap_seconds, 3),
            "epochs_with_C1": sum(1 for v in data["C1"] if v is not None),
            "epochs_with_P1": sum(1 for v in data["P1"] if v is not None),
            "epochs_with_L1": sum(1 for v in data["L1"] if v is not None),
            "epochs_with_C2": sum(1 for v in data["C2"] if v is not None),
            "epochs_with_P2": sum(1 for v in data["P2"] if v is not None),
            "epochs_with_L2": sum(1 for v in data["L2"] if v is not None),
            "epochs_with_S1": sum(1 for v in data["S1"] if v is not None),
            "epochs_with_S2": sum(1 for v in data["S2"] if v is not None),
            "c1_jump_rms": round(_rms(c1_jumps), 6),
            "p1_jump_rms": round(_rms(p1_jumps), 6),
            "l1_jump_rms": round(_rms(l1_jumps), 6),
            "c2_jump_rms": round(_rms(c2_jumps), 6),
            "p2_jump_rms": round(_rms(p2_jumps), 6),
            "l2_jump_rms": round(_rms(l2_jumps), 6),
            "c1_jump_std": round(_std(c1_jumps), 6),
            "l1_jump_std": round(_std(l1_jumps), 6),
            "l2_jump_std": round(_std(l2_jumps), 6),
        }

        metrics["code_phase_consistency_score"] = round(
            _score_code_phase_consistency(metrics),
            6,
        )

        results.append(metrics)

    results.sort(key=lambda item: item["satellite"])
    return results


def select_best_ppp_continuous_block(
    seed: dict,
    min_satellites_per_epoch: int = 5,
) -> dict:
    epochs = seed.get("epochs", []) or []
    if not epochs:
        return {
            "start_epoch": None,
            "end_epoch": None,
            "start_index": None,
            "end_index": None,
            "length_epochs": 0,
            "length_minutes": 0.0,
            "avg_satellites": 0.0,
            "reason": "Seed sem épocas.",
        }

    interval_seconds = _estimate_interval_seconds(epochs)

    valid_indexes = [
        idx
        for idx, epoch in enumerate(epochs)
        if len(epoch.get("satellites", []) or []) >= min_satellites_per_epoch
    ]

    if not valid_indexes:
        return {
            "start_epoch": None,
            "end_epoch": None,
            "start_index": None,
            "end_index": None,
            "length_epochs": 0,
            "length_minutes": 0.0,
            "avg_satellites": 0.0,
            "reason": "Nenhuma época atende ao mínimo de satélites.",
        }

    best_start = valid_indexes[0]
    best_end = valid_indexes[0]
    current_start = valid_indexes[0]
    current_end = valid_indexes[0]

    for idx in valid_indexes[1:]:
        if idx == current_end + 1:
            current_end = idx
        else:
            if (current_end - current_start) > (best_end - best_start):
                best_start, best_end = current_start, current_end
            current_start = idx
            current_end = idx

    if (current_end - current_start) > (best_end - best_start):
        best_start, best_end = current_start, current_end

    block = epochs[best_start : best_end + 1]
    avg_satellites = (
        sum(len(epoch.get("satellites", []) or []) for epoch in block) / len(block)
        if block
        else 0.0
    )

    return {
        "start_epoch": block[0]["epoch"] if block else None,
        "end_epoch": block[-1]["epoch"] if block else None,
        "start_index": best_start,
        "end_index": best_end,
        "length_epochs": len(block),
        "length_minutes": round((len(block) * interval_seconds) / 60.0, 3),
        "avg_satellites": round(avg_satellites, 3),
        "reason": "Melhor bloco contínuo global com base em satélites por época.",
    }


def generate_ppp_observation_diagnostics(
    seed: dict,
    priority_obs: list[str] | None = None,
    min_satellites_per_epoch: int = 5,
) -> dict:
    epochs = seed.get("epochs", []) or []
    interval_seconds = _estimate_interval_seconds(epochs)
    sat_metrics = analyze_ppp_satellite_observations(seed, priority_obs=priority_obs)
    best_block = select_best_ppp_continuous_block(
        seed,
        min_satellites_per_epoch=min_satellites_per_epoch,
    )

    dual_frequency_epochs = 0
    avg_satellites_per_epoch = 0.0

    if epochs:
        avg_satellites_per_epoch = sum(
            len(epoch.get("satellites", []) or []) for epoch in epochs
        ) / len(epochs)

        for epoch in epochs:
            sats = epoch.get("satellites", []) or []
            valid_count = 0
            for sat in sats:
                obs = sat.get("observations", {}) or {}
                if _has_required_dual_frequency(obs):
                    valid_count += 1
            if valid_count >= min_satellites_per_epoch:
                dual_frequency_epochs += 1

    summary = {
        "processing_mode": seed.get("processing_mode"),
        "method": seed.get("method"),
        "generated_epoch_count": len(epochs),
        "interval_seconds": float(interval_seconds),
        "avg_satellites_per_epoch": round(avg_satellites_per_epoch, 6),
        "min_satellites_per_epoch": int(min_satellites_per_epoch),
        "epochs_with_dual_frequency_support": int(dual_frequency_epochs),
        "epochs_with_dual_frequency_support_ratio": (
            round(dual_frequency_epochs / len(epochs), 6) if epochs else 0.0
        ),
        "best_continuous_block": best_block,
        "satellite_count": len(sat_metrics),
    }

    rankings = {
        "worst_by_presence": sorted(
            sat_metrics,
            key=lambda item: (
                item.get("presence_ratio", 0.0),
                item.get("longest_arc_epochs", 0),
            ),
        )[:10],
        "worst_by_l1_jump": sorted(
            sat_metrics,
            key=lambda item: item.get("l1_jump_rms", 0.0),
            reverse=True,
        )[:10],
        "worst_by_l2_jump": sorted(
            sat_metrics,
            key=lambda item: item.get("l2_jump_rms", 0.0),
            reverse=True,
        )[:10],
        "worst_by_code_phase_consistency": sorted(
            sat_metrics,
            key=lambda item: item.get("code_phase_consistency_score", 0.0),
            reverse=False,
        )[:10],
    }

    return {
        "summary": summary,
        "rankings": rankings,
        "satellite_metrics": sat_metrics,
    }


def filter_ppp_seed_by_quality(
    seed: dict,
    diagnostics: dict,
    min_presence_ratio: float = 0.70,
    min_longest_arc_epochs: int = 300,
    max_gap_epochs: int = 8,
    max_c1_jump_rms: float = 1500.0,
    max_l1_jump_rms: float = 5000.0,
    max_l2_jump_rms: float = 5000.0,
    min_code_phase_consistency_score: float = 0.60,
    min_satellites_per_epoch: int = 5,
) -> dict:
    sat_metrics = diagnostics.get("satellite_metrics", []) or []
    sat_metric_map = {item["satellite"]: item for item in sat_metrics}

    approved_satellites: list[str] = []
    rejected_satellites: list[dict] = []

    for sat_id, metric in sat_metric_map.items():
        reasons = []

        if metric.get("presence_ratio", 0.0) < min_presence_ratio:
            reasons.append("low_presence_ratio")

        if metric.get("longest_arc_epochs", 0) < min_longest_arc_epochs:
            reasons.append("short_longest_arc")

        if metric.get("max_gap_epochs", 0) > max_gap_epochs:
            reasons.append("gap_too_large")

        if metric.get("c1_jump_rms", 0.0) > max_c1_jump_rms:
            reasons.append("code_jump_l1")

        if metric.get("l1_jump_rms", 0.0) > max_l1_jump_rms:
            reasons.append("phase_jump_l1")

        if metric.get("l2_jump_rms", 0.0) > max_l2_jump_rms:
            reasons.append("phase_jump_l2")

        if (
            metric.get("code_phase_consistency_score", 0.0)
            < min_code_phase_consistency_score
        ):
            reasons.append("dual_phase_inconsistency")

        if reasons:
            rejected_satellites.append(
                {
                    "satellite": sat_id,
                    "reasons": reasons,
                    "metrics": metric,
                }
            )
        else:
            approved_satellites.append(sat_id)

    filtered_epochs = []
    removed_epochs = 0
    removed_satellite_instances = 0

    for epoch in seed.get("epochs", []) or []:
        sats = epoch.get("satellites", []) or []
        kept_sats = []

        for sat in sats:
            sat_id = _normalize_satellite_id(sat.get("satellite"))
            if sat_id not in approved_satellites:
                removed_satellite_instances += 1
                continue
            kept_sats.append({**sat, "satellite": sat_id})

        kept_sats.sort(key=lambda item: item.get("satellite", ""))

        if len(kept_sats) >= min_satellites_per_epoch:
            filtered_epochs.append(
                {
                    **epoch,
                    "satellites": kept_sats,
                    "generated_satellite_count": len(kept_sats),
                }
            )
        else:
            removed_epochs += 1

    cloned = dict(seed)
    cloned["epochs"] = filtered_epochs
    cloned["generated_epoch_count"] = len(filtered_epochs)
    cloned["systems"] = ["G"] if filtered_epochs else []

    cloned["ppp_quality_filter"] = {
        "approved_satellites": approved_satellites,
        "rejected_satellites": rejected_satellites,
        "removed_epochs": removed_epochs,
        "removed_satellite_instances": removed_satellite_instances,
        "criteria": {
            "min_presence_ratio": min_presence_ratio,
            "min_longest_arc_epochs": min_longest_arc_epochs,
            "max_gap_epochs": max_gap_epochs,
            "max_c1_jump_rms": max_c1_jump_rms,
            "max_l1_jump_rms": max_l1_jump_rms,
            "max_l2_jump_rms": max_l2_jump_rms,
            "min_code_phase_consistency_score": min_code_phase_consistency_score,
            "min_satellites_per_epoch": min_satellites_per_epoch,
        },
    }

    return cloned
