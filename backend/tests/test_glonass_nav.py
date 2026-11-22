import math
from datetime import datetime, timedelta, timezone

from app.services.vrs_unified import glonass_nav as gn


def _jacobi(state):
    x, y, z, vx, vy, vz = state
    r = math.sqrt(x * x + y * y + z * z)
    u = gn.MU / r * (1 + gn.J2 / 2 * (gn.AE / r) ** 2 * (1 - 3 * (z / r) ** 2))
    return 0.5 * (vx * vx + vy * vy + vz * vz) - 0.5 * gn.OMEGA ** 2 * (x * x + y * y) - u


STATE = [1.4e7, -1.6e7, 1.3e7, 1500.0, 1800.0, -800.0]


def test_orbit_conserves_jacobi_constant_and_is_reversible():
    s1 = gn.propagate(STATE, (0.0, 0.0, 0.0), 900.0)
    assert abs(_jacobi(s1) - _jacobi(STATE)) < 1e-3
    back = gn.propagate(s1, (0.0, 0.0, 0.0), -900.0)
    assert max(abs(a - b) for a, b in zip(back[:3], STATE[:3])) < 1e-3


def _d(v):
    return f"{v:19.12E}".replace("E", "D")


def test_parse_rinex2_and_rinex3(tmp_path):
    vals = [(14000.0, 1.5, 0.0, 0.0), (-16000.0, 1.8, 0.0, -3.0), (13000.0, -0.8, 0.0, 0.0)]
    hdr = "     2.11           G: GLONASS NAV DATA".ljust(60) + "RINEX VERSION / TYPE\n"
    hdr += " " * 60 + "END OF HEADER\n"
    body = " 5 26  8 20  7 15  0.0" + _d(1e-5) + _d(0.0) + _d(0.0) + "\n"
    for row in vals:
        body += "   " + "".join(_d(v) for v in row) + "\n"
    p2 = tmp_path / "apma2320.26g"
    p2.write_text(hdr + body)
    r2 = gn.parse_glonass_nav(str(p2))
    rec = r2["R05"][0]
    assert rec["toc"] == datetime(2026, 8, 20, 7, 15, 0)
    assert rec["channel"] == -3
    assert rec["pos"] == (14_000_000.0, -16_000_000.0, 13_000_000.0)

    hdr3 = "     3.04           N: GNSS NAV DATA    R: GLONASS".ljust(60) + "RINEX VERSION / TYPE\n"
    hdr3 += " " * 60 + "END OF HEADER\n"
    body3 = "R05 2026 08 20 07 15 00" + _d(1e-5) + _d(0.0) + _d(0.0) + "\n"
    for row in vals:
        body3 += "    " + "".join(_d(v) for v in row) + "\n"
    p3 = tmp_path / "X_RN.rnx"
    p3.write_text(hdr3 + body3)
    r3 = gn.parse_glonass_nav(str(p3))
    assert r3["R05"][0]["pos"] == rec["pos"]
    assert r3["R05"][0]["channel"] == -3


def test_positions_use_gps_time_and_velocity():
    toc = datetime(2026, 8, 20, 7, 15, 0)
    rec = {"satellite": "R05", "toc": toc, "pos": tuple(STATE[:3]), "vel": tuple(STATE[3:]),
           "acc": (0.0, 0.0, 0.0), "health": 0, "channel": 1}
    t_gps = toc + timedelta(seconds=gn.GPS_UTC_LEAP_SECONDS)
    pos = gn.glonass_positions({"R05": [rec]}, [t_gps, t_gps + timedelta(seconds=1)])
    first = pos[t_gps]["R05"]
    assert abs(first["x"] - STATE[0]) < 1e-6
    assert first["vx"] == STATE[3]
    assert gn.glonass_wavelength("L1", 1) < gn.glonass_wavelength("L1", 0)
