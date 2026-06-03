from __future__ import annotations


def _count_priority_obs(satellite: dict, priority_obs: list[str]) -> int:
    observations = (satellite or {}).get("observations", {}) or {}
    return sum(1 for obs_name in priority_obs if observations.get(obs_name) is not None)


def _epoch_metrics(epoch: dict, priority_obs: list[str]) -> dict:
    satellites = (epoch or {}).get("satellites", []) or []

    satellite_count = len(satellites)

    if satellite_count == 0:
        return {
            "epoch": epoch.get("epoch"),
            "satellite_count": 0,
            "avg_priority_obs_per_sat": 0.0,
            "is_ppp_candidate": False,
        }

    total_priority_obs = sum(
        _count_priority_obs(satellite, priority_obs) for satellite in satellites
    )

    avg_priority_obs_per_sat = total_priority_obs / satellite_count

    return {
        "epoch": epoch.get("epoch"),
        "satellite_count": satellite_count,
        "avg_priority_obs_per_sat": avg_priority_obs_per_sat,
        "is_ppp_candidate": True,
    }


def _estimate_epoch_interval_seconds(epochs: list[dict]) -> float:
    if len(epochs) < 2:
        return 15.0

    try:
        from datetime import datetime

        dt1 = datetime.fromisoformat(epochs[0]["epoch"])
        dt2 = datetime.fromisoformat(epochs[1]["epoch"])
        delta = (dt2 - dt1).total_seconds()
        return delta if delta > 0 else 15.0
    except Exception:
        return 15.0


def select_best_continuous_ppp_block(
    epochs: list[dict],
    min_consecutive_epochs: int = 60,
    min_satellites_per_epoch: int = 6,
    min_priority_obs_per_satellite_avg: float = 3.0,
    max_gap_factor: float = 2.0,
    priority_obs: list[str] | None = None,
) -> dict:
    if priority_obs is None:
        priority_obs = ["L1", "L2", "C1", "P1", "C2", "P2", "C5", "S1", "S2", "S5"]

    if not epochs:
        return {
            "selected_epochs": [],
            "selected_epoch_count": 0,
            "selected_start": None,
            "selected_end": None,
            "selected_avg_satellites": 0.0,
            "selected_avg_priority_obs_per_sat": 0.0,
            "best_block_score": 0.0,
            "blocks_found": [],
            "reason": "Nenhuma época disponível para seleção PPP.",
        }

    epoch_interval_seconds = _estimate_epoch_interval_seconds(epochs)
    max_gap_seconds = epoch_interval_seconds * max_gap_factor

    metrics = [_epoch_metrics(epoch, priority_obs) for epoch in epochs]

    filtered = []
    for epoch, metric in zip(epochs, metrics):
        satellite_count = metric["satellite_count"]
        avg_priority_obs_per_sat = metric["avg_priority_obs_per_sat"]

        is_valid = (
            satellite_count >= min_satellites_per_epoch
            and avg_priority_obs_per_sat >= min_priority_obs_per_satellite_avg
        )

        filtered.append(
            {
                "epoch_dict": epoch,
                "epoch": epoch.get("epoch"),
                "satellite_count": satellite_count,
                "avg_priority_obs_per_sat": avg_priority_obs_per_sat,
                "is_valid": is_valid,
            }
        )

    from datetime import datetime

    blocks = []
    current_block = []

    for item in filtered:
        if not item["is_valid"]:
            if current_block:
                blocks.append(current_block)
                current_block = []
            continue

        if not current_block:
            current_block = [item]
            continue

        prev_dt = datetime.fromisoformat(current_block[-1]["epoch"])
        curr_dt = datetime.fromisoformat(item["epoch"])
        gap_seconds = (curr_dt - prev_dt).total_seconds()

        if gap_seconds <= max_gap_seconds:
            current_block.append(item)
        else:
            blocks.append(current_block)
            current_block = [item]

    if current_block:
        blocks.append(current_block)

    qualified_blocks = [
        block for block in blocks if len(block) >= min_consecutive_epochs
    ]

    blocks_found = []

    for block in qualified_blocks:
        epoch_count = len(block)
        avg_satellites = sum(item["satellite_count"] for item in block) / epoch_count
        avg_priority_obs_per_sat = (
            sum(item["avg_priority_obs_per_sat"] for item in block) / epoch_count
        )

        continuity_ratio = epoch_count / max(1, len(epochs))
        score = (
            epoch_count
            + (avg_satellites * 2.0)
            + (avg_priority_obs_per_sat * 4.0)
            + (continuity_ratio * 20.0)
        )

        blocks_found.append(
            {
                "start": block[0]["epoch"],
                "end": block[-1]["epoch"],
                "epoch_count": epoch_count,
                "avg_satellites": avg_satellites,
                "avg_priority_obs_per_sat": avg_priority_obs_per_sat,
                "continuity_ratio": continuity_ratio,
                "score": score,
                "epochs": [item["epoch_dict"] for item in block],
            }
        )

    if not blocks_found:
        return {
            "selected_epochs": [],
            "selected_epoch_count": 0,
            "selected_start": None,
            "selected_end": None,
            "selected_avg_satellites": 0.0,
            "selected_avg_priority_obs_per_sat": 0.0,
            "best_block_score": 0.0,
            "blocks_found": [],
            "reason": "Nenhum bloco contínuo atendeu aos critérios PPP.",
        }

    best_block = max(blocks_found, key=lambda item: item["score"])

    serializable_blocks = [
        {
            "start": block["start"],
            "end": block["end"],
            "epoch_count": block["epoch_count"],
            "avg_satellites": block["avg_satellites"],
            "avg_priority_obs_per_sat": block["avg_priority_obs_per_sat"],
            "continuity_ratio": block["continuity_ratio"],
            "score": block["score"],
        }
        for block in blocks_found
    ]

    return {
        "selected_epochs": best_block["epochs"],
        "selected_epoch_count": best_block["epoch_count"],
        "selected_start": best_block["start"],
        "selected_end": best_block["end"],
        "selected_avg_satellites": best_block["avg_satellites"],
        "selected_avg_priority_obs_per_sat": best_block["avg_priority_obs_per_sat"],
        "best_block_score": best_block["score"],
        "blocks_found": serializable_blocks,
        "reason": "Bloco PPP-ready selecionado com sucesso.",
    }
