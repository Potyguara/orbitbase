from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Any
import json

GPS_ONLY = True

DEFAULT_PRIORITY_OBS = ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


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


def _json_default(value: Any):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_advanced_ppp_filter_diagnostics(
    output_path: str | Path, diagnostics: dict
) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    return str(path)


def _normalize_satellite_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if not text:
        return ""

    if len(text) == 3 and text[0].isalpha():
        try:
            return f"{text[0]}{int(text[1:]):02d}"
        except Exception:
            return text

    if len(text) == 2 and text[0].isalpha() and text[1].isdigit():
        return f"{text[0]}0{text[1]}"

    if text.isdigit():
        return f"G{int(text):02d}"

    return text


def _sat_system(sat: str) -> str:
    sat = _normalize_satellite_code(sat)
    if sat and sat[0].isalpha():
        return sat[0]
    return "G"


def _extract_epochs(seed: dict) -> list[dict]:
    epochs = seed.get("epochs")
    return epochs if isinstance(epochs, list) else []


def _extract_epoch_dt(epoch: dict) -> datetime | None:
    for key in ("epoch", "epoch_utc", "time", "timestamp", "datetime"):
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
    raw = sat_item.get("observations")
    if not isinstance(raw, dict):
        raw = {}

    out: dict[str, float | None] = {}
    for k, v in raw.items():
        kk = str(k).strip().upper()
        if not kk:
            continue
        out[kk] = _safe_float(v)
    return out


def _estimate_interval_seconds(epochs: list[dict]) -> float:
    dts = []
    for epoch in epochs[:5]:
        dt = _extract_epoch_dt(epoch)
        if dt is not None:
            dts.append(dt)

    if len(dts) < 2:
        return 1.0

    delta = (dts[1] - dts[0]).total_seconds()
    return delta if delta > 0 else 1.0


def _has_any(obs: dict[str, float | None], names: list[str]) -> bool:
    return any(obs.get(name) is not None for name in names)


def _has_code_dual_frequency(obs: dict[str, float | None]) -> bool:
    has_code_l1 = _has_any(obs, ["C1", "P1"])
    has_code_l2 = _has_any(obs, ["C2", "P2"])
    return has_code_l1 and has_code_l2


def _has_phase_dual_frequency(obs: dict[str, float | None]) -> bool:
    return obs.get("L1") is not None and obs.get("L2") is not None


def _jump_series(values: list[float | None]) -> list[float]:
    out: list[float] = []
    prev = None

    for v in values:
        curr = _safe_float(v)
        if curr is None:
            prev = None
            continue

        if prev is not None:
            out.append(curr - prev)

        prev = curr

    return out


