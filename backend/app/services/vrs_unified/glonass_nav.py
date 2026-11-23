"""Efemérides transmitidas GLONASS: leitura (RINEX 2 .g e RINEX 3) e órbita.

A órbita segue o ICD GLONASS: integração Runge-Kutta 4 das equações do
movimento no referencial PZ-90 (que gira com a Terra), com J2 e as acelerações
luni-solares constantes da mensagem. PZ-90.11 e ITRF diferem em poucos cm,
o que é desprezível no transporte mestra -> alvo (o erro de órbita entra
multiplicado por linha de base / distância ao satélite).

Tempos: as efemérides vêm em UTC; as observações RINEX estão em tempo GPS.
"""

from __future__ import annotations

import gzip
import math
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

MU = 398600.4418e9
J2 = 1.08262575e-3
AE = 6378136.0
OMEGA = 7.292115e-5
GPS_UTC_LEAP_SECONDS = 18
RK_STEP_SECONDS = 10.0
# efemérides GLONASS valem ~±15 min em torno de toc
MAX_EPHEMERIS_AGE_SECONDS = 20 * 60

L1_BASE_HZ = 1602.0e6
L1_STEP_HZ = 0.5625e6
L2_BASE_HZ = 1246.0e6
L2_STEP_HZ = 0.4375e6
C_LIGHT = 299792458.0


def glonass_wavelength(obs_code: str, channel: int | None) -> float | None:
    if channel is None:
        return None
    if obs_code == "L1":
        return C_LIGHT / (L1_BASE_HZ + channel * L1_STEP_HZ)
    if obs_code == "L2":
        return C_LIGHT / (L2_BASE_HZ + channel * L2_STEP_HZ)
    return None


def _num(text: str) -> float | None:
    text = text.strip().replace("D", "E").replace("d", "E")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_lines(path: str) -> list[str]:
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="ascii", errors="ignore") as fh:
        return fh.read().splitlines()


def _values(line: str, start: int, count: int = 4) -> list[float | None]:
    return [_num(line[start + 19 * i: start + 19 * (i + 1)]) for i in range(count)]


def _record(sat: str, toc: datetime, first: list, lines: list[str], start: int) -> dict | None:
    if len(lines) < 3:
        return None
    x, vx, ax, health = _values(lines[0], start)
    y, vy, ay, channel = _values(lines[1], start)
    z, vz, az, _age = _values(lines[2], start)
    if None in (x, y, z, vx, vy, vz):
        return None
    return {
        "satellite": sat,
        "toc": toc,
        "tau_n": -(first[0] or 0.0),
        "gamma_n": first[1] or 0.0,
        "pos": (x * 1000.0, y * 1000.0, z * 1000.0),
        "vel": (vx * 1000.0, vy * 1000.0, vz * 1000.0),
        "acc": ((ax or 0.0) * 1000.0, (ay or 0.0) * 1000.0, (az or 0.0) * 1000.0),
        "health": int(health or 0),
        "channel": int(channel) if channel is not None else None,
    }


def parse_glonass_nav(path: str) -> dict[str, list[dict]]:
    """{'R05': [efemérides ordenadas por toc]} de um arquivo RINEX 2 (.g) ou 3."""
    lines = _read_lines(path)
    version = 2.0
    body = 0
    for i, line in enumerate(lines):
        label = line[60:].strip()
        if label == "RINEX VERSION / TYPE":
            try:
                version = float(line[:9])
            except ValueError:
                pass
        if label == "END OF HEADER":
            body = i + 1
            break

    out: dict[str, list[dict]] = {}
    i = body
    while i < len(lines):
        line = lines[i]
        if version >= 3:
            if not line.startswith("R"):
                i += 1
                continue
            try:
                sat = f"R{int(line[1:3]):02d}"
                toc = datetime(int(line[4:8]), int(line[9:11]), int(line[12:14]),
                               int(line[15:17]), int(line[18:20]), int(line[21:23]))
            except ValueError:
                i += 1
                continue
            first = _values(line, 23, 3)
            rec = _record(sat, toc, first, lines[i + 1:i + 4], 4)
        else:
            if not line[:2].strip().isdigit():
                i += 1
                continue
            try:
                sat = f"R{int(line[:2]):02d}"
                yy = int(line[2:5])
                sec = float(line[17:22])
                toc = datetime(2000 + yy if yy < 80 else 1900 + yy, int(line[5:8]),
                               int(line[8:11]), int(line[11:14]), int(line[14:17]),
                               int(sec))
            except ValueError:
                i += 1
                continue
            first = _values(line, 22, 3)
            rec = _record(sat, toc, first, lines[i + 1:i + 4], 3)
        if rec:
            out.setdefault(sat, []).append(rec)
        i += 4
    for recs in out.values():
        recs.sort(key=lambda r: r["toc"])
    return out


