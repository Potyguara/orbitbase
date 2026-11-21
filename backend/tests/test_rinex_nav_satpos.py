import math
from datetime import datetime, timedelta, timezone

import pytest

from app.services import rinex_nav_satpos_service as nav
from tests.gnss_fixtures import (
    analytic_circular_position,
    make_record,
    write_rinex2_nav,
    write_rinex3_nav,
)

# 2026-06-03 é uma quarta-feira: o bug antigo (segundos do dia x segundos
# da semana) erraria a posição em ~3 dias de propagação orbital.
WEDNESDAY_TOC = datetime(2026, 6, 3, 12, 0, 0)


def _dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


@pytest.fixture
def rec():
    return make_record(WEDNESDAY_TOC)


@pytest.mark.parametrize("writer", [write_rinex2_nav, write_rinex3_nav])
def test_parser_reads_rinex2_and_rinex3_gps_records(tmp_path, rec, writer):
    path = writer(tmp_path / "aplj1541.26n", [(3, WEDNESDAY_TOC, rec), (12, WEDNESDAY_TOC, rec)])

    records = nav.parse_rinex_nav_gps(str(path))

    assert sorted(records) == ["G03", "G12"]
    parsed = records["G03"][0]
    assert parsed["toc"] == WEDNESDAY_TOC
    assert parsed["sqrt_a"] == pytest.approx(rec["sqrt_a"])
    assert parsed["toe"] == pytest.approx(rec["toe"])
    assert parsed["omega0"] == pytest.approx(rec["omega0"])
    assert parsed["gps_week"] == pytest.approx(rec["gps_week"])
    assert parsed["af0"] == pytest.approx(rec["af0"])


def test_position_uses_gps_week_seconds_not_seconds_of_day(rec):
    for dt_s in (0, 600, 3600, -1800):
        t = WEDNESDAY_TOC + timedelta(seconds=dt_s)
        got = nav.compute_gps_satellite_position_ecef(rec, t)
        expected = analytic_circular_position(rec, t)
        assert _dist((got["x"], got["y"], got["z"]), expected) < 1e-3


def test_position_accepts_timezone_aware_epochs(rec):
    aware = WEDNESDAY_TOC.replace(tzinfo=timezone.utc)
    got = nav.compute_gps_satellite_position_ecef(rec, aware)
    expected = analytic_circular_position(rec, WEDNESDAY_TOC)
    assert _dist((got["x"], got["y"], got["z"]), expected) < 1e-3


def test_state_has_plausible_velocity_and_clock(rec):
    state = nav.compute_gps_satellite_state_ecef(rec, WEDNESDAY_TOC)
    speed = math.sqrt(state["vx"] ** 2 + state["vy"] ** 2 + state["vz"] ** 2)
    # Velocidade inercial ~3.87 km/s; no ECEF fica entre ~1.5 e 4.5 km/s.
    assert 1500 < speed < 4500
    assert state["clock_us"] == pytest.approx(rec["af0"] * 1e6)


def test_stale_or_unhealthy_ephemeris_is_rejected(rec):
    far = WEDNESDAY_TOC + timedelta(hours=6)
    assert nav.select_ephemeris([rec], far) is None

    sick = dict(rec, sv_health=1.0)
    assert nav.select_ephemeris([sick], WEDNESDAY_TOC) is None


def test_cache_from_nav_keeps_z_suffixed_epoch_keys(tmp_path, rec):
    path = write_rinex2_nav(tmp_path / "bele1541.26n", [(5, WEDNESDAY_TOC, rec)])
    key = "2026-06-03T12:00:00Z"

    cache = nav.build_satellite_position_cache_from_nav(str(path), {key: ["G05", "G06"]})

    assert set(cache[key]) == {"G05"}
    assert "vx" in cache[key]["G05"]


def test_merge_records_from_several_station_files(tmp_path, rec):
    a = write_rinex2_nav(tmp_path / "a" / "aps11541.26n", [(1, WEDNESDAY_TOC, rec)])
    b = write_rinex2_nav(
        tmp_path / "b" / "bepa1541.26n",
        [(1, WEDNESDAY_TOC, rec), (7, WEDNESDAY_TOC, rec)],
    )
    merged, errors = nav.merge_navigation_records([str(a), str(b)])
    assert errors == []
    assert sorted(merged) == ["G01", "G07"]
    assert len(merged["G01"]) == 1  # duplicata removida


def test_rinex2_glonass_file_is_not_parsed_as_gps(tmp_path):
    path = tmp_path / "aplj1541.26g"
    path.write_text(
        "     2.11           G: GLONASS NAV DATA                     RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        " 1 26  6  3 12 15  0.0 .100000000000D-04 .000000000000D+00 .000000000000D+00\n",
        encoding="utf-8",
    )
    assert nav.parse_rinex_nav_gps(str(path)) == {}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("aplj1541.26n", True),
        ("APLJ1541.26N", True),
        ("brdc1540.26n.gz", True),
        ("APLJ00BRA_R_20261540000_01D_GN.rnx", True),
        ("APLJ00BRA_R_20261540000_01D_MN.rnx", True),
        ("aplj1541.26g", False),
        ("brdc1540.26g", False),
        ("aplj1541.26o", False),
        ("aplj1541.26d", False),
        ("APLJ00BRA_R_20261540000_01D_30S_MO.rnx", False),
        ("APLJ00BRA_R_20261540000_01D_RN.rnx", False),
    ],
)
def test_navigation_filename_classification(name, expected):
    assert nav.is_gps_navigation_filename(name) is expected