def _rms(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if not usable:
        return 0.0
    return sqrt(sum(v * v for v in usable) / len(usable))


def _std(values: list[float]) -> float:
    usable = [float(v) for v in values if v is not None]
    if len(usable) < 2:
        return 0.0
    mean = sum(usable) / len(usable)
    return sqrt(sum((v - mean) ** 2 for v in usable) / len(usable))


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


def _longest_consecutive_arc(indexes: list[int]) -> int:
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
    epochs = _extract_epochs(seed)

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
        epoch_dt = _extract_epoch_dt(epoch)
        if epoch_dt is None:
            continue

        for sat_item in _extract_sat_items(epoch):
            sat_id = _extract_sat_code(sat_item)
            if not sat_id:
                continue

            if GPS_ONLY and _sat_system(sat_id) != "G":
                continue

            obs = _extract_obs_map(sat_item)
            if not obs:
                continue

            entry = sat_map[sat_id]
            entry["epoch_indexes"].append(epoch_idx)
            entry["epochs"].append(epoch_dt)

            for obs_name in DEFAULT_PRIORITY_OBS:
                entry.setdefault(obs_name, [])
                entry[obs_name].append(obs.get(obs_name))

    return sat_map


def analyze_advanced_ppp_satellites(
    seed: dict,
    prefer_phase_but_do_not_require: bool = True,
) -> list[dict]:
    epochs = _extract_epochs(seed)
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

        longest_arc_epochs = _longest_consecutive_arc(indexes)
        longest_arc_minutes = (longest_arc_epochs * interval_seconds) / 60.0

        gap_count, max_gap_epochs = _count_epoch_gaps(indexes)
        max_gap_seconds = max_gap_epochs * interval_seconds

        c1_jumps = _jump_series(data["C1"])
        p1_jumps = _jump_series(data["P1"])
        l1_jumps = _jump_series(data["L1"])
        c2_jumps = _jump_series(data["C2"])
        p2_jumps = _jump_series(data["P2"])
        l2_jumps = _jump_series(data["L2"])

        code_dual_freq_epochs = 0
        phase_dual_freq_epochs = 0

        for i in range(presence_epochs):
            obs_map = {
                "C1": data["C1"][i] if i < len(data["C1"]) else None,
                "P1": data["P1"][i] if i < len(data["P1"]) else None,
                "L1": data["L1"][i] if i < len(data["L1"]) else None,
                "C2": data["C2"][i] if i < len(data["C2"]) else None,
                "P2": data["P2"][i] if i < len(data["P2"]) else None,
                "L2": data["L2"][i] if i < len(data["L2"]) else None,
            }

            if _has_code_dual_frequency(obs_map):
                code_dual_freq_epochs += 1
            if _has_code_dual_frequency(obs_map) and _has_phase_dual_frequency(obs_map):
                phase_dual_freq_epochs += 1

        code_dual_freq_ratio = (
            code_dual_freq_epochs / presence_epochs if presence_epochs > 0 else 0.0
        )
        phase_dual_freq_ratio = (
            phase_dual_freq_epochs / presence_epochs if presence_epochs > 0 else 0.0
        )

        score = 0.0
        score += 45.0 * presence_ratio
        score += 30.0 * code_dual_freq_ratio
        score += 15.0 * phase_dual_freq_ratio
        score += 10.0 * min(1.0, longest_arc_epochs / max(1.0, total_epochs))
        score -= 2.0 * gap_count
        score -= 0.015 * _rms(c1_jumps)
        score -= 0.015 * _rms(c2_jumps)

        if not prefer_phase_but_do_not_require:
            score -= 0.000001 * _rms(l1_jumps)
            score -= 0.000001 * _rms(l2_jumps)

        metrics = {
            "satellite": sat_id,
            "presence_epochs": presence_epochs,
            "presence_ratio": round(presence_ratio, 6),
            "code_dual_freq_epochs": code_dual_freq_epochs,
            "code_dual_freq_ratio": round(code_dual_freq_ratio, 6),
            "phase_dual_freq_epochs": phase_dual_freq_epochs,
            "phase_dual_freq_ratio": round(phase_dual_freq_ratio, 6),
            "prefer_phase_but_do_not_require": prefer_phase_but_do_not_require,
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
            "c1_jump_rms": round(_rms(c1_jumps), 6),
            "p1_jump_rms": round(_rms(p1_jumps), 6),
            "l1_jump_rms": round(_rms(l1_jumps), 6),
            "c2_jump_rms": round(_rms(c2_jumps), 6),
            "p2_jump_rms": round(_rms(p2_jumps), 6),
            "l2_jump_rms": round(_rms(l2_jumps), 6),
            "c1_jump_std": round(_std(c1_jumps), 6),
            "p1_jump_std": round(_std(p1_jumps), 6),
            "l1_jump_std": round(_std(l1_jumps), 6),
            "c2_jump_std": round(_std(c2_jumps), 6),
            "p2_jump_std": round(_std(p2_jumps), 6),
            "l2_jump_std": round(_std(l2_jumps), 6),
            "ppp_score": round(score, 6),
        }
        results.append(metrics)

    results.sort(key=lambda item: item["ppp_score"], reverse=True)
    return results


def _select_good_satellites(
    satellite_metrics: list[dict],
    max_satellites: int = 12,
    min_presence_ratio: float = 0.30,
    min_dual_freq_ratio: float = 0.25,
    max_gap_epochs: int = 45,
) -> set[str]:
    selected: list[str] = []

    for item in satellite_metrics:
        if item["presence_ratio"] < min_presence_ratio:
            continue
        if item["code_dual_freq_ratio"] < min_dual_freq_ratio:
            continue
        if item["max_gap_epochs"] > max_gap_epochs:
            continue
        selected.append(item["satellite"])

    if not selected:
        selected = [item["satellite"] for item in satellite_metrics[:max_satellites]]

    return set(selected[:max_satellites])


def _filter_sat_observations(
    obs: dict[str, float | None],
    priority_obs: list[str],
    require_dual_frequency: bool,
    prefer_phase_but_do_not_require: bool,
) -> dict[str, float | None]:
    out = {name: obs.get(name) for name in priority_obs if name in obs}
    out = {k: v for k, v in out.items() if v is not None}

    if not out:
        return {}

    if require_dual_frequency and not _has_code_dual_frequency(out):
        return {}

    # não exige fase aqui; apenas deixa o writer decidir o que grava
    _ = prefer_phase_but_do_not_require

    return out


def apply_advanced_ppp_satellite_filter(
    seed: dict,
    max_satellites: int = 12,
    min_presence_ratio: float = 0.30,
    min_dual_freq_ratio: float = 0.25,
    max_gap_epochs: int = 45,
    require_dual_frequency: bool = True,
    prefer_phase_but_do_not_require: bool = True,
    priority_obs: list[str] | None = None,
) -> tuple[dict, dict]:
    if priority_obs is None:
        priority_obs = DEFAULT_PRIORITY_OBS[:]

    epochs = _extract_epochs(seed)
    metrics = analyze_advanced_ppp_satellites(
        seed=seed,
        prefer_phase_but_do_not_require=prefer_phase_but_do_not_require,
    )

    selected_satellites = _select_good_satellites(
        satellite_metrics=metrics,
        max_satellites=max_satellites,
        min_presence_ratio=min_presence_ratio,
        min_dual_freq_ratio=min_dual_freq_ratio,
        max_gap_epochs=max_gap_epochs,
    )

    filtered_epochs = []
    removed_satellite_instances = 0
    kept_satellite_instances = 0
    epochs_with_satellites = 0

    for epoch in epochs:
        epoch_dt = _extract_epoch_dt(epoch)
        if epoch_dt is None:
            continue

        kept_sat_items = []

        for sat_item in _extract_sat_items(epoch):
            sat_code = _extract_sat_code(sat_item)
            if not sat_code:
                removed_satellite_instances += 1
                continue

            if GPS_ONLY and _sat_system(sat_code) != "G":
                removed_satellite_instances += 1
                continue

            if sat_code not in selected_satellites:
                removed_satellite_instances += 1
                continue

            obs_map = _extract_obs_map(sat_item)
            filtered_obs = _filter_sat_observations(
                obs=obs_map,
                priority_obs=priority_obs,
                require_dual_frequency=require_dual_frequency,
                prefer_phase_but_do_not_require=prefer_phase_but_do_not_require,
            )
            if not filtered_obs:
                removed_satellite_instances += 1
                continue

            kept_sat_items.append(
                {
                    "satellite": sat_code,
                    "observations": filtered_obs,
                }
            )
            kept_satellite_instances += 1

        kept_sat_items.sort(key=lambda x: x["satellite"])

        filtered_epoch = {
            "epoch": epoch_dt.isoformat(),
            "satellite_count": len(kept_sat_items),
            "observations": kept_sat_items,
        }

        if kept_sat_items:
            epochs_with_satellites += 1

        filtered_epochs.append(filtered_epoch)

    filtered_seed = dict(seed)
    filtered_seed["epochs"] = filtered_epochs
    filtered_seed["epochs_generated"] = len(filtered_epochs)
    filtered_seed["message"] = "Filtro PPP por satélite aplicado."

    diagnostics = {
        "selected_satellites": sorted(selected_satellites),
        "selected_satellite_count": len(selected_satellites),
        "removed_satellite_instances": removed_satellite_instances,
        "kept_satellite_instances": kept_satellite_instances,
        "epochs_total": len(filtered_epochs),
        "epochs_with_satellites": epochs_with_satellites,
        "epochs_without_satellites": max(
            0, len(filtered_epochs) - epochs_with_satellites
        ),
        "require_dual_frequency": require_dual_frequency,
        "prefer_phase_but_do_not_require": prefer_phase_but_do_not_require,
        "priority_obs": priority_obs,
        "satellite_metrics": metrics,
    }

    return filtered_seed, diagnostics


def _best_contiguous_block_indexes(
    epochs: list[dict],
    min_satellites_per_epoch: int = 4,
) -> tuple[int | None, int | None]:
    valid_indexes = [
        idx
        for idx, epoch in enumerate(epochs)
        if len(_extract_sat_items(epoch)) >= min_satellites_per_epoch
    ]

    if not valid_indexes:
        return None, None

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

    return best_start, best_end


def select_best_advanced_ppp_block(
    seed: dict,
    min_satellites_per_epoch: int = 4,
    preserve_if_reasonable: bool = True,
) -> tuple[dict, dict]:
    epochs = _extract_epochs(seed)
    if not epochs:
        diagnostics = {
            "start_index": None,
            "end_index": None,
            "length_epochs": 0,
            "reason": "Seed sem épocas.",
            "preserved_original": True,
        }
        return seed, diagnostics

    total_epochs = len(epochs)
    start_idx, end_idx = _best_contiguous_block_indexes(
        epochs=epochs,
        min_satellites_per_epoch=min_satellites_per_epoch,
    )

    if start_idx is None or end_idx is None:
        diagnostics = {
            "start_index": None,
            "end_index": None,
            "length_epochs": 0,
            "reason": "Nenhuma época atende ao mínimo de satélites.",
            "preserved_original": True,
        }
        return seed, diagnostics

    block_epochs = epochs[start_idx : end_idx + 1]
    block_len = len(block_epochs)

    if preserve_if_reasonable and block_len >= max(1, int(round(total_epochs * 0.60))):
        diagnostics = {
            "start_index": 0,
            "end_index": total_epochs - 1,
            "length_epochs": total_epochs,
            "reason": "original_preserved_reasonable",
            "preserved_original": True,
            "best_block_start_index": start_idx,
            "best_block_end_index": end_idx,
            "best_block_length_epochs": block_len,
        }
        return seed, diagnostics

    out_seed = dict(seed)
    out_seed["epochs"] = block_epochs
    out_seed["epochs_generated"] = len(block_epochs)
    out_seed["epochs_skipped"] = max(0, len(epochs) - len(block_epochs))
    out_seed["message"] = "Melhor bloco PPP contínuo selecionado."

    diagnostics = {
        "start_index": start_idx,
        "end_index": end_idx,
        "start_epoch": block_epochs[0].get("epoch") if block_epochs else None,
        "end_epoch": block_epochs[-1].get("epoch") if block_epochs else None,
        "length_epochs": len(block_epochs),
        "avg_satellites": (
            round(
                sum(len(_extract_sat_items(ep)) for ep in block_epochs)
                / len(block_epochs),
                3,
            )
            if block_epochs
            else 0.0
        ),
        "reason": "best_contiguous_block",
        "preserved_original": False,
    }

    return out_seed, diagnostics


def harden_advanced_seed_for_ppp(
    seed: dict,
    max_satellites: int = 12,
    min_presence_ratio: float = 0.30,
    min_dual_freq_ratio: float = 0.25,
    max_gap_epochs: int = 45,
    min_satellites_per_epoch: int = 4,
    require_dual_frequency: bool = True,
    prefer_phase_but_do_not_require: bool = True,
    priority_obs: list[str] | None = None,
) -> tuple[dict, dict]:
    sat_filtered_seed, sat_diag = apply_advanced_ppp_satellite_filter(
        seed=seed,
        max_satellites=max_satellites,
        min_presence_ratio=min_presence_ratio,
        min_dual_freq_ratio=min_dual_freq_ratio,
        max_gap_epochs=max_gap_epochs,
        require_dual_frequency=require_dual_frequency,
        prefer_phase_but_do_not_require=prefer_phase_but_do_not_require,
        priority_obs=priority_obs,
    )

    block_seed, block_diag = select_best_advanced_ppp_block(
        seed=sat_filtered_seed,
        min_satellites_per_epoch=min_satellites_per_epoch,
        preserve_if_reasonable=True,
    )

    diagnostics = {
        "stage": "advanced_ppp_hardening",
        "require_dual_frequency": require_dual_frequency,
        "prefer_phase_but_do_not_require": prefer_phase_but_do_not_require,
        "satellite_filter": sat_diag,
        "block_selection": block_diag,
        "final_epoch_count": len(_extract_epochs(block_seed)),
        "final_satellite_catalog": sorted(
            {
                _extract_sat_code(sat_item)
                for epoch in _extract_epochs(block_seed)
                for sat_item in _extract_sat_items(epoch)
                if _extract_sat_code(sat_item)
            }
        ),
    }

    return block_seed, diagnostics