def merge_glonass_nav(paths: list[str]) -> dict[str, list[dict]]:
    merged: dict[str, dict[datetime, dict]] = {}
    for p in paths:
        try:
            recs = parse_glonass_nav(p)
        except Exception:  # noqa: BLE001
            continue
        for sat, items in recs.items():
            for r in items:
                merged.setdefault(sat, {})[r["toc"]] = r
    return {s: [v[k] for k in sorted(v)] for s, v in merged.items()}


def _deriv(state: list[float], acc: tuple[float, float, float]) -> list[float]:
    x, y, z, vx, vy, vz = state
    r2 = x * x + y * y + z * z
    r = math.sqrt(r2)
    r3 = r2 * r
    r5 = r3 * r2
    k = 1.5 * J2 * MU * AE * AE / r5
    z2 = 5.0 * z * z / r2
    w2 = OMEGA * OMEGA
    ax = -MU / r3 * x - k * x * (1.0 - z2) + w2 * x + 2.0 * OMEGA * vy + acc[0]
    ay = -MU / r3 * y - k * y * (1.0 - z2) + w2 * y - 2.0 * OMEGA * vx + acc[1]
    az = -MU / r3 * z - k * z * (3.0 - z2) + acc[2]
    return [vx, vy, vz, ax, ay, az]


def _rk4(state: list[float], acc, h: float) -> list[float]:
    k1 = _deriv(state, acc)
    k2 = _deriv([s + 0.5 * h * d for s, d in zip(state, k1)], acc)
    k3 = _deriv([s + 0.5 * h * d for s, d in zip(state, k2)], acc)
    k4 = _deriv([s + h * d for s, d in zip(state, k3)], acc)
    return [s + h / 6.0 * (a + 2 * b + 2 * c + d)
            for s, a, b, c, d in zip(state, k1, k2, k3, k4)]


def propagate(state: list[float], acc, seconds: float) -> list[float]:
    if seconds == 0:
        return list(state)
    n = max(1, int(math.ceil(abs(seconds) / RK_STEP_SECONDS)))
    h = seconds / n
    for _ in range(n):
        state = _rk4(state, acc, h)
    return state


def _nearest(recs: list[dict], t_utc: datetime, tocs: list[datetime] | None = None) -> dict | None:
    if tocs is not None:
        i = bisect_left(tocs, t_utc)
        recs = recs[max(0, i - 3): i + 3]
    best, best_age = None, None
    for r in recs:
        if r["health"] != 0:
            continue
        age = abs((t_utc - r["toc"]).total_seconds())
        if best_age is None or age < best_age:
            best, best_age = r, age
    if best is None or best_age > MAX_EPHEMERIS_AGE_SECONDS:
        return None
    return best


def glonass_positions(records: dict[str, list[dict]], times_gps: list[datetime]) -> dict:
    """{tempo_gps: {sat: {x, y, z, vx, vy, vz}}} para tempos em ordem crescente.

    Integra de forma incremental: a cada tempo parte do estado anterior da mesma
    efeméride, então uma janela de 1 s custa um passo RK por segundo.
    """
    out: dict[datetime, dict] = {t: {} for t in times_gps}
    leap = timedelta(seconds=GPS_UTC_LEAP_SECONDS)
    for sat, recs in records.items():
        current = None
        state = None
        state_t = None
        tocs = [r["toc"] for r in recs]
        for t in times_gps:
            t_utc = t - leap
            eph = _nearest(recs, t_utc, tocs)
            if eph is None:
                current = None
                continue
            if eph is not current:
                current = eph
                state = list(eph["pos"]) + list(eph["vel"])
                state_t = eph["toc"]
            state = propagate(state, eph["acc"], (t_utc - state_t).total_seconds())
            state_t = t_utc
            out[t][sat] = {"x": state[0], "y": state[1], "z": state[2],
                           "vx": state[3], "vy": state[4], "vz": state[5]}
    return out


def glonass_channels(records: dict[str, list[dict]]) -> dict[str, int]:
    out = {}
    for sat, recs in records.items():
        for r in recs:
            if r.get("channel") is not None:
                out[sat] = r["channel"]
    return out
