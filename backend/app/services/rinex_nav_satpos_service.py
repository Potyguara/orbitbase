"""
Leitura de navegação broadcast (RINEX 2.x / 3.x / 4.x) e cálculo de posição
de satélites GPS no referencial ECEF (WGS-84) segundo o IS-GPS-200.

Pontos importantes corrigidos nesta versão:

* RINEX 2 usa PRN numérico (" 1", "12") sem a letra do sistema. O parser
  antigo descartava todos os registros desses arquivos (.yyN do IBGE/RBMC).
* RINEX 3/4 tem colunas deslocadas em 1 caractere em relação ao RINEX 2.
* O tempo tk era calculado com "segundos do dia" contra o TOE em "segundos
  da semana GPS", o que só acertava aos domingos. Agora usamos tempo GPS
  absoluto (semana + segundos).
* Épocas com timezone (``...Z``) eram comparadas com datas naive, gerando
  TypeError silencioso no fallback NAV.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import gzip
import math
import re
import subprocess

GPS_MU = 3.986005e14
GPS_OMEGA_E = 7.2921151467e-5
GPS_F_REL = -4.442807633e-10  # s / m^(1/2)
GPS_WEEK_SECONDS = 604800.0
GPS_EPOCH = datetime(1980, 1, 6)

# Validade máxima de uma efeméride broadcast GPS em relação ao TOE.
MAX_EPHEMERIS_AGE_SECONDS = 4.0 * 3600.0

_NAV_NAME_PATTERNS = [
    re.compile(r".*\.\d{2}[np](\.gz|\.z)?$", re.I),  # GPS / mista (nomes curtos)
    re.compile(r".*_[gm]n\.rnx(\.gz)?$", re.I),  # RINEX 3 GPS / mista
    re.compile(r".*\.nav(\.gz|\.z)?$", re.I),
    re.compile(r"^brdc.*", re.I),
    re.compile(r".*gnav.*", re.I),
]

_OBS_NAME_PATTERNS = [
    re.compile(r".*_mo\.rnx(\.gz)?$", re.I),
    re.compile(r".*_[gm]o\.rnx(\.gz)?$", re.I),
    re.compile(r".*\.crx(\.gz)?$", re.I),
    re.compile(r".*\.\d{2}[od](\.gz|\.z)?$", re.I),
]


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_satellite_id(token: str, default_system: str = "G") -> str | None:
    """Normaliza 'G01', 'G 1', ' 1', '12' (RINEX 2) para 'G01'."""
    if token is None:
        return None

    raw = str(token).strip().upper().replace(" ", "")
    if not raw:
        return None

    if raw[0].isdigit():
        system = default_system
        digits = raw
    else:
        system = raw[0]
        digits = raw[1:]

    try:
        prn = int(digits)
    except Exception:
        return None

    if prn <= 0:
        return None

    return f"{system}{prn:02d}"


def to_naive_gps_datetime(value) -> datetime | None:
    """Converte datetime/ISO (com ou sem 'Z') para datetime naive."""
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def gps_seconds_since_epoch(dt: datetime) -> float:
    dt = to_naive_gps_datetime(dt)
    return (dt - GPS_EPOCH).total_seconds()


def gps_week_and_seconds(dt: datetime) -> tuple[int, float]:
    total = gps_seconds_since_epoch(dt)
    week = int(total // GPS_WEEK_SECONDS)
    return week, total - week * GPS_WEEK_SECONDS


# ---------------------------------------------------------------------------
# Detecção de arquivos
# ---------------------------------------------------------------------------


def is_observation_filename(name: str) -> bool:
    return any(p.match(name) for p in _OBS_NAME_PATTERNS)


_NON_GPS_NAV_PATTERN = re.compile(
    r"(.*\.\d{2}[glhqc](\.gz|\.z)?$)|(.*_(r|e|c|j|i|s)n\.rnx(\.gz)?$)", re.I
)


def is_gps_navigation_filename(name: str) -> bool:
    if is_observation_filename(name) or _NON_GPS_NAV_PATTERN.match(name):
        return False
    return any(p.match(name) for p in _NAV_NAME_PATTERNS)


def navigation_file_priority(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    if name.startswith("brdc") and re.search(r"\.\d{2}n(\.gz|\.z)?$", name):
        rank = 0
    elif re.search(r"_gn\.rnx(\.gz)?$", name):
        rank = 1
    elif re.search(r"\.\d{2}n$", name):
        rank = 2
    elif re.search(r"_mn\.rnx(\.gz)?$", name):
        rank = 3
    elif re.search(r"\.\d{2}n(\.gz|\.z)$", name):
        rank = 4
    elif name.endswith(".nav"):
        rank = 5
    else:
        rank = 8
    return (rank, name)


def detect_navigation_file_near_observation(obs_file_path: str) -> str | None:
    """
    Procura NAV GPS perto da observação. Cobre a estrutura RBMC do projeto:
    storage/rbmc/<estacao>/<data>/{converted,extracted,zip}.
    """
    obs_path = Path(obs_file_path)
    if not obs_path.exists():
        return None

    parent = obs_path.parent
    search_dirs = [
        parent,
        parent / "nav",
        parent / "converted",
        parent / "extracted",
        parent.parent,
        parent.parent / "extracted",
        parent.parent / "converted",
        parent.parent / "nav",
    ]

    seen: set[str] = set()
    nav_candidates: list[Path] = []

    for directory in search_dirs:
        try:
            if not directory.exists() or not directory.is_dir():
                continue
            key = str(directory.resolve())
            if key in seen:
                continue
            seen.add(key)

            for item in directory.iterdir():
                if item.is_file() and is_gps_navigation_filename(item.name):
                    nav_candidates.append(item)
        except Exception:
            continue

    if not nav_candidates:
        return None

    nav_candidates.sort(key=navigation_file_priority)
    return str(nav_candidates[0])


def open_navigation_text(nav_file_path: str) -> str:
    """Lê NAV em texto, descompactando .gz/.Z quando necessário."""
    path = Path(nav_file_path)
    raw = path.read_bytes()

    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif raw[:2] == b"\x1f\x9d":
        result = subprocess.run(
            ["gzip", "-dc"],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        raw = result.stdout

    return raw.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _rinex_float(text: str) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return _safe_float(text.replace("D", "E").replace("d", "e"))


def _fixed_values(text: str, count: int, width: int = 19) -> list[float | None]:
    return [_rinex_float(text[i * width : (i + 1) * width]) for i in range(count)]


def _make_datetime(year, month, day, hour, minute, second) -> datetime | None:
    try:
        year = int(year)
        if year < 100:
            year += 2000 if year < 80 else 1900
        second = float(second)
        sec_int = int(second)
        micro = int(round((second - sec_int) * 1_000_000))
        base = datetime(year, int(month), int(day), int(hour), int(minute))
        return base + timedelta(seconds=sec_int, microseconds=micro)
    except Exception:
        return None


_RINEX2_START = re.compile(r"^[ \d]\d [ \d]\d [ \d]\d [ \d]\d [ \d]\d [ \d]\d [ \d]\d\.\d")
_RINEX3_START = re.compile(r"^[GRECJSI][ \d]\d \d{4} \d\d \d\d \d\d \d\d \d\d")

_FIELD_LAYOUT = [
    ("af0", "af1", "af2"),
    ("iode", "crs", "delta_n", "m0"),
    ("cuc", "ecc", "cus", "sqrt_a"),
    ("toe", "cic", "omega0", "cis"),
    ("i0", "crc", "w", "omega_dot"),
    ("idot", "codes_l2", "gps_week", "l2_p_flag"),
    ("sv_accuracy", "sv_health", "tgd", "iodc"),
    ("transmission_time", "fit_interval"),
]


def _detect_nav_version(lines: list[str]) -> tuple[float, str]:
    for line in lines[:5]:
        if "RINEX VERSION / TYPE" in line:
            version = _safe_float(line[:9].strip()) or 2.0
            file_type = line[20:21].upper() if len(line) > 20 else "N"
            return version, file_type
    return 2.0, "N"


def _build_record(sat_id: str, toc: datetime, block: list[str], v3: bool) -> dict | None:
    if len(block) < 8:
        return None

    first_offset = 23 if v3 else 22
    cont_offset = 4 if v3 else 3

    record: dict = {"satellite": sat_id, "toc": toc}
    values = _fixed_values(block[0][first_offset:], 3)
    for key, val in zip(_FIELD_LAYOUT[0], values):
        record[key] = val

    for idx in range(1, 8):
        keys = _FIELD_LAYOUT[idx]
        values = _fixed_values(block[idx][cont_offset:], len(keys))
        for key, val in zip(keys, values):
            record[key] = val

    required = (
        "sqrt_a", "ecc", "m0", "toe", "delta_n", "omega0", "i0", "w",
        "omega_dot", "idot", "cuc", "cus", "crc", "crs", "cic", "cis",
    )
    if any(record.get(k) is None for k in required):
        return None
    if record["sqrt_a"] < 1000.0:
        return None

    return record


def parse_rinex_nav_gps(nav_file_path: str) -> dict[str, list[dict]]:
    """
    Retorna {"G01": [registro, ...]} com efemérides GPS ordenadas por TOC.
    Aceita RINEX 2.x (GPS), 3.x (GPS/misto) e 4.x (registros > EPH G.. LNAV).
    """
    path = Path(nav_file_path)
    if not path.exists():
        raise FileNotFoundError(f"NAV não encontrado: {nav_file_path}")

    lines = open_navigation_text(str(path)).splitlines()

    end_header_idx = None
    for idx, line in enumerate(lines):
        if "END OF HEADER" in line:
            end_header_idx = idx
            break

    if end_header_idx is None:
        raise ValueError(f"END OF HEADER não encontrado no NAV: {path.name}")

    version, file_type = _detect_nav_version(lines[: end_header_idx + 1])
    v3 = version >= 3.0

    if not v3 and file_type not in ("N", " "):
        # RINEX 2 'G' (GLONASS) / 'H' (SBAS): não são efemérides GPS.
        return {}

    body = lines[end_header_idx + 1 :]
    starts: list[int] = []
    for idx, line in enumerate(body):
        if v3:
            if _RINEX3_START.match(line):
                starts.append(idx)
            elif line.startswith(">"):
                starts.append(idx)
        elif _RINEX2_START.match(line):
            starts.append(idx)
    starts.append(len(body))

    records_by_sat: dict[str, list[dict]] = {}

    for pos in range(len(starts) - 1):
        a, b = starts[pos], starts[pos + 1]
        line = body[a]

        if line.startswith(">"):
            continue  # cabeçalho de registro RINEX 4; o registro vem na próxima linha

        if v3:
            # RINEX 4: descarta mensagens não LNAV (CNAV, CNV2 ...).
            if a > 0 and body[a - 1].startswith(">"):
                tokens = body[a - 1][1:].split()
                if len(tokens) >= 3 and (tokens[0] != "EPH" or tokens[2] != "LNAV"):
                    continue
            sat_id = _normalize_satellite_id(line[:3])
            if not sat_id or not sat_id.startswith("G"):
                continue
            toc = _make_datetime(
                line[4:8], line[9:11], line[12:14], line[15:17], line[18:20], line[21:23]
            )
        else:
            sat_id = _normalize_satellite_id(line[:2], default_system="G")
            if not sat_id:
                continue
            toc = _make_datetime(
                line[3:5], line[6:8], line[9:11], line[12:14], line[15:17], line[17:22]
            )

        if toc is None:
            continue

        record = _build_record(sat_id, toc, body[a:b], v3=v3)
        if record is None:
            continue

        records_by_sat.setdefault(sat_id, []).append(record)

    for sat in records_by_sat:
        records_by_sat[sat].sort(key=lambda r: r["toc"])

    print(
        f"[NAV-PARSE] file={path.name} version={version} "
        f"satellites={len(records_by_sat)} "
        f"records={sum(len(v) for v in records_by_sat.values())}",
        flush=True,
    )

    return records_by_sat


# ---------------------------------------------------------------------------
# Cálculo orbital
# ---------------------------------------------------------------------------


def _toe_absolute_seconds(record: dict) -> float:
    """Tempo GPS absoluto (s desde 1980-01-06) do TOE do registro."""
    toe = float(record["toe"])
    week = record.get("gps_week")
    toc_abs = gps_seconds_since_epoch(record["toc"])

    if week is not None and week > 0:
        week = int(round(week))
        # Corrige semanas truncadas em 10 bits (rollover).
        toc_week = int(toc_abs // GPS_WEEK_SECONDS)
        while week + 1024 <= toc_week + 1:
            week += 1024
        candidate = week * GPS_WEEK_SECONDS + toe
        # Sanidade: TOE e TOC devem estar próximos (até alguns dias).
        if abs(candidate - toc_abs) < 3.5 * 86400:
            return candidate

    toc_week = math.floor(toc_abs / GPS_WEEK_SECONDS)
    candidate = toc_week * GPS_WEEK_SECONDS + toe
    diff = candidate - toc_abs
    if diff > GPS_WEEK_SECONDS / 2:
        candidate -= GPS_WEEK_SECONDS
    elif diff < -GPS_WEEK_SECONDS / 2:
        candidate += GPS_WEEK_SECONDS
    return candidate


def select_ephemeris(records: list[dict], epoch_dt: datetime) -> dict | None:
    """Escolhe a efeméride saudável com TOE mais próximo, dentro da validade."""
    if not records:
        return None

    t = gps_seconds_since_epoch(epoch_dt)
    best = None
    best_dt = None

    for rec in records:
        health = rec.get("sv_health")
        if health is not None and health != 0:
            continue
        try:
            dt = abs(t - _toe_absolute_seconds(rec))
        except Exception:
            continue
        if dt > MAX_EPHEMERIS_AGE_SECONDS:
            continue
        if best_dt is None or dt < best_dt:
            best, best_dt = rec, dt

    return best


# Mantido por compatibilidade com código antigo.
def _nearest_ephemeris(records: list[dict], epoch_dt: datetime) -> dict | None:
    return select_ephemeris(records, epoch_dt)


def _wrap_gps_time(dt_seconds: float) -> float:
    while dt_seconds > 302400.0:
        dt_seconds -= GPS_WEEK_SECONDS
    while dt_seconds < -302400.0:
        dt_seconds += GPS_WEEK_SECONDS
    return dt_seconds


def compute_gps_satellite_position_ecef(
    record: dict, epoch_dt: datetime
) -> dict | None:
    """
    Posição ECEF (m) do satélite no instante ``epoch_dt`` (tempo GPS),
    mais o erro de relógio do satélite em microssegundos (``clock_us``),
    incluindo a correção relativística.
    """
    try:
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
        toe = float(record["toe"])
        epoch_dt = to_naive_gps_datetime(epoch_dt)
        t_abs = gps_seconds_since_epoch(epoch_dt)
        toe_abs = _toe_absolute_seconds(record)
    except Exception:
        return None

    a = sqrt_a * sqrt_a
    if a < 1.0:
        return None

    tk = _wrap_gps_time(t_abs - toe_abs)

    n0 = math.sqrt(GPS_MU / (a**3))
    n = n0 + delta_n
    mk = m0 + n * tk

    ek = mk
    for _ in range(30):
        ek_next = mk + ecc * math.sin(ek)
        if abs(ek_next - ek) < 1e-14:
            ek = ek_next
            break
        ek = ek_next

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

    clock_us = None
    af0 = record.get("af0")
    if af0 is not None:
        af1 = record.get("af1") or 0.0
        af2 = record.get("af2") or 0.0
        dt_clk = _wrap_gps_time(t_abs - gps_seconds_since_epoch(record["toc"]))
        rel = GPS_F_REL * ecc * sqrt_a * math.sin(ek)
        clock_us = (af0 + af1 * dt_clk + af2 * dt_clk * dt_clk + rel) * 1e6

    return {"x": xk, "y": yk, "z": zk, "clock_us": clock_us}


def compute_gps_satellite_state_ecef(
    record: dict, epoch_dt: datetime, half_step_s: float = 0.5
) -> dict | None:
    """Posição + velocidade (diferença central) no ECEF."""
    epoch_dt = to_naive_gps_datetime(epoch_dt)
    pos = compute_gps_satellite_position_ecef(record, epoch_dt)
    if pos is None:
        return None

    before = compute_gps_satellite_position_ecef(
        record, epoch_dt - timedelta(seconds=half_step_s)
    )
    after = compute_gps_satellite_position_ecef(
        record, epoch_dt + timedelta(seconds=half_step_s)
    )
    if before and after:
        span = 2.0 * half_step_s
        pos["vx"] = (after["x"] - before["x"]) / span
        pos["vy"] = (after["y"] - before["y"]) / span
        pos["vz"] = (after["z"] - before["z"]) / span

    return pos


def merge_navigation_records(nav_file_paths: list[str]) -> tuple[dict[str, list[dict]], list[str]]:
    """
    Junta efemérides GPS de vários arquivos NAV (ex.: um por estação RBMC),
    removendo duplicatas (mesmo satélite + mesmo TOC + mesmo IODE).
    """
    merged: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    errors: list[str] = []

    for nav_path in nav_file_paths or []:
        try:
            records = parse_rinex_nav_gps(str(nav_path))
        except Exception as exc:
            errors.append(f"{nav_path} :: {type(exc).__name__}: {exc}")
            continue
        if not records:
            errors.append(f"{nav_path} :: nenhum registro GPS válido")
            continue
        for sat, recs in records.items():
            for rec in recs:
                key = (sat, rec["toc"], rec.get("iode"))
                if key in seen:
                    continue
                seen.add(key)
                merged.setdefault(sat, []).append(rec)

    for sat in merged:
        merged[sat].sort(key=lambda r: r["toc"])

    return merged, errors


def build_satellite_position_cache_from_records(
    records_by_sat: dict[str, list[dict]],
    epoch_satellite_map: dict[str, list[str]],
    with_velocity: bool = True,
) -> dict[str, dict[str, dict]]:
    """
    Gera {epoch_iso: {sat: {x, y, z, vx, vy, vz, clock_us}}}. As chaves de
    época são preservadas exatamente como recebidas.
    """
    cache: dict[str, dict[str, dict]] = {}
    ok = 0
    fail = 0

    for epoch_iso, satellites in (epoch_satellite_map or {}).items():
        epoch_dt = to_naive_gps_datetime(epoch_iso)
        if epoch_dt is None:
            continue

        bucket: dict[str, dict] = {}

        for sat in satellites or []:
            sat_id = _normalize_satellite_id(sat)
            if not sat_id or not sat_id.startswith("G"):
                continue

            eph = select_ephemeris(records_by_sat.get(sat_id, []), epoch_dt)
            if not eph:
                fail += 1
                continue

            if with_velocity:
                state = compute_gps_satellite_state_ecef(eph, epoch_dt)
            else:
                state = compute_gps_satellite_position_ecef(eph, epoch_dt)
            if state is None:
                fail += 1
                continue

            bucket[sat_id] = state
            ok += 1

        cache[epoch_iso] = bucket

    non_empty = sum(1 for bucket in cache.values() if bucket)
    print(
        f"[NAV-CACHE] epochs={len(cache)} non_empty={non_empty} "
        f"positions={ok} missing={fail}",
        flush=True,
    )
    return cache


def build_satellite_position_cache_from_nav(
    nav_file_path: str,
    epoch_satellite_map: dict[str, list[str]],
) -> dict[str, dict[str, dict]]:
    records_by_sat = parse_rinex_nav_gps(nav_file_path)
    return build_satellite_position_cache_from_records(
        records_by_sat, epoch_satellite_map
    )
