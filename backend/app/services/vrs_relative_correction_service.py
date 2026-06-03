from __future__ import annotations

from datetime import datetime
import math

from pathlib import Path
from datetime import timedelta

GPS_MU = 3.986005e14
GPS_OMEGA_E_DOT = 7.2921151467e-5
GPS_F = -4.442807633e-10
SPEED_OF_LIGHT = 299792458.0

GPS_L1_HZ = 1575.42e6
GPS_L2_HZ = 1227.60e6
GPS_L5_HZ = 1176.45e6

GPS_WAVELENGTHS = {
    "L1": SPEED_OF_LIGHT / GPS_L1_HZ,
    "L2": SPEED_OF_LIGHT / GPS_L2_HZ,
    "L5": SPEED_OF_LIGHT / GPS_L5_HZ,
}

SUPPORTED_OBS_PRIORITY = [
    "C1",
    "P1",
    "L1",
    "C2",
    "P2",
    "L2",
    "C5",
    "L5",
    "S1",
    "S2",
    "S5",
]

DEFAULT_PRIORITY_OBS = [
    "C1",
    "P1",
    "L1",
    "C2",
    "P2",
    "L2",
    "C5",
    "L5",
    "S1",
    "S2",
    "S5",
]


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _count_non_null(observations: dict, obs_names: list[str] | None = None) -> int:
    if obs_names is None:
        return sum(1 for value in (observations or {}).values() if value is not None)
    return sum(
        1
        for obs_name in obs_names
        if _safe_float((observations or {}).get(obs_name)) is not None
    )


def _index_epochs(numeric_data: dict) -> dict[str, dict]:
    return {epoch["epoch"]: epoch for epoch in numeric_data.get("epochs", [])}


def _index_satellite_records(epoch_record: dict) -> dict[str, dict]:
    sats = epoch_record.get("satellite_records", []) or []
    return {sat["satellite"]: sat for sat in sats}


def _normalize_satellite_id(sat: str) -> str:
    sat = (sat or "").strip().upper()
    if not sat:
        return sat

    if len(sat) >= 2 and sat[0] in {"G", "R", "E", "C"}:
        prefix = sat[0]
        suffix = sat[1:].strip()
        try:
            return f"{prefix}{int(suffix):02d}"
        except Exception:
            return sat

    return sat


def _get_satellite_system(sat: str) -> str:
    sat = _normalize_satellite_id(sat)
    if sat and sat[0] in {"G", "R", "E", "C"}:
        return sat[0]
    return "G"


def _extract_epoch_datetime(epoch_iso: str) -> datetime:
    return datetime.fromisoformat(epoch_iso)


def _estimate_interval_seconds(epochs: list[dict]) -> float:
    if len(epochs) < 2:
        return 15.0

    try:
        dt1 = _extract_epoch_datetime(epochs[0]["epoch"])
        dt2 = _extract_epoch_datetime(epochs[1]["epoch"])
        delta = (dt2 - dt1).total_seconds()
        if delta > 0:
            return float(delta)
    except Exception:
        pass

    return 15.0


def _common_observation_types(
    master_numeric: dict,
    aux1_numeric: dict,
    aux2_numeric: dict,
) -> list[str]:
    obs1 = set(master_numeric.get("observation_types", []) or [])
    obs2 = set(aux1_numeric.get("observation_types", []) or [])
    obs3 = set(aux2_numeric.get("observation_types", []) or [])

    common = obs1 & obs2 & obs3
    ordered = [obs for obs in SUPPORTED_OBS_PRIORITY if obs in common]
    extra = sorted(common - set(ordered))
    return ordered + extra


def _has_required_dual_frequency(obs: dict) -> bool:
    obs = obs or {}

    has_l1 = _safe_float(obs.get("L1")) is not None
    has_l2 = _safe_float(obs.get("L2")) is not None

    has_code_1 = (
        _safe_float(obs.get("C1")) is not None or _safe_float(obs.get("P1")) is not None
    )
    has_code_2 = (
        _safe_float(obs.get("C2")) is not None or _safe_float(obs.get("P2")) is not None
    )

    return has_l1 and has_l2 and has_code_1 and has_code_2


