from __future__ import annotations

from pathlib import Path
import math
import re
from datetime import datetime

GPS_MU = 3.986005e14
GPS_OMEGA_E = 7.2921151467e-5
GPS_WEEK_SECONDS = 604800.0

print(f"[DEBUG-NAV-MODULE] loaded_from={__file__}", flush=True)


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_satellite_id(token: str) -> str | None:
    if not token:
        return None

    raw = token.strip().upper().replace(" ", "")
    if len(raw) < 2:
        return None

    system = raw[0]
    try:
        prn = int(raw[1:])
    except Exception:
        return None

    return f"{system}{prn:02d}"


def _normalize_epoch_key(epoch_value) -> str | None:
    if epoch_value is None:
        return None

    if isinstance(epoch_value, datetime):
        return epoch_value.isoformat()

    text = str(epoch_value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text).isoformat()
    except Exception:
        return text


def detect_navigation_file_near_observation(obs_file_path: str) -> str | None:
    obs_path = Path(obs_file_path)
    if not obs_path.exists():
        return None

    search_dirs = [
        obs_path.parent,
        obs_path.parent.parent if obs_path.parent.parent.exists() else None,
        obs_path.parent / "nav",
        obs_path.parent / "converted",
    ]
    search_dirs = [d for d in search_dirs if d and d.exists() and d.is_dir()]

    nav_candidates = []

    nav_patterns = [
        re.compile(r".*\.nav$", re.I),
        re.compile(r".*\.rnx$", re.I),
        re.compile(r".*\.\d{2}n$", re.I),
        re.compile(r".*\.\d{2}g$", re.I),
        re.compile(r".*brdc.*", re.I),
        re.compile(r".*gnav.*", re.I),
    ]

    for directory in search_dirs:
        for item in directory.iterdir():
            if not item.is_file():
                continue

            name = item.name
            if any(p.match(name) for p in nav_patterns):
                nav_candidates.append(item)

    if not nav_candidates:
        return None

    def score(path: Path):
        name = path.name.lower()
        s = 99
        if "brdc" in name:
            s = 1
        elif re.search(r"\.\d{2}n$", name):
            s = 2
        elif name.endswith(".nav"):
            s = 3
        elif name.endswith(".rnx"):
            s = 4
        return (s, name)

    nav_candidates.sort(key=score)
    chosen = str(nav_candidates[0])
    print(f"[DEBUG-NAV-DETECT] obs={obs_file_path}", flush=True)
    print(f"[DEBUG-NAV-DETECT] chosen={chosen}", flush=True)
    return chosen


def _parse_nav_datetime(parts: list[str]) -> datetime | None:
    try:
        year = int(parts[0])
        if year < 100:
            year += 2000 if year < 80 else 1900

        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        minute = int(parts[4])
        second = float(parts[5])

        sec_int = int(second)
        micro = int(round((second - sec_int) * 1_000_000))

        if micro >= 1_000_000:
            sec_int += 1
            micro -= 1_000_000

        return datetime(year, month, day, hour, minute, sec_int, micro)
    except Exception:
        return None


def _rinex_float(text: str) -> float | None:
    if text is None:
        return None
    return _safe_float(text.replace("D", "E").replace("d", "e"))


def _split_fixed_width_values(line: str, width: int = 19) -> list[float | None]:
    values: list[float | None] = []
    for i in range(0, len(line), width):
        chunk = line[i : i + width]
        if not chunk.strip():
            continue
        values.append(_rinex_float(chunk.strip()))
    return values


def _cont_values(line: str) -> list[float | None]:
    """
    Linhas 2..8 do bloco NAV RINEX 2.x:
    os 4 campos numéricos ocupam 19 caracteres e começam na coluna 4.
    """
    return _split_fixed_width_values(line[3:] if len(line) > 3 else "")


