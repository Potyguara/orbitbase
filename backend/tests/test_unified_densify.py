from datetime import datetime, timedelta, timezone

from app.services.vrs_advanced.advanced_observation_interpolation_service import (
    _gps_wavelength,
    _range_with_tropo,
)
from app.services.vrs_advanced.advanced_rinex_writer_service import (
    write_advanced_rinex_from_seed,
)
from app.services.vrs_unified.unified_service import (
    _padded_basic_window,
    build_target_grid,
    densify_basic_output,
    parse_local_time,
)

TARGET = (3982061.7589, -4982280.2085, 24728.8544)
T0 = datetime(2026, 8, 20, 10, 5, 0, tzinfo=timezone.utc)
SATS = {
    "G05": ((15_600_000.0, -10_200_000.0, 19_100_000.0), (1200.0, 2500.0, -900.0)),
    "G07": ((20_100_000.0, -8_400_000.0, -12_300_000.0), (-800.0, 1900.0, 2100.0)),
    "G12": ((9_800_000.0, -21_500_000.0, 9_900_000.0), (2300.0, 600.0, -1800.0)),
    "G15": ((24_000_000.0, 2_100_000.0, 8_700_000.0), (-300.0, -2600.0, 1500.0)),
    "G20": ((1_500_000.0, -18_000_000.0, -19_000_000.0), (2900.0, -400.0, 700.0)),
}


def _sat(sat, t):
    (x, y, z), (vx, vy, vz) = SATS[sat]
    dt = (t - T0).total_seconds()
    return {"x": x + vx * dt, "y": y + vy * dt, "z": z + vz * dt}


def _obs(sat, t):
    dt = (t - T0).total_seconds()
    m = _range_with_tropo(TARGET, _sat(sat, t)) + 500.0 + 2.0 * dt
    return {"C1": m, "P2": m + 1.5, "L1": m / _gps_wavelength("L1"),
            "L2": m / _gps_wavelength("L2")}


def test_densify_reaches_exact_seconds_at_one_second(tmp_path):
    epochs = []
    t = T0
    while t <= T0 + timedelta(minutes=3):
        epochs.append({"epoch": t.isoformat(), "observations": [
            {"satellite": s, **_obs(s, t)} for s in SATS]})
        t += timedelta(seconds=15)
    basic = write_advanced_rinex_from_seed(
        output_path=str(tmp_path / "basic.obs"), advanced_seed={"epochs": epochs},
        approx_position_xyz=TARGET, interval_seconds=15.0,
        forced_obs_types=["C1", "P1", "L1", "C2", "P2", "L2"])

    geometry = {}
    t = T0
    while t <= T0 + timedelta(minutes=3):
        geometry[t.isoformat().replace("+00:00", "Z")] = {s: _sat(s, t) for s in SATS}
        t += timedelta(seconds=1)

    start = T0 + timedelta(seconds=22)
    end = T0 + timedelta(seconds=158)
    grid = build_target_grid(start, end, 1)
    out, skipped = densify_basic_output(tmp_path / "basic.obs", TARGET, geometry, grid)
    assert skipped == 0
    assert len(out) == len(grid) == 137
    assert out[0]["epoch"].endswith("10:05:22Z") and out[-1]["epoch"].endswith("10:07:38Z")
    for ep in out[::17]:
        t = datetime.fromisoformat(ep["epoch"].replace("Z", "+00:00"))
        for item in ep["observations"]:
            exp = _obs(item["satellite"], t)
            # RINEX grava 3 casas decimais: tolerância de milímetros/milésimos de ciclo
            assert abs(item["C1"] - exp["C1"]) < 0.01
            assert abs(item["L1"] - exp["L1"]) < 0.01


def test_time_parsing_and_padding():
    assert parse_local_time("2026-08-20", "07:05").second == 0
    assert parse_local_time("2026-08-20", "07:05:22").second == 22
    s, e = _padded_basic_window(parse_local_time("2026-08-20", "07:05:22"),
                                parse_local_time("2026-08-20", "13:26:38"))
    assert (s, e) == ("07:04", "13:28")
    grid = build_target_grid(datetime(2026, 1, 1, 0, 0, 0), datetime(2026, 1, 1, 0, 0, 37), 15)
    assert [g.second for g in grid] == [0, 15, 30, 37]


def test_short_occupation_gets_wide_basic_window():
    s, e = _padded_basic_window(parse_local_time("2026-08-20", "10:00:10"),
                                parse_local_time("2026-08-20", "10:02:40"))
    assert (s, e) == ("09:31", "10:32")
    s, e = _padded_basic_window(parse_local_time("2026-08-20", "00:01:00"),
                                parse_local_time("2026-08-20", "00:03:00"))
    assert s == "00:00" and e >= "01:00"
