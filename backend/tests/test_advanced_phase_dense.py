from datetime import datetime, timedelta, timezone

from app.services.vrs_advanced.advanced_observation_interpolation_service import (
    _choose_master_base,
    _geodetic_lat_h,
    _range_with_tropo,
    _slant_tropo_delay,
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
    rho = _range_with_tropo(xyz, _sat_at(sat, t))
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


def _fake_base(code, xyz, dual_phase_epochs):
    rec = {"C1": 1.0, "P2": 1.0, "L1": 1.0, "L2": 1.0}
    return {"station_code": code, "xyz": xyz,
            "epochs": [{"obs_map": {"G05": rec}} for _ in range(dual_phase_epochs)]}


def test_master_is_nearest_base_with_enough_quality():
    target = (3982061.76, -4982280.21, 24728.85)
    apma = _fake_base("APMA", (4005474.1188, -4963530.7910, 5200.8584), 1400)
    boav = _fake_base("BOAV", (3117452.2165, -5555487.8182, 314480.7389), 1540)
    weak = _fake_base("NEAR", (3982100.0, -4982300.0, 24700.0), 100)
    chosen = _choose_master_base([boav, weak, apma], target)
    assert chosen["station_code"] == "APMA"


def test_tropo_delay_is_sane():
    target = (3982061.76, -4982280.21, 24728.85)
    _, _, h = _geodetic_lat_h(target)
    assert -20.0 < h < 10.0
    # satélite no zênite (~20.000 km acima) e outro baixo no horizonte
    zenith = {"x": target[0] * 4.2, "y": target[1] * 4.2, "z": target[2] * 4.2}
    low = {"x": 15_600_000.0, "y": -10_200_000.0, "z": 19_100_000.0}
    dz = _slant_tropo_delay(target, zenith)
    assert 2.3 < dz < 2.8
    assert _slant_tropo_delay(target, low) > dz


def test_antenna_delta_moves_base_up():
    from app.services.vrs_advanced.advanced_observation_interpolation_service import (
        _apply_antenna_delta,
    )
    from app.services.vrs_advanced.advanced_rinex_observation_reader_service import (
        _parse_antenna_delta_hen,
    )

    line = f"{0.0080:14.4f}{0.0:14.4f}{0.0:14.4f}".ljust(60) + "ANTENNA: DELTA H/E/N"
    delta = _parse_antenna_delta_hen([line])
    assert delta == (0.008, 0.0, 0.0)

    marker = (4005474.1188, -4963530.7910, 5200.8584)
    arp = _apply_antenna_delta(marker, list(delta))
    _, _, h0 = _geodetic_lat_h(marker)
    _, _, h1 = _geodetic_lat_h(arp)
    assert abs((h1 - h0) - 0.008) < 1e-4


def test_phase_dense_transports_glonass_with_channel_wavelength():
    from app.services.vrs_unified.glonass_nav import glonass_wavelength

    start = T0 + timedelta(seconds=22)
    end = T0 + timedelta(seconds=40)
    grid, bases, geometry, _ = _build_inputs(start, end)
    k = -3
    lam = {"L1": glonass_wavelength("L1", k), "L2": glonass_wavelength("L2", k)}
    for ep in bases[0]["epochs"]:
        t = datetime.fromisoformat(ep["epoch_utc"].replace("Z", "+00:00"))
        rho = _range_with_tropo(MASTER, _sat_at("G07", t))
        r = _residual("G07", t) + 7.0
        ep["satellites"].append({"satellite": "R11", "observations": {
            "C1": rho + r, "P2": rho + r, "L1": (rho + r) / lam["L1"], "L2": (rho + r) / lam["L2"]}})
    for g in geometry["epochs"]:
        g["satellites"]["R11"] = dict(g["satellites"]["G07"])

    from app.services.vrs_advanced.advanced_observation_interpolation_service import (
        _build_output_epochs_phase_dense, _gps_wavelength,
    )

    master = {"xyz": MASTER, "epochs": [
        {"epoch": datetime.fromisoformat(ep["epoch_utc"].replace("Z", "+00:00")),
         "epoch_iso": ep["epoch_utc"],
         "obs_map": {s["satellite"]: s["observations"] for s in ep["satellites"]}}
        for ep in bases[0]["epochs"]]}
    geo = {g["epoch"]: g["satellites"] for g in geometry["epochs"]}

    def for_sat(sat):
        if sat.startswith("G"):
            return _gps_wavelength
        return lambda code: lam.get(code)

    epochs, skipped = _build_output_epochs_phase_dense(
        master, geo, TARGET, grid, 30.0, 4, systems=("G", "R"), wavelength_for=for_sat)
    assert skipped == 0
    for ep in epochs:
        t = datetime.fromisoformat(ep["epoch"].replace("Z", "+00:00"))
        r11 = [o for o in ep["observations"] if o["satellite"] == "R11"][0]
        expected = _range_with_tropo(TARGET, _sat_at("G07", t)) + _residual("G07", t) + 7.0
        assert abs(r11["L1"] * lam["L1"] - expected) < 1e-3

    only_gps, _ = _build_output_epochs_phase_dense(master, geo, TARGET, grid, 30.0, 4)
    assert all(o["satellite"].startswith("G") for ep in only_gps for o in ep["observations"])
