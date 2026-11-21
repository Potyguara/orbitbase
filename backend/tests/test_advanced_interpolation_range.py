import math

from app.services.vrs_advanced.advanced_observation_interpolation_service import (
    _geometric_range,
)

SAT = {"x": 15_600_000.0, "y": -10_200_000.0, "z": 19_100_000.0,
       "vx": 1200.0, "vy": 2500.0, "vz": -900.0}
BELEM = (4_142_000.0, -4_670_000.0, -160_000.0)


def test_range_includes_light_time_and_sagnac():
    plain = math.dist(BELEM, (SAT["x"], SAT["y"], SAT["z"]))
    corrected = _geometric_range(BELEM, SAT)
    # Correções de dezenas de metros: relevantes para a VRS, mas limitadas.
    assert 1.0 < abs(corrected - plain) < 500.0


def test_range_without_velocity_still_works():
    sat = {k: SAT[k] for k in ("x", "y", "z")}
    assert _geometric_range(BELEM, sat) is not None
    assert _geometric_range(BELEM, {"x": None, "y": 1, "z": 2}) is None
