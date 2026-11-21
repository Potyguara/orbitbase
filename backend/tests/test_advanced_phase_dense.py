from datetime import datetime, timedelta, timezone

from app.services.vrs_advanced.advanced_observation_interpolation_service import (
    _geometric_range,
    _gps_wavelength,
    interpolate_advanced_observations,
)

MASTER = (4_142_000.0, -4_670_000.0, -160_000.0)
TARGET = (4_143_500.0, -4_668_900.0, -158_700.0)
T0 = datetime(2026, 8, 20, 7, 5, 0, tzinfo=timezone.utc)

SATS = {
    "G05": ((15_600_000.0, -10_200_000.0, 19_100_000.0), (1200.0, 2500.0, -900.0)),
    "G07": ((20_100_000.0, -8_400_000.0, -12_300_000.0), (-800.0, 1900.0, 2100.0)),
    "G12": ((9_800_000.0, -21_500_000.0, 9_900_000.0), (2300.0, 600.0, -1800.0)),
    "G15": ((24_000_000.0, 2_100_000.0, 8_700_000.0), (-300.0, -2600.0, 1500.0)),
    "G20": ((1_500_000.0, -18_000_000.0, -19_000_000.0), (2900.0, -400.0, 700.0)),
}


def _sat_at(sat: str, t: datetime) -> dict:
    (x, y, z), (vx, vy, vz) = SATS[sat]
    dt = (t - T0).total_seconds()
    return {"x": x + vx * dt, "y": y + vy * dt, "z": z + vz * dt,
            "vx": vx, "vy": vy, "vz": vz}


def _residual(sat: str, t: datetime) -> float:
    # relógio do receptor + satélite: variação linear e lenta
    dt = (t - T0).total_seconds()
    return 1_000.0 + 3.0 * dt + 10.0 * int(sat[1:])


def _obs(sat: str, t: datetime, xyz=MASTER) -> dict:
    rho = _geometric_range(xyz, _sat_at(sat, t))
    r = _residual(sat, t)
    return {
        "C1": rho + r, "P2": rho + r + 2.0,
        "L1": (rho + r) / _gps_wavelength("L1"),
        "L2": (rho + r) / _gps_wavelength("L2"),
        "S1": 45.0, "S2": 40.0,
    }


def _build_inputs(start: datetime, end: datetime, jump_sat: str | None = None):
    epochs = []
    t = start - timedelta(seconds=30)
    while t <= end + timedelta(seconds=30):
        sats = []
        for sat in SATS:
            obs = _obs(sat, t)
            if jump_sat == sat and t >= T0 + timedelta(seconds=45):
                obs["L1"] += 1_000_000.0  # perda de ciclo grosseira
            sats.append({"satellite": sat, "observations": obs})
        epochs.append({"epoch_utc": t.isoformat().replace("+00:00", "Z"),
                       "satellites": sats})
        t += timedelta(seconds=15)

    base = {"station_code": "BELE", "approx_x": MASTER[0], "approx_y": MASTER[1],
            "approx_z": MASTER[2], "epochs": epochs}

    geometry_times = set()
    t = start - timedelta(seconds=30)
    while t <= end + timedelta(seconds=30):
        geometry_times.add(t)
        t += timedelta(seconds=1)
    geometry = {"status": "ok", "epochs": [
        {"epoch": g.isoformat().replace("+00:00", "Z"),
         "satellites": {s: _sat_at(s, g) for s in SATS}}
        for g in sorted(geometry_times)
    ]}

    grid = []
    t = start
    while t <= end:
        grid.append(t)
        t += timedelta(seconds=1)

    config = {"target_x": TARGET[0], "target_y": TARGET[1], "target_z": TARGET[2],
              "interpolate_phase_observables": True, "target_step_seconds": 1,
              "min_satellites_per_epoch": 4}
    return grid, [base], geometry, config


def test_phase_dense_outputs_requested_one_second_grid():
    start = T0 + timedelta(seconds=22)
    end = T0 + timedelta(seconds=98)
    grid, bases, geometry, config = _build_inputs(start, end)

    result = interpolate_advanced_observations(grid, bases, geometry, config)

    assert result["summary"]["mode"] == "phase_master_dense"
    epochs = result["epochs"]
    assert len(epochs) == len(grid) == 77
    assert epochs[0]["epoch"] == "2026-08-20T07:05:22Z"
    assert epochs[-1]["epoch"] == "2026-08-20T07:06:38Z"

    for ep in epochs:
        t = datetime.fromisoformat(ep["epoch"].replace("Z", "+00:00"))
        assert ep["satellite_count"] == len(SATS)
        for item in ep["observations"]:
            expected = _obs(item["satellite"], t, xyz=TARGET)
            # resíduo linear: a interpolação reproduz o valor no alvo
            assert abs(item["L1"] - expected["L1"]) < 1e-3
            assert abs(item["L2"] - expected["L2"]) < 1e-3
            assert abs(item["C1"] - expected["C1"]) < 1e-3


def test_phase_dense_drops_satellite_with_cycle_slip():
    start = T0 + timedelta(seconds=22)
    end = T0 + timedelta(seconds=98)
    grid, bases, geometry, config = _build_inputs(start, end, jump_sat="G12")

    result = interpolate_advanced_observations(grid, bases, geometry, config)

    by_epoch = {ep["epoch"]: {o["satellite"] for o in ep["observations"]}
                for ep in result["epochs"]}
    # épocas da mestra: 07:05:37 e 07:05:52; o salto fica entre elas,
    # então G12 sai só nesse intervalo e volta depois
    assert "G12" not in by_epoch["2026-08-20T07:05:45Z"]
    assert "G12" in by_epoch["2026-08-20T07:05:35Z"]
    assert "G12" in by_epoch["2026-08-20T07:06:00Z"]
    assert len(by_epoch["2026-08-20T07:05:45Z"]) == len(SATS) - 1
