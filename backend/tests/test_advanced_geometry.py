import math
from datetime import datetime, timedelta, timezone

import pytest

from app.services.vrs_advanced import advanced_geometry_service as geo
from tests.gnss_fixtures import (
    analytic_circular_position,
    make_record,
    write_rinex2_nav,
    write_sp3,
)

TOC = datetime(2026, 6, 3, 12, 0, 0)


def _grid(start, count, step_s=30):
    return [
        (start + timedelta(seconds=k * step_s)).replace(tzinfo=timezone.utc)
        for k in range(count)
    ]


def test_nav_fallback_produces_geometry_when_sp3_missing(tmp_path):
    rec = make_record(TOC)
    nav = write_rinex2_nav(tmp_path / "aplj1541.26n", [(1, TOC, rec), (9, TOC, rec)])
    grid = _grid(TOC, 20)

    cache = geo.build_precise_geometry_cache(
        time_grid=grid,
        precise_products={"sp3_available": False, "sp3_path": None},
        navigation_context=[{"station_code": "APLJ", "nav_path": str(nav)}],
    )

    assert cache["status"] == "completed"
    assert cache["source"] == "broadcast_nav_fallback"
    assert cache["epochs_with_geometry"] == len(grid)
    first = cache["epochs"][0]
    assert first["epoch"] == "2026-06-03T12:00:00Z"
    g01 = first["satellites"]["G01"]
    expected = analytic_circular_position(rec, TOC)
    assert math.dist((g01["x"], g01["y"], g01["z"]), expected) < 1e-3
    assert "vx" in g01


def test_geometry_unavailable_has_actionable_message():
    cache = geo.build_precise_geometry_cache(
        time_grid=_grid(TOC, 3),
        precise_products={
            "sp3_available": False,
            "sp3_path": None,
            "download_errors": [
                "Resposta HTML/login em vez de SP3 :: https://cddis.nasa.gov/x",
                "Request error (timeout/conexão) :: https://igs.ign.fr/x :: timed out",
            ],
        },
        navigation_context=[],
    )

    assert cache["status"] == "geometry_unavailable"
    assert cache["epochs_with_geometry"] == 0
    msg = cache["message"]
    assert "Geometria orbital indisponível" in msg
    assert "HTML/login" in msg
    assert "timeout" in msg
    assert ".yyN" in msg


def _circular_orbit(t):
    # Órbita circular inercial simples (período ~12 h) — suave, como GPS.
    a = 26_560_000.0
    n = 2 * math.pi / 43082.0
    s = (t - datetime(2026, 6, 3)).total_seconds()
    return (a * math.cos(n * s), a * math.sin(n * s) * 0.6, a * math.sin(n * s) * 0.8)


def test_sp3_lagrange_interpolation_is_sub_centimetre(tmp_path):
    start = datetime(2026, 6, 3)
    sp3 = write_sp3(tmp_path / "igs.sp3", start, 96, 900, {"G01": _circular_orbit})
    target = datetime(2026, 6, 3, 12, 7, 30)  # no meio entre dois nós

    cache = geo.build_precise_geometry_cache(
        time_grid=[target.replace(tzinfo=timezone.utc)],
        precise_products={"sp3_available": True, "sp3_path": str(sp3)},
        navigation_context=[],
    )

    assert cache["source"] == "sp3_lagrange_interpolation"
    pos = cache["epochs"][0]["satellites"]["G01"]
    err = math.dist((pos["x"], pos["y"], pos["z"]), _circular_orbit(target))
    # O método linear antigo errava ~20 km aqui; SP3 tem resolução de 1 mm.
    assert err < 0.01
    assert pos["clock_us"] == pytest.approx(12.345678)
    assert math.sqrt(pos["vx"] ** 2 + pos["vy"] ** 2 + pos["vz"] ** 2) == pytest.approx(
        26_560_000.0 * 2 * math.pi / 43082.0, rel=1e-3
    )


def test_sp3_bad_clock_and_missing_position_are_discarded():
    assert geo._parse_sp3_position_line(
        "PG01      0.000000      0.000000      0.000000 999999.999999"
    ) is None
    sat, pos = geo._parse_sp3_position_line(
        "PG 2  15000.000000  20000.000000   5000.000000 999999.999999"
    )
    assert sat == "G02"
    assert pos["clock_us"] is None
    assert pos["x"] == pytest.approx(15_000_000.0)


def test_partial_sp3_falls_back_to_nav_when_nav_covers_more(tmp_path):
    rec = make_record(TOC)
    nav = write_rinex2_nav(tmp_path / "aplj1541.26n", [(1, TOC, rec)])
    # SP3 termina antes da janela pedida.
    sp3 = write_sp3(
        tmp_path / "igs.sp3", datetime(2026, 6, 3, 9), 12, 900, {"G01": _circular_orbit}
    )
    cache = geo.build_precise_geometry_cache(
        time_grid=_grid(TOC, 10),
        precise_products={"sp3_available": True, "sp3_path": str(sp3)},
        navigation_context=[{"nav_path": str(nav)}],
    )
    assert cache["source"] == "broadcast_nav_fallback"
    assert cache["epochs_with_geometry"] == 10
