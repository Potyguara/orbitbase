from __future__ import annotations


def validate_advanced_seed(seed: dict, config: dict) -> dict:
    epochs_generated = int(seed.get("epochs_generated", 0))
    min_sat = int(config.get("min_satellites_per_epoch", 4))

    return {
        "status": "failed" if epochs_generated <= 0 else "completed",
        "epochs_generated": epochs_generated,
        "min_satellites_per_epoch": min_sat,
        "message": (
            "Nenhuma época avançada foi gerada."
            if epochs_generated <= 0
            else "Seed avançada validada."
        ),
    }
