from __future__ import annotations

from pathlib import Path
import json


DEFAULT_PRIORITY_OBS = ["P1", "C1", "L1", "P2", "C2", "L2", "C5", "S1", "S2", "S5"]


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _read_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _count_non_null(observations: dict, obs_types: list[str]) -> int:
    total = 0
    for obs_name in obs_types:
        if _safe_float(observations.get(obs_name)) is not None:
            total += 1
    return total


def sanitize_virtual_observations(
    virtual_data: dict,
    min_obs_per_satellite: int = 2,
    keep_only_priority_obs: bool = True,
    priority_obs: list[str] | None = None,
    drop_empty_epochs: bool = True,
) -> dict:
    if priority_obs is None:
        priority_obs = DEFAULT_PRIORITY_OBS[:]

    original_obs_types = virtual_data.get("common_observation_types", []) or []
    original_epochs = virtual_data.get("epochs", []) or []

    if keep_only_priority_obs:
        final_obs_types = [obs for obs in priority_obs if obs in original_obs_types]
        extra = [obs for obs in original_obs_types if obs not in final_obs_types]
        final_obs_types.extend(extra)
    else:
        final_obs_types = original_obs_types[:]

    sanitized_epochs = []
    removed_satellites = 0
    removed_epochs = 0

    for epoch in original_epochs:
        sats = epoch.get("satellites", []) or []
        clean_sats = []

        for sat in sats:
            obs = sat.get("observations", {}) or {}
            filtered_obs = {obs_name: obs.get(obs_name) for obs_name in final_obs_types}

            available_count = _count_non_null(filtered_obs, final_obs_types)
            if available_count < min_obs_per_satellite:
                removed_satellites += 1
                continue

            clean_sats.append(
                {
                    "satellite": sat.get("satellite"),
                    "available_observation_count": available_count,
                    "observations": filtered_obs,
                }
            )

        if not clean_sats and drop_empty_epochs:
            removed_epochs += 1
            continue

        sanitized_epochs.append(
            {
                "epoch": epoch.get("epoch"),
                "common_satellite_count": epoch.get("common_satellite_count", 0),
                "generated_satellite_count": len(clean_sats),
                "satellites": clean_sats,
            }
        )

    return {
        "processing_mode": virtual_data.get("processing_mode"),
        "method": f"{virtual_data.get('method', 'unknown')}_sanitized",
        "target": virtual_data.get("target", {}),
        "weights_reference": virtual_data.get("weights_reference", {}),
        "common_observation_types": final_obs_types,
        "common_epoch_count": virtual_data.get("common_epoch_count", 0),
        "generated_epoch_count": len(sanitized_epochs),
        "min_obs_per_satellite": min_obs_per_satellite,
        "drop_empty_epochs": drop_empty_epochs,
        "removed_satellites": removed_satellites,
        "removed_epochs": removed_epochs,
        "epochs": sanitized_epochs,
    }


def summarize_sanitized_virtual_observations(sanitized_data: dict) -> dict:
    obs_types = sanitized_data.get("common_observation_types", []) or []
    epochs = sanitized_data.get("epochs", []) or []

    per_obs_non_null = {obs: 0 for obs in obs_types}
    total_satellites = 0
    epochs_with_satellites = 0
    max_satellites_per_epoch = 0

    for epoch in epochs:
        sats = epoch.get("satellites", []) or []
        sat_count = len(sats)

        total_satellites += sat_count
        if sat_count > 0:
            epochs_with_satellites += 1
        if sat_count > max_satellites_per_epoch:
            max_satellites_per_epoch = sat_count

        for sat in sats:
            obs = sat.get("observations", {}) or {}
            for obs_name in obs_types:
                if _safe_float(obs.get(obs_name)) is not None:
                    per_obs_non_null[obs_name] += 1

    return {
        "processing_mode": sanitized_data.get("processing_mode"),
        "method": sanitized_data.get("method"),
        "common_epoch_count": sanitized_data.get("common_epoch_count", 0),
        "generated_epoch_count": sanitized_data.get("generated_epoch_count", 0),
        "epochs_with_satellites": epochs_with_satellites,
        "max_satellite_count_per_epoch": max_satellites_per_epoch,
        "total_satellites_after_sanitize": total_satellites,
        "common_observation_types": obs_types,
        "per_observation_non_null": per_obs_non_null,
        "removed_satellites": sanitized_data.get("removed_satellites", 0),
        "removed_epochs": sanitized_data.get("removed_epochs", 0),
        "min_obs_per_satellite": sanitized_data.get("min_obs_per_satellite"),
        "sample_epochs": epochs[:10],
    }


def write_sanitized_virtual_observations_json(
    output_path: str | Path,
    sanitized_data: dict,
) -> str:
    path = Path(output_path)
    path.write_text(
        json.dumps(sanitized_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)