def parse_rinex_nav_gps(nav_file_path: str) -> dict[str, list[dict]]:
    print("[DEBUG-NAV-PARSE] USANDO PARSER NOVO", flush=True)

    path = Path(nav_file_path)
    if not path.exists():
        raise FileNotFoundError(f"NAV não encontrado: {nav_file_path}")

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    print(f"[DEBUG-NAV-PARSE] nav_file={nav_file_path}", flush=True)
    print(f"[DEBUG-NAV-PARSE] total_lines={len(lines)}", flush=True)

    end_header_idx = None
    for idx, line in enumerate(lines):
        if "END OF HEADER" in line:
            end_header_idx = idx
            break

    if end_header_idx is None:
        raise ValueError("END OF HEADER não encontrado no NAV.")

    records_by_sat: dict[str, list[dict]] = {}
    i = end_header_idx + 1
    gps_blocks = 0
    first_valid_debug_done = False

    while i < len(lines):
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        if i + 7 >= len(lines):
            break

        prn_token = line[:3].strip()
        sat_id = _normalize_satellite_id(prn_token)

        if not sat_id or not sat_id.startswith("G"):
            i += 8
            continue

        gps_blocks += 1

        head = line[3:22].split()
        toc = _parse_nav_datetime(head[:6]) if len(head) >= 6 else None

        if toc is None:
            print(f"[DEBUG-NAV-BAD-TOC] i={i} line='{line}'", flush=True)
            i += 8
            continue

        line1_vals = _split_fixed_width_values(line[22:])
        line2_vals = _cont_values(lines[i + 1])
        line3_vals = _cont_values(lines[i + 2])
        line4_vals = _cont_values(lines[i + 3])
        line5_vals = _cont_values(lines[i + 4])
        line6_vals = _cont_values(lines[i + 5])
        line7_vals = _cont_values(lines[i + 6])
        line8_vals = _cont_values(lines[i + 7])

        try:
            record = {
                "satellite": sat_id,
                "toc": toc,
                # linha 1
                "af0": line1_vals[0] if len(line1_vals) > 0 else None,
                "af1": line1_vals[1] if len(line1_vals) > 1 else None,
                "af2": line1_vals[2] if len(line1_vals) > 2 else None,
                # linha 2
                "iode": line2_vals[0] if len(line2_vals) > 0 else None,
                "crs": line2_vals[1] if len(line2_vals) > 1 else None,
                "delta_n": line2_vals[2] if len(line2_vals) > 2 else None,
                "m0": line2_vals[3] if len(line2_vals) > 3 else None,
                # linha 3
                "cuc": line3_vals[0] if len(line3_vals) > 0 else None,
                "ecc": line3_vals[1] if len(line3_vals) > 1 else None,
                "cus": line3_vals[2] if len(line3_vals) > 2 else None,
                "sqrt_a": line3_vals[3] if len(line3_vals) > 3 else None,
                # linha 4
                "toe": line4_vals[0] if len(line4_vals) > 0 else None,
                "cic": line4_vals[1] if len(line4_vals) > 1 else None,
                "omega0": line4_vals[2] if len(line4_vals) > 2 else None,
                "cis": line4_vals[3] if len(line4_vals) > 3 else None,
                # linha 5
                "i0": line5_vals[0] if len(line5_vals) > 0 else None,
                "crc": line5_vals[1] if len(line5_vals) > 1 else None,
                "w": line5_vals[2] if len(line5_vals) > 2 else None,
                "omega_dot": line5_vals[3] if len(line5_vals) > 3 else None,
                # linha 6
                "idot": line6_vals[0] if len(line6_vals) > 0 else None,
                "codes_l2": line6_vals[1] if len(line6_vals) > 1 else None,
                "gps_week": line6_vals[2] if len(line6_vals) > 2 else None,
                "l2_p_flag": line6_vals[3] if len(line6_vals) > 3 else None,
                # linha 7
                "sv_accuracy": line7_vals[0] if len(line7_vals) > 0 else None,
                "sv_health": line7_vals[1] if len(line7_vals) > 1 else None,
                "tgd": line7_vals[2] if len(line7_vals) > 2 else None,
                "iodc": line7_vals[3] if len(line7_vals) > 3 else None,
                # linha 8
                "transmission_time": line8_vals[0] if len(line8_vals) > 0 else None,
                "fit_interval": line8_vals[1] if len(line8_vals) > 1 else None,
            }
        except Exception as e:
            print(
                f"[DEBUG-NAV-PARSE-ERROR] bloco em i={i} erro={type(e).__name__}: {e}",
                flush=True,
            )
            i += 8
            continue

        if (
            record["sqrt_a"] is None
            or record["ecc"] is None
            or record["m0"] is None
            or record["toe"] is None
        ):
            print(
                f"[DEBUG-NAV-INVALID-RECORD] sat={sat_id} toc={toc.isoformat()} "
                f"sqrt_a={record['sqrt_a']} ecc={record['ecc']} m0={record['m0']} toe={record['toe']}",
                flush=True,
            )
            i += 8
            continue

        if not first_valid_debug_done:
            print(f"[DEBUG-NAV-FIRST-VALID] sat={sat_id}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] toc={toc.isoformat()}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line1_vals={line1_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line2_vals={line2_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line3_vals={line3_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line4_vals={line4_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line5_vals={line5_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line6_vals={line6_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line7_vals={line7_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] line8_vals={line8_vals}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] keys={list(record.keys())}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] toe={record['toe']}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] sqrt_a={record['sqrt_a']}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] ecc={record['ecc']}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] m0={record['m0']}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] gps_week={record['gps_week']}", flush=True)
            print(f"[DEBUG-NAV-FIRST-VALID] iodc={record['iodc']}", flush=True)
            print(
                f"[DEBUG-NAV-FIRST-VALID] transmission_time={record['transmission_time']}",
                flush=True,
            )
            first_valid_debug_done = True

        records_by_sat.setdefault(sat_id, []).append(record)
        i += 8

    for sat in records_by_sat:
        records_by_sat[sat].sort(key=lambda r: r["toc"])

    print(f"[DEBUG-NAV-PARSE] gps_blocks={gps_blocks}", flush=True)
    print(f"[DEBUG-NAV-PARSE] satellites_total={len(records_by_sat)}", flush=True)
    if records_by_sat:
        sats = sorted(records_by_sat.keys())
        print(f"[DEBUG-NAV-PARSE] satellites_sample={sats[:20]}", flush=True)
    else:
        print(
            "[DEBUG-NAV-PARSE] ALERTA: nenhum registro GPS válido foi carregado",
            flush=True,
        )

    return records_by_sat