def _geodetic_to_ecef(
    lat_deg: float, lon_deg: float, h_m: float
) -> tuple[float, float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)

    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)

    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)

    x = (n + h_m) * cos_lat * cos_lon
    y = (n + h_m) * cos_lat * sin_lon
    z = (n * (1 - e2) + h_m) * sin_lat

    return x, y, z


def _ecef_to_geodetic(
    x: float,
    y: float,
    z: float,
) -> tuple[float, float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    b = a * (1 - f)
    ep2 = (a * a - b * b) / (b * b)

    p = math.sqrt(x * x + y * y)
    lon = math.atan2(y, x)

    th = math.atan2(a * z, b * p)
    sin_th = math.sin(th)
    cos_th = math.cos(th)

    lat = math.atan2(
        z + ep2 * b * sin_th**3,
        p - e2 * a * cos_th**3,
    )

    sin_lat = math.sin(lat)
    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    h = p / math.cos(lat) - n

    return math.degrees(lat), math.degrees(lon), h


def _ecef_vector_from_dict(data: dict) -> tuple[float, float, float]:
    return (
        float(data.get("x", 0.0) or 0.0),
        float(data.get("y", 0.0) or 0.0),
        float(data.get("z", 0.0) or 0.0),
    )


def _vector_sub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vector_add(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vector_scale(
    a: tuple[float, float, float], s: float
) -> tuple[float, float, float]:
    return (a[0] * s, a[1] * s, a[2] * s)


def _vector_norm(a: tuple[float, float, float]) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _unit_vector(a: tuple[float, float, float]) -> tuple[float, float, float]:
    n = _vector_norm(a)
    if n <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


def _ecef_delta_to_enu(
    dx: float,
    dy: float,
    dz: float,
    ref_lat_deg: float,
    ref_lon_deg: float,
) -> tuple[float, float, float]:
    lat = math.radians(ref_lat_deg)
    lon = math.radians(ref_lon_deg)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)

    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

    return east, north, up


def _build_target_xyz(target_meta: dict) -> tuple[float, float, float]:
    x = _safe_float(target_meta.get("x"))
    y = _safe_float(target_meta.get("y"))
    z = _safe_float(target_meta.get("z"))

    if x is not None and y is not None and z is not None:
        return x, y, z

    lat = _safe_float(target_meta.get("lat"))
    lon = _safe_float(target_meta.get("lon"))
    height = _safe_float(target_meta.get("height"))

    if lat is None or lon is None or height is None:
        raise ValueError("target_meta sem coordenadas suficientes para o alvo.")

    return _geodetic_to_ecef(lat, lon, height)


def _distance_weight(distance_m: float, power: float = 1.2) -> float:
    d = max(distance_m, 1.0)
    return 1.0 / (d**power)


def _extract_station_xyz(target_meta: dict, key: str) -> tuple[float, float, float]:
    data = target_meta.get(key) or {}
    xyz = _ecef_vector_from_dict(data)
    if _vector_norm(xyz) > 0.0:
        return xyz
    raise ValueError(f"{key} sem XYZ válido em target_meta.")


def _extract_satellite_xyz(sat_record: dict) -> tuple[float, float, float] | None:
    for key in ["satellite_xyz", "sat_xyz", "xyz"]:
        value = sat_record.get(key)
        if isinstance(value, dict):
            x = _safe_float(value.get("x"))
            y = _safe_float(value.get("y"))
            z = _safe_float(value.get("z"))
            if x is not None and y is not None and z is not None:
                return (x, y, z)

    x = _safe_float(sat_record.get("sat_x"))
    y = _safe_float(sat_record.get("sat_y"))
    z = _safe_float(sat_record.get("sat_z"))
    if x is not None and y is not None and z is not None:
        return (x, y, z)

    return None


def _geometric_range(
    receiver_xyz: tuple[float, float, float],
    satellite_xyz: tuple[float, float, float],
) -> float:
    return _vector_norm(_vector_sub(satellite_xyz, receiver_xyz))


def _line_of_sight(
    receiver_xyz: tuple[float, float, float],
    satellite_xyz: tuple[float, float, float],
) -> tuple[float, float, float]:
    return _unit_vector(_vector_sub(satellite_xyz, receiver_xyz))


def _project_baseline_on_los(
    baseline_xyz: tuple[float, float, float],
    los_unit: tuple[float, float, float],
) -> float:
    return (
        baseline_xyz[0] * los_unit[0]
        + baseline_xyz[1] * los_unit[1]
        + baseline_xyz[2] * los_unit[2]
    )


def _obs_frequency_scale(obs_name: str) -> float:
    obs_name = (obs_name or "").upper()

    if obs_name in {"C1", "P1", "C2", "P2", "C5"}:
        return 1.0

    if obs_name in {"L1", "L2", "L5"}:
        return 1.0

    if obs_name in {"S1", "S2", "S5"}:
        return 0.0

    return 1.0


def _nav_float(value: str) -> float:
    text = (value or "").strip().replace("D", "E").replace("d", "E")
    if not text:
        return 0.0
    return float(text)


def _parse_nav_epoch_2digit_year(
    yy: int,
    mm: int,
    dd: int,
    hh: int,
    mi: int,
    ss: float,
) -> datetime:
    year = 2000 + yy if yy < 80 else 1900 + yy
    sec_int = int(ss)
    micro = int(round((ss - sec_int) * 1_000_000))
    if micro >= 1_000_000:
        sec_int += 1
        micro -= 1_000_000

    return datetime(year, mm, dd, hh, mi, sec_int, micro)


def _split_nav_fields(line: str, start: int) -> list[str]:
    if not line or len(line) <= start:
        return []

    payload = line[start:]
    fields = []
    for i in range(0, len(payload), 19):
        chunk = payload[i : i + 19]
        if chunk.strip():
            fields.append(chunk)
    return fields


def _datetime_to_gps_week_seconds(dt: datetime) -> tuple[int, float]:
    gps_epoch = datetime(1980, 1, 6, 0, 0, 0)
    delta = dt - gps_epoch
    total_seconds = delta.total_seconds()
    gps_week = int(total_seconds // 604800)
    sow = total_seconds - gps_week * 604800
    return gps_week, sow


def _normalize_gps_time_diff(seconds: float) -> float:
    while seconds > 302400.0:
        seconds -= 604800.0
    while seconds < -302400.0:
        seconds += 604800.0
    return seconds


def _solve_kepler(mk: float, ecc: float, max_iter: int = 20) -> float:
    ek = mk
    for _ in range(max_iter):
        ek_next = mk + ecc * math.sin(ek)
        if abs(ek_next - ek) < 1e-12:
            ek = ek_next
            break
        ek = ek_next
    return ek


def _is_code_obs(obs_name: str) -> bool:
    return (obs_name or "").upper().startswith(("C", "P"))


def _is_phase_obs(obs_name: str) -> bool:
    return (obs_name or "").upper().startswith("L")


def _is_snr_obs(obs_name: str) -> bool:
    return (obs_name or "").upper().startswith("S")


def _phase_wavelength_m(obs_name: str, sat_id: str | None = None) -> float | None:
    obs = (obs_name or "").upper()

    if obs in GPS_WAVELENGTHS:
        return GPS_WAVELENGTHS[obs]

    return None


def _obs_to_meters(
    obs_name: str, value: float | None, sat_id: str | None = None
) -> float | None:
    v = _safe_float(value)
    if v is None:
        return None

    if _is_code_obs(obs_name):
        return v

    if _is_phase_obs(obs_name):
        wl = _phase_wavelength_m(obs_name, sat_id=sat_id)
        if wl is None:
            return None
        return v * wl

    return v


def _meters_to_obs_units(
    obs_name: str, value_m: float | None, sat_id: str | None = None
) -> float | None:
    v = _safe_float(value_m)
    if v is None:
        return None

    if _is_code_obs(obs_name):
        return v

    if _is_phase_obs(obs_name):
        wl = _phase_wavelength_m(obs_name, sat_id=sat_id)
        if wl is None or abs(wl) < 1e-12:
            return None
        return v / wl

    return v


def _interpolate_residual(
    residual_aux1: float | None,
    residual_aux2: float | None,
    weight_aux1: float,
    weight_aux2: float,
) -> float:
    vals = []
    ws = []

    r1 = _safe_float(residual_aux1)
    r2 = _safe_float(residual_aux2)

    if r1 is not None:
        vals.append(r1)
        ws.append(weight_aux1)

    if r2 is not None:
        vals.append(r2)
        ws.append(weight_aux2)

    if not vals:
        return 0.0

    sw = sum(ws)
    if sw <= 0:
        return sum(vals) / len(vals)

    return sum(v * w for v, w in zip(vals, ws)) / sw


def _extract_gps_nav_blocks(nav_file_path: str) -> list[dict]:
    lines = (
        Path(nav_file_path)
        .read_text(
            encoding="utf-8",
            errors="ignore",
        )
        .splitlines()
    )

    end_header_idx = None
    for idx, line in enumerate(lines):
        if "END OF HEADER" in line:
            end_header_idx = idx
            break

    if end_header_idx is None:
        raise ValueError(f"Arquivo NAV sem END OF HEADER: {nav_file_path}")

    data_lines = lines[end_header_idx + 1 :]

    print("[DEBUG-NAV-PARSE] ===== INÍCIO =====", flush=True)
    print(f"[DEBUG-NAV-PARSE] nav_file={nav_file_path}", flush=True)
    print(f"[DEBUG-NAV-PARSE] total_lines={len(lines)}", flush=True)
    print(f"[DEBUG-NAV-PARSE] data_lines={len(data_lines)}", flush=True)

    blocks: list[dict] = []
    i = 0

    while i < len(data_lines):
        line = data_lines[i]

        if not line.strip():
            i += 1
            continue

        if i + 7 >= len(data_lines):
            break

        raw_prn = line[0:2].strip()
        if not raw_prn.isdigit():
            i += 1
            continue

        sat_id = f"G{int(raw_prn):02d}"
        block_lines = data_lines[i : i + 8]

        try:
            yy = int(line[2:5].strip())
            mm = int(line[5:8].strip())
            dd = int(line[8:11].strip())
            hh = int(line[11:14].strip())
            mi = int(line[14:17].strip())
            ss = float(line[17:22].strip().replace("D", "E"))

            toc = _parse_nav_epoch_2digit_year(yy, mm, dd, hh, mi, ss)

            row0 = _split_nav_fields(block_lines[0], start=22)
            row1 = _split_nav_fields(block_lines[1], start=3)
            row2 = _split_nav_fields(block_lines[2], start=3)
            row3 = _split_nav_fields(block_lines[3], start=3)
            row4 = _split_nav_fields(block_lines[4], start=3)
            row5 = _split_nav_fields(block_lines[5], start=3)
            row6 = _split_nav_fields(block_lines[6], start=3)
            row7 = _split_nav_fields(block_lines[7], start=3)

            eph = {
                "satellite": sat_id,
                "toc": toc,
                "af0": _nav_float(row0[0]) if len(row0) > 0 else 0.0,
                "af1": _nav_float(row0[1]) if len(row0) > 1 else 0.0,
                "af2": _nav_float(row0[2]) if len(row0) > 2 else 0.0,
                "iode": _nav_float(row1[0]) if len(row1) > 0 else 0.0,
                "crs": _nav_float(row1[1]) if len(row1) > 1 else 0.0,
                "delta_n": _nav_float(row1[2]) if len(row1) > 2 else 0.0,
                "m0": _nav_float(row1[3]) if len(row1) > 3 else 0.0,
                "cuc": _nav_float(row2[0]) if len(row2) > 0 else 0.0,
                "ecc": _nav_float(row2[1]) if len(row2) > 1 else 0.0,
                "cus": _nav_float(row2[2]) if len(row2) > 2 else 0.0,
                "sqrt_a": _nav_float(row2[3]) if len(row2) > 3 else 0.0,
                "toe": _nav_float(row3[0]) if len(row3) > 0 else 0.0,
                "cic": _nav_float(row3[1]) if len(row3) > 1 else 0.0,
                "omega0": _nav_float(row3[2]) if len(row3) > 2 else 0.0,
                "cis": _nav_float(row3[3]) if len(row3) > 3 else 0.0,
                "i0": _nav_float(row4[0]) if len(row4) > 0 else 0.0,
                "crc": _nav_float(row4[1]) if len(row4) > 1 else 0.0,
                "w": _nav_float(row4[2]) if len(row4) > 2 else 0.0,
                "omega_dot": _nav_float(row4[3]) if len(row4) > 3 else 0.0,
                "idot": _nav_float(row5[0]) if len(row5) > 0 else 0.0,
                "codes_l2": _nav_float(row5[1]) if len(row5) > 1 else 0.0,
                "gps_week": _nav_float(row5[2]) if len(row5) > 2 else 0.0,
                "l2_p_flag": _nav_float(row5[3]) if len(row5) > 3 else 0.0,
                "sv_accuracy": _nav_float(row6[0]) if len(row6) > 0 else 0.0,
                "sv_health": _nav_float(row6[1]) if len(row6) > 1 else 0.0,
                "tgd": _nav_float(row6[2]) if len(row6) > 2 else 0.0,
                "iodc": _nav_float(row6[3]) if len(row6) > 3 else 0.0,
                "transmission_time": _nav_float(row7[0]) if len(row7) > 0 else 0.0,
                "fit_interval": _nav_float(row7[1]) if len(row7) > 1 else 0.0,
            }

            blocks.append(eph)

        except Exception as exc:
            print(
                f"[DEBUG-NAV-PARSE] bloco ignorado sat={sat_id} erro={type(exc).__name__}: {exc} | line={line}",
                flush=True,
            )

        i += 8

    print(f"[DEBUG-NAV-PARSE] gps_blocks={len(blocks)}", flush=True)
    if blocks:
        print(
            f"[DEBUG-NAV-PARSE] first_block_sat={blocks[0]['satellite']}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV-PARSE] first_block_toc={blocks[0]['toc'].isoformat()}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV-PARSE] first_block_toe={blocks[0]['toe']}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV-PARSE] first_block_sqrt_a={blocks[0]['sqrt_a']}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV-PARSE] first_block_ecc={blocks[0]['ecc']}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV-PARSE] first_block_keys={list(blocks[0].keys())}",
            flush=True,
        )

    print("[DEBUG-NAV-PARSE] ===== FIM =====", flush=True)
    return blocks


def _select_best_ephemeris_for_epoch(
    ephemerides: list[dict],
    target_dt: datetime,
) -> dict | None:
    if not ephemerides:
        return None

    best = None
    best_abs_dt = None

    _, target_sow = _datetime_to_gps_week_seconds(target_dt)

    for eph in ephemerides:
        toe = float(eph.get("toe", 0.0) or 0.0)
        dt = _normalize_gps_time_diff(target_sow - toe)
        abs_dt = abs(dt)

        if best is None or abs_dt < best_abs_dt:
            best = eph
            best_abs_dt = abs_dt

    return best


def _compute_gps_satellite_xyz_from_broadcast(
    eph: dict,
    target_dt: datetime,
) -> tuple[float, float, float]:
    _, t = _datetime_to_gps_week_seconds(target_dt)

    toe = float(eph["toe"])
    tk = _normalize_gps_time_diff(t - toe)

    sqrt_a = float(eph["sqrt_a"])
    a = sqrt_a * sqrt_a
    if abs(a) < 1e-12:
        raise ValueError("Semieixo maior inválido no broadcast.")

    ecc = float(eph["ecc"])
    delta_n = float(eph["delta_n"])
    m0 = float(eph["m0"])
    cuc = float(eph["cuc"])
    cus = float(eph["cus"])
    crc = float(eph["crc"])
    crs = float(eph["crs"])
    cic = float(eph["cic"])
    cis = float(eph["cis"])
    i0 = float(eph["i0"])
    idot = float(eph["idot"])
    omega0 = float(eph["omega0"])
    omega_dot = float(eph["omega_dot"])
    w = float(eph["w"])

    n0 = math.sqrt(GPS_MU / (a**3))
    n = n0 + delta_n

    mk = m0 + n * tk
    ek = _solve_kepler(mk, ecc)

    denom = 1.0 - ecc * math.cos(ek)
    if abs(denom) < 1e-15:
        raise ValueError("Denominador degenerado na equação orbital.")

    sin_vk = (math.sqrt(max(0.0, 1.0 - ecc * ecc)) * math.sin(ek)) / denom
    cos_vk = (math.cos(ek) - ecc) / denom
    vk = math.atan2(sin_vk, cos_vk)

    phi_k = vk + w

    du = cuc * math.cos(2 * phi_k) + cus * math.sin(2 * phi_k)
    dr = crc * math.cos(2 * phi_k) + crs * math.sin(2 * phi_k)
    di = cic * math.cos(2 * phi_k) + cis * math.sin(2 * phi_k)

    u = phi_k + du
    r = a * denom + dr
    i = i0 + di + idot * tk

    x_orb = r * math.cos(u)
    y_orb = r * math.sin(u)

    omega_k = omega0 + (omega_dot - GPS_OMEGA_E_DOT) * tk - GPS_OMEGA_E_DOT * toe

    x = x_orb * math.cos(omega_k) - y_orb * math.cos(i) * math.sin(omega_k)
    y = x_orb * math.sin(omega_k) + y_orb * math.cos(i) * math.cos(omega_k)
    z = y_orb * math.sin(i)

    if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z):
        raise ValueError("XYZ satelital não finito.")

    return (x, y, z)


def build_satellite_xyz_cache_from_rinex_nav(
    nav_file_path: str,
    start_dt: datetime,
    end_dt: datetime,
    step_seconds: int = 15,
) -> dict:
    print("[DEBUG-NAV-CACHE] ===== INÍCIO =====", flush=True)
    print(f"[DEBUG-NAV-CACHE] nav_file_path={nav_file_path}", flush=True)
    print(f"[DEBUG-NAV-CACHE] start_dt={start_dt.isoformat()}", flush=True)
    print(f"[DEBUG-NAV-CACHE] end_dt={end_dt.isoformat()}", flush=True)
    print(f"[DEBUG-NAV-CACHE] step_seconds={step_seconds}", flush=True)

    gps_blocks = _extract_gps_nav_blocks(nav_file_path)

    print(f"[DEBUG-NAV-CACHE] gps_blocks_total={len(gps_blocks)}", flush=True)

    by_satellite: dict[str, list[dict]] = {}
    for eph in gps_blocks:
        sat = _normalize_satellite_id(eph["satellite"])
        by_satellite.setdefault(sat, []).append(eph)

    for sat in by_satellite:
        by_satellite[sat].sort(key=lambda item: item["toc"])

    print(
        f"[DEBUG-NAV-CACHE] satellites_in_nav_total={len(by_satellite)}",
        flush=True,
    )
    print(
        f"[DEBUG-NAV-CACHE] satellites_in_nav_sample={sorted(by_satellite.keys())[:20]}",
        flush=True,
    )

    cache: dict[str, dict] = {}
    current_dt = start_dt
    epoch_counter = 0
    xyz_counter = 0
    failure_counter = 0

    while current_dt <= end_dt:
        epoch_iso = current_dt.replace(microsecond=0).isoformat()
        epoch_cache: dict[str, dict] = {}

        for sat, eph_list in by_satellite.items():
            try:
                eph = _select_best_ephemeris_for_epoch(eph_list, current_dt)
                if eph is None:
                    continue

                x, y, z = _compute_gps_satellite_xyz_from_broadcast(eph, current_dt)

                epoch_cache[sat] = {
                    "x": float(x),
                    "y": float(y),
                    "z": float(z),
                    "toc": eph["toc"].isoformat(),
                    "toe": float(eph.get("toe", 0.0) or 0.0),
                }
                xyz_counter += 1

            except Exception as exc:
                failure_counter += 1
                if failure_counter <= 30:
                    print(
                        f"[DEBUG-NAV-CACHE] falha sat={sat} epoch={epoch_iso} erro={type(exc).__name__}: {exc}",
                        flush=True,
                    )

        cache[epoch_iso] = epoch_cache
        epoch_counter += 1
        current_dt += timedelta(seconds=step_seconds)

    print(f"[DEBUG-NAV-CACHE] epochs_generated={epoch_counter}", flush=True)
    print(f"[DEBUG-NAV-CACHE] xyz_counter={xyz_counter}", flush=True)
    print(f"[DEBUG-NAV-CACHE] failure_counter={failure_counter}", flush=True)

    non_empty_epochs = [k for k, v in cache.items() if v]
    print(
        f"[DEBUG-NAV-CACHE] non_empty_epochs={len(non_empty_epochs)}",
        flush=True,
    )

    if non_empty_epochs:
        first_non_empty = non_empty_epochs[0]
        print(
            f"[DEBUG-NAV-CACHE] first_non_empty_epoch={first_non_empty}",
            flush=True,
        )
        print(
            f"[DEBUG-NAV-CACHE] first_non_empty_keys={sorted(cache[first_non_empty].keys())[:20]}",
            flush=True,
        )
    else:
        print("[DEBUG-NAV-CACHE] ALERTA: todas as épocas ficaram vazias", flush=True)

    print("[DEBUG-NAV-CACHE] ===== FIM =====", flush=True)
    return cache


def build_relative_correction_vrs_seed(
    master_numeric: dict,
    aux1_numeric: dict,
    aux2_numeric: dict,
    target_meta: dict,
    max_epochs: int | None = None,
    min_satellites_per_epoch: int = 4,
    require_dual_frequency: bool = True,
    priority_obs: list[str] | None = None,
    gps_only: bool = True,
    distance_power: float = 1.2,
) -> dict:
    """
    VRS v4:
    - ancora explicitamente no alvo;
    - aplica delta geométrico explícito: rho_target - rho_master;
    - remove a componente geométrica das auxiliares antes da interpolação;
    - interpola resíduo atmosférico/rede;
    - mantém a mestre como base física das observações.
    """
    priority_obs = priority_obs or DEFAULT_PRIORITY_OBS

    master_epochs = _index_epochs(master_numeric)
    aux1_epochs = _index_epochs(aux1_numeric)
    aux2_epochs = _index_epochs(aux2_numeric)

    common_epoch_keys = sorted(
        set(master_epochs.keys()) & set(aux1_epochs.keys()) & set(aux2_epochs.keys())
    )

    if max_epochs is not None:
        common_epoch_keys = common_epoch_keys[:max_epochs]

    common_obs_types = _common_observation_types(
        master_numeric,
        aux1_numeric,
        aux2_numeric,
    )

    usable_obs_types = [
        obs
        for obs in common_obs_types
        if obs in {"C1", "P1", "L1", "C2", "P2", "L2", "C5", "L5", "S1", "S2", "S5"}
    ]

    if not usable_obs_types:
        raise ValueError(
            "Não há observáveis comuns suficientes para gerar a VRS relativa v4."
        )

    master_xyz = _extract_station_xyz(target_meta, "master_xyz")
    aux1_xyz = _extract_station_xyz(target_meta, "aux1_xyz")
    aux2_xyz = _extract_station_xyz(target_meta, "aux2_xyz")
    target_xyz = _build_target_xyz(target_meta)

    d_master_target = _vector_norm(_vector_sub(target_xyz, master_xyz))
    d_aux1_target = _vector_norm(_vector_sub(target_xyz, aux1_xyz))
    d_aux2_target = _vector_norm(_vector_sub(target_xyz, aux2_xyz))

    w_aux1 = _distance_weight(d_aux1_target, power=distance_power)
    w_aux2 = _distance_weight(d_aux2_target, power=distance_power)

    seed_epochs = []
    epochs_with_sat_xyz = 0
    epochs_without_sat_xyz = 0

    for epoch_iso in common_epoch_keys:
        master_epoch = master_epochs[epoch_iso]
        aux1_epoch = aux1_epochs[epoch_iso]
        aux2_epoch = aux2_epochs[epoch_iso]

        master_sats = _index_satellite_records(master_epoch)
        aux1_sats = _index_satellite_records(aux1_epoch)
        aux2_sats = _index_satellite_records(aux2_epoch)

        epoch_satellites = []
        epoch_has_sat_xyz = False

        for sat_id in sorted(master_sats.keys()):
            norm_sat = _normalize_satellite_id(sat_id)

            if gps_only and _get_satellite_system(norm_sat) != "G":
                continue

            master_record = master_sats.get(sat_id) or {}
            aux1_record = aux1_sats.get(sat_id) or {}
            aux2_record = aux2_sats.get(sat_id) or {}

            master_obs = master_record.get("observations", {}) or {}
            aux1_obs = aux1_record.get("observations", {}) or {}
            aux2_obs = aux2_record.get("observations", {}) or {}

            sat_xyz = (
                _extract_satellite_xyz(master_record)
                or _extract_satellite_xyz(aux1_record)
                or _extract_satellite_xyz(aux2_record)
            )

            if sat_xyz is None:
                continue

            epoch_has_sat_xyz = True

            rho_master = _geometric_range(master_xyz, sat_xyz)
            rho_aux1 = _geometric_range(aux1_xyz, sat_xyz)
            rho_aux2 = _geometric_range(aux2_xyz, sat_xyz)
            rho_target = _geometric_range(target_xyz, sat_xyz)

            delta_rho_target = rho_target - rho_master
            delta_rho_aux1 = rho_aux1 - rho_master
            delta_rho_aux2 = rho_aux2 - rho_master

            corrected_obs = {}

            for obs_name in usable_obs_types:
                if _is_snr_obs(obs_name):
                    corrected_obs[obs_name] = _safe_float(master_obs.get(obs_name))
                    continue

                m_raw = _safe_float(master_obs.get(obs_name))
                a1_raw = _safe_float(aux1_obs.get(obs_name))
                a2_raw = _safe_float(aux2_obs.get(obs_name))

                if m_raw is None:
                    corrected_obs[obs_name] = None
                    continue

                m_m = _obs_to_meters(obs_name, m_raw, sat_id=norm_sat)
                a1_m = _obs_to_meters(obs_name, a1_raw, sat_id=norm_sat)
                a2_m = _obs_to_meters(obs_name, a2_raw, sat_id=norm_sat)

                if m_m is None:
                    corrected_obs[obs_name] = None
                    continue

                residual_aux1 = None
                residual_aux2 = None

                if a1_m is not None:
                    residual_aux1 = (a1_m - m_m) - delta_rho_aux1

                if a2_m is not None:
                    residual_aux2 = (a2_m - m_m) - delta_rho_aux2

                interpolated_residual = _interpolate_residual(
                    residual_aux1=residual_aux1,
                    residual_aux2=residual_aux2,
                    weight_aux1=w_aux1,
                    weight_aux2=w_aux2,
                )

                target_obs_m = m_m + delta_rho_target + interpolated_residual

                corrected_obs[obs_name] = _meters_to_obs_units(
                    obs_name,
                    target_obs_m,
                    sat_id=norm_sat,
                )

            nn = _count_non_null(corrected_obs)
            if nn == 0:
                continue

            if require_dual_frequency and not _has_required_dual_frequency(
                corrected_obs
            ):
                continue

            epoch_satellites.append(
                {
                    "satellite": norm_sat,
                    "available_observation_count": nn,
                    "priority_obs_count": sum(
                        1
                        for k in priority_obs
                        if _safe_float(corrected_obs.get(k)) is not None
                    ),
                    "observations": corrected_obs,
                    "debug_geometry": {
                        "rho_master_m": rho_master,
                        "rho_aux1_m": rho_aux1,
                        "rho_aux2_m": rho_aux2,
                        "rho_target_m": rho_target,
                        "delta_rho_target_m": delta_rho_target,
                        "delta_rho_aux1_m": delta_rho_aux1,
                        "delta_rho_aux2_m": delta_rho_aux2,
                    },
                }
            )

        epoch_satellites.sort(
            key=lambda item: (
                item.get("priority_obs_count", 0),
                item.get("available_observation_count", 0),
                item.get("satellite", ""),
            ),
            reverse=True,
        )

        if len(epoch_satellites) >= min_satellites_per_epoch:
            seed_epochs.append(
                {
                    "epoch": epoch_iso,
                    "generated_satellite_count": len(epoch_satellites),
                    "satellites": epoch_satellites,
                }
            )

        if epoch_has_sat_xyz:
            epochs_with_sat_xyz += 1
        else:
            epochs_without_sat_xyz += 1

    return {
        "processing_mode": "relative_correction_vrs_v4",
        "method": "target_anchored_relative_geometry_v4",
        "target": target_meta,
        "master_based": True,
        "master_station_name": target_meta.get("master_station_name"),
        "geometry_coefficients": {
            "distance_power": distance_power,
            "master_target_distance_m": d_master_target,
            "aux1_target_distance_m": d_aux1_target,
            "aux2_target_distance_m": d_aux2_target,
            "epochs_with_satellite_xyz": epochs_with_sat_xyz,
            "epochs_without_satellite_xyz": epochs_without_sat_xyz,
        },
        "common_observation_types": usable_obs_types,
        "common_epoch_count": len(common_epoch_keys),
        "generated_epoch_count": len(seed_epochs),
        "systems": ["G"] if seed_epochs else [],
        "epochs": seed_epochs,
    }
