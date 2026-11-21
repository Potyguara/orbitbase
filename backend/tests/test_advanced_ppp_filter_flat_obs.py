from datetime import datetime, timedelta, timezone

from app.services.vrs_advanced.advanced_ppp_filter_service import harden_advanced_seed_for_ppp


def _phase_mode_seed(epochs=240, sats=("G05", "G06", "G07", "G09", "G11", "G12")):
    t0 = datetime(2026, 8, 20, 10, 5, 30, tzinfo=timezone.utc)
    out = []
    for i in range(epochs):
        obs = []
        for k, sat in enumerate(sats):
            base = 2.3e7 + k * 1e5 + i * 10.0
            # formato gerado por _build_output_epochs_phase_mode: observáveis no nível do satélite
            obs.append({
                "satellite": sat,
                "C1": base, "P1": None, "L1": base / 0.19029367,
                "C2": base + 2.0, "P2": base + 2.3, "L2": base / 0.24421021,
            })
        out.append({"epoch": (t0 + timedelta(seconds=15 * i)).isoformat(),
                    "observations": obs, "satellite_count": len(obs)})
    return {"status": "completed", "epochs": out, "interval_seconds": 15.0}


def test_phase_mode_flat_observations_are_kept():
    seed, diag = harden_advanced_seed_for_ppp(
        seed=_phase_mode_seed(),
        require_dual_frequency=True,
        require_phase_for_dual_frequency=True,
        prefer_phase_but_do_not_require=False,
        allowed_systems={"G"},
    )
    epochs_with_sats = sum(1 for e in seed["epochs"] if e.get("observations"))
    assert epochs_with_sats > 0
    first = seed["epochs"][0]["observations"][0]["observations"]
    assert {"C1", "L1", "P2", "L2"} <= set(first)