def _nearest_ephemeris(records: list[dict], epoch_dt: datetime) -> dict | None:
    if not records:
        return None
    return min(records, key=lambda r: abs((epoch_dt - r["toc"]).total_seconds()))


def _wrap_gps_time(dt_seconds: float) -> float:
    while dt_seconds > 302400.0:
        dt_seconds -= GPS_WEEK_SECONDS
    while dt_seconds < -302400.0:
        dt_seconds += GPS_WEEK_SECONDS
    return dt_seconds


def compute_gps_satellite_position_ecef(
    record: dict, epoch_dt: datetime
) -> dict | None:
    try:
        toe = float(record["toe"])
        sqrt_a = float(record["sqrt_a"])
        ecc = float(record["ecc"])
        m0 = float(record["m0"])
        delta_n = float(record["delta_n"])
        omega0 = float(record["omega0"])
        i0 = float(record["i0"])
        w = float(record["w"])
        omega_dot = float(record["omega_dot"])
        idot = float(record["idot"])
        cuc = float(record["cuc"])
        cus = float(record["cus"])
        crc = float(record["crc"])
        crs = float(record["crs"])
        cic = float(record["cic"])
        cis = float(record["cis"])
    except Exception:
        return None

    if abs(sqrt_a) < 1e-12:
        return None

    a = sqrt_a * sqrt_a
    if abs(a) < 1e-12:
        return None

    sow_approx = (
        epoch_dt.hour * 3600
        + epoch_dt.minute * 60
        + epoch_dt.second
        + epoch_dt.microsecond / 1_000_000
    )
    tk = _wrap_gps_time(sow_approx - toe)

    n0 = math.sqrt(GPS_MU / (a**3))
    n = n0 + delta_n
    mk = m0 + n * tk

    ek = mk
    for _ in range(12):
        ek = mk + ecc * math.sin(ek)

    one_minus_ecos = 1.0 - ecc * math.cos(ek)
    if abs(one_minus_ecos) < 1e-15:
        return None

    sin_vk = math.sqrt(max(0.0, 1.0 - ecc * ecc)) * math.sin(ek) / one_minus_ecos
    cos_vk = (math.cos(ek) - ecc) / one_minus_ecos
    vk = math.atan2(sin_vk, cos_vk)

    phik = vk + w

    duk = cuc * math.cos(2.0 * phik) + cus * math.sin(2.0 * phik)
    drk = crc * math.cos(2.0 * phik) + crs * math.sin(2.0 * phik)
    dik = cic * math.cos(2.0 * phik) + cis * math.sin(2.0 * phik)

    uk = phik + duk
    rk = a * one_minus_ecos + drk
    ik = i0 + dik + idot * tk

    xk_prime = rk * math.cos(uk)
    yk_prime = rk * math.sin(uk)

    omega_k = omega0 + (omega_dot - GPS_OMEGA_E) * tk - GPS_OMEGA_E * toe

    xk = xk_prime * math.cos(omega_k) - yk_prime * math.cos(ik) * math.sin(omega_k)
    yk = xk_prime * math.sin(omega_k) + yk_prime * math.cos(ik) * math.cos(omega_k)
    zk = yk_prime * math.sin(ik)

    if not math.isfinite(xk) or not math.isfinite(yk) or not math.isfinite(zk):
        return None

    return {"x": xk, "y": yk, "z": zk}


