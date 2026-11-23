from __future__ import annotations


def get_default_advanced_config() -> dict:
    return {
        "mode": "advanced_experimental",
        "target_step_seconds": 1,
        "use_precise_ephemeris": True,
        "use_precise_clock": False,
        "interpolate_code_observables": True,
        "interpolate_phase_observables": False,
        "interpolate_snr": False,
        "max_interpolation_gap_seconds": 30,
        "min_common_bases": 3,
        "min_satellites_per_epoch": 4,
        "receiver_brand": "ORBITBASE",
        "receiver_model": "ORBITBASE VRS",
        "receiver_version": "1.0",
        "antenna_model": "VIRTUAL",
        "write_debug_json": True,
        "write_intermediate_files": True,
    }


def normalize_advanced_config(raw: dict | None) -> dict:
    cfg = get_default_advanced_config()
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg
