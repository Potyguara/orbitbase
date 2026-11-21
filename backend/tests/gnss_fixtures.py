"""Geradores de arquivos GNSS sintéticos para os testes."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

GPS_EPOCH = datetime(1980, 1, 6)

# Efeméride "limpa": órbita circular sem perturbações -> posição analítica.
BASE_RECORD = {
    "af0": 1.25e-4,
    "af1": 0.0,
    "af2": 0.0,
    "iode": 10.0,
    "crs": 0.0,
    "delta_n": 0.0,
    "m0": 0.5,
    "cuc": 0.0,
    "ecc": 0.0,
    "cus": 0.0,
    "sqrt_a": 5153.7,
    "cic": 0.0,
    "omega0": 1.2,
    "cis": 0.0,
    "i0": 0.96,
    "crc": 0.0,
    "w": 0.3,
    "omega_dot": 0.0,
    "idot": 0.0,
    "codes_l2": 1.0,
    "sv_accuracy": 2.0,
    "sv_health": 0.0,
    "tgd": 0.0,
    "iodc": 10.0,
    "transmission_time": 0.0,
    "fit_interval": 4.0,
}


def gps_week_sow(dt: datetime) -> tuple[int, float]:
    total = (dt - GPS_EPOCH).total_seconds()
    week = int(total // 604800)
    return week, total - week * 604800


def _d19(value: float) -> str:
    """Formata como D19.12 do RINEX (ex.: ' 1.250000000000D-04')."""
    if value == 0:
        mantissa, exp = 0.0, 0
    else:
        exp = int(math.floor(math.log10(abs(value)))) + 1
        mantissa = value / (10**exp)
    text = f"{mantissa:.12f}".replace("0.", ".", 1) if mantissa >= 0 else f"{mantissa:.12f}".replace("-0.", "-.", 1)
    out = f"{text}D{exp:+03d}"
    return out.rjust(19)


def _record_rows(rec: dict) -> list[list[float]]:
    return [
        [rec["iode"], rec["crs"], rec["delta_n"], rec["m0"]],
        [rec["cuc"], rec["ecc"], rec["cus"], rec["sqrt_a"]],
        [rec["toe"], rec["cic"], rec["omega0"], rec["cis"]],
        [rec["i0"], rec["crc"], rec["w"], rec["omega_dot"]],
        [rec["idot"], rec["codes_l2"], rec["gps_week"], 0.0],
        [rec["sv_accuracy"], rec["sv_health"], rec["tgd"], rec["iodc"]],
        [rec["transmission_time"], rec["fit_interval"]],
    ]


def make_record(toc: datetime, **overrides) -> dict:
    week, sow = gps_week_sow(toc)
    rec = dict(BASE_RECORD)
    rec["toe"] = sow
    rec["gps_week"] = float(week)
    rec["toc"] = toc
    rec.update(overrides)
    return rec


def write_rinex2_nav(path: Path, entries: list[tuple[int, datetime, dict]]) -> Path:
    lines = [
        "     2.11           N: GPS NAV DATA                         RINEX VERSION / TYPE",
        "OrbitBase test      pytest              20260101 000000 UTC PGM / RUN BY / DATE",
        "                                                            END OF HEADER",
    ]
    for prn, toc, rec in entries:
        head = (
            f"{prn:2d} {toc.year % 100:02d} {toc.month:2d} {toc.day:2d} "
            f"{toc.hour:2d} {toc.minute:2d}{toc.second:5.1f}"
        )
        lines.append(head + _d19(rec["af0"]) + _d19(rec["af1"]) + _d19(rec["af2"]))
        for row in _record_rows(rec):
            lines.append("   " + "".join(_d19(v) for v in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_rinex3_nav(path: Path, entries: list[tuple[int, datetime, dict]]) -> Path:
    lines = [
        "     3.04           N: GNSS NAV DATA    M: Mixed            RINEX VERSION / TYPE",
        "OrbitBase test      pytest              20260101 000000 UTC PGM / RUN BY / DATE",
        "                                                            END OF HEADER",
    ]
    for prn, toc, rec in entries:
        head = (
            f"G{prn:02d} {toc.year:04d} {toc.month:02d} {toc.day:02d} "
            f"{toc.hour:02d} {toc.minute:02d} {toc.second:02d}"
        )
        lines.append(head + _d19(rec["af0"]) + _d19(rec["af1"]) + _d19(rec["af2"]))
        for row in _record_rows(rec):
            lines.append("    " + "".join(_d19(v) for v in row))
    # Um registro GLONASS (4 linhas) no meio para garantir que é ignorado.
    lines.append("R01 2026 06 03 12 15 00" + _d19(1e-5) + _d19(0.0) + _d19(0.0))
    for _ in range(3):
        lines.append("    " + _d19(1.0) * 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def analytic_circular_position(rec: dict, t: datetime) -> tuple[float, float, float]:
    """Posição ECEF exata de uma órbita circular sem perturbações (IS-GPS-200)."""
    mu = 3.986005e14
    we = 7.2921151467e-5
    week, sow = gps_week_sow(t)
    toe_abs = rec["gps_week"] * 604800 + rec["toe"]
    tk = week * 604800 + sow - toe_abs
    a = rec["sqrt_a"] ** 2
    n = math.sqrt(mu / a**3)
    u = rec["m0"] + n * tk + rec["w"]
    xp, yp = a * math.cos(u), a * math.sin(u)
    om = rec["omega0"] - we * tk - we * rec["toe"]
    i = rec["i0"]
    return (
        xp * math.cos(om) - yp * math.cos(i) * math.sin(om),
        xp * math.sin(om) + yp * math.cos(i) * math.cos(om),
        yp * math.sin(i),
    )


def write_sp3(path: Path, start: datetime, count: int, step_s: int, sats: dict) -> Path:
    """sats: {"G01": func(datetime)->(x,y,z) em metros}."""
    lines = [
        f"#dP{start.year:4d} {start.month:2d} {start.day:2d} {start.hour:2d} {start.minute:2d} {start.second:11.8f} {count:7d} ORBIT IGS20 HLM  IGS",
        "## 2400      0.00000000   900.00000000 61000 0.0000000000000",
        "+    1   G01  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0",
        "/* synthetic",
    ]
    for k in range(count):
        t = start + timedelta(seconds=k * step_s)
        lines.append(
            f"*  {t.year:4d} {t.month:2d} {t.day:2d} {t.hour:2d} {t.minute:2d} {t.second:11.8f}"
        )
        for sat, fn in sats.items():
            x, y, z = fn(t)
            lines.append(
                f"P{sat}{x / 1000:14.6f}{y / 1000:14.6f}{z / 1000:14.6f}{12.345678:14.6f}"
            )
    lines.append("EOF")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