def build_satellite_position_cache_from_nav(
    nav_file_path: str,
    epoch_satellite_map: dict[str, list[str]],
) -> dict[str, dict[str, dict]]:
    print("[DEBUG-NAV-CACHE-BUILDER] INÍCIO", flush=True)
    print(f"[DEBUG-NAV-CACHE-BUILDER] nav_file_path={nav_file_path}", flush=True)

    records_by_sat = parse_rinex_nav_gps(nav_file_path)
    cache: dict[str, dict[str, dict]] = {}

    xyz_counter = 0
    fail_counter = 0
    first_xyz_done = False

    for epoch_iso, satellites in (epoch_satellite_map or {}).items():
        try:
            epoch_dt = datetime.fromisoformat(epoch_iso)
        except Exception:
            continue

        cache[epoch_iso] = {}

        for sat in satellites or []:
            sat_id = _normalize_satellite_id(sat)
            if not sat_id or not sat_id.startswith("G"):
                continue

            eph = _nearest_ephemeris(records_by_sat.get(sat_id, []), epoch_dt)
            if not eph:
                fail_counter += 1
                continue

            sat_xyz = compute_gps_satellite_position_ecef(eph, epoch_dt)
            if sat_xyz is not None:
                cache[epoch_iso][sat_id] = sat_xyz
                xyz_counter += 1

                if not first_xyz_done:
                    print(f"[DEBUG-NAV-FIRST-XYZ] epoch={epoch_iso}", flush=True)
                    print(f"[DEBUG-NAV-FIRST-XYZ] sat={sat_id}", flush=True)
                    print(
                        f"[DEBUG-NAV-FIRST-XYZ] eph_toe={eph.get('toe')} "
                        f"gps_week={eph.get('gps_week')} sqrt_a={eph.get('sqrt_a')}",
                        flush=True,
                    )
                    print(f"[DEBUG-NAV-FIRST-XYZ] xyz={sat_xyz}", flush=True)
                    first_xyz_done = True
            else:
                fail_counter += 1

    non_empty_epochs = sum(1 for _, bucket in cache.items() if bucket)

    print(f"[DEBUG-NAV-CACHE-BUILDER] epochs_generated={len(cache)}", flush=True)
    print(f"[DEBUG-NAV-CACHE-BUILDER] xyz_counter={xyz_counter}", flush=True)
    print(f"[DEBUG-NAV-CACHE-BUILDER] fail_counter={fail_counter}", flush=True)
    print(f"[DEBUG-NAV-CACHE-BUILDER] non_empty_epochs={non_empty_epochs}", flush=True)

    if cache:
        first_epoch = next(iter(cache.keys()))
        print(f"[DEBUG-NAV-CACHE-BUILDER] first_epoch={first_epoch}", flush=True)
        print(
            f"[DEBUG-NAV-CACHE-BUILDER] first_epoch_sample_sats={list(cache[first_epoch].keys())[:10]}",
            flush=True,
        )

    if non_empty_epochs == 0:
        print(
            "[DEBUG-NAV-CACHE-BUILDER] ALERTA: todas as épocas ficaram vazias",
            flush=True,
        )

    print("[DEBUG-NAV-CACHE-BUILDER] FIM", flush=True)
    return cache
