from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from math import asin, atan2, cos, exp, hypot, sin, sqrt
from statistics import median
from typing import Any

print(
    f"[ADV-INT-MODULE] loaded={__file__}",
    flush=True,
)

C_LIGHT = 299792458.0

GPS_L1_HZ = 1575.42e6
GPS_L2_HZ = 1227.60e6


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _normalize_satellite_code(raw_code: Any) -> str | None:
    if raw_code is None:
        return None

    code = str(raw_code).strip().upper()
    if not code:
        return None

    code = code.replace(" ", "")

    if len(code) >= 2 and code[0].isalpha():
        system = code[0]
        rest = "".join(ch for ch in code[1:] if ch.isdigit())
        if system in {"G", "R", "E", "C", "J", "I", "S"} and rest:
            try:
                prn = int(rest)
                return f"{system}{prn:02d}"
            except Exception:
                pass

    if len(code) >= 2 and code[-1].isalpha():
        system = code[-1]
        rest = "".join(ch for ch in code[:-1] if ch.isdigit())
        if system in {"G", "R", "E", "C", "J", "I", "S"} and rest:
            try:
                prn = int(rest)
                return f"{system}{prn:02d}"
            except Exception:
                pass

    return None


def _extract_obs_values(obs_payload: Any) -> dict[str, float | None]:
    if not isinstance(obs_payload, dict):
        return {}

    out: dict[str, float | None] = {}

    for obs_code, raw_value in obs_payload.items():
        if obs_code is None:
            continue

        code = str(obs_code).strip().upper()
        if not code:
            continue

        out[code] = _safe_float(raw_value)

    return out


def _ensure_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _epoch_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ecef_distance(
    x1: float,
    y1: float,
    z1: float,
    x2: float,
    y2: float,
    z2: float,
) -> float:
    return sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)


OMEGA_E_DOT = 7.2921151467e-5


def _geometric_range(
    receiver_xyz: tuple[float, float, float],
    sat_xyz: dict,
) -> float | None:
    """
    Distância geométrica receptor-satélite com correção do tempo de
    propagação (posição do satélite na emissão, usando a velocidade quando
    disponível) e da rotação da Terra (efeito Sagnac). Sem essas correções o
    termo delta_rho base->VRS carregava erros decimétricos a métricos.
    """
    rx, ry, rz = receiver_xyz
    sx = _safe_float(sat_xyz.get("x"))
    sy = _safe_float(sat_xyz.get("y"))
    sz = _safe_float(sat_xyz.get("z"))

    if sx is None or sy is None or sz is None:
        return None

    vx = _safe_float(sat_xyz.get("vx")) or 0.0
    vy = _safe_float(sat_xyz.get("vy")) or 0.0
    vz = _safe_float(sat_xyz.get("vz")) or 0.0

    rho = _ecef_distance(rx, ry, rz, sx, sy, sz)

    for _ in range(3):
        tau = rho / C_LIGHT
        ex = sx - vx * tau
        ey = sy - vy * tau
        ez = sz - vz * tau
        theta = OMEGA_E_DOT * tau
        cos_t = cos(theta)
        sin_t = sin(theta)
        xr = cos_t * ex + sin_t * ey
        yr = -sin_t * ex + cos_t * ey
        rho = _ecef_distance(rx, ry, rz, xr, yr, ez)

    return rho


_WGS84_A = 6378137.0
_WGS84_E2 = 6.69437999014e-3


def _geodetic_lat_h(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    """Latitude, longitude (rad) e altura elipsoidal (m) a partir de ECEF."""
    x, y, z = xyz
    lon = atan2(y, x)
    p = hypot(x, y)
    lat = atan2(z, p * (1.0 - _WGS84_E2))
    h = 0.0
    for _ in range(5):
        n = _WGS84_A / sqrt(1.0 - _WGS84_E2 * sin(lat) ** 2)
        h = p / max(cos(lat), 1e-12) - n
        lat = atan2(z, p * (1.0 - _WGS84_E2 * n / (n + h)))
    return lat, lon, h


def _slant_tropo_delay(receiver_xyz: tuple[float, float, float], sat_xyz: dict) -> float:
    """Atraso troposférico inclinado (m) com atmosfera padrão.

    Saastamoinen hidrostático + úmido típico de região equatorial, com função de
    mapeamento simples. Só a diferença base->alvo entra nas observações, então o
    erro absoluto do modelo se cancela quase todo; o que importa é corrigir a
    diferença de elevação e de altitude entre a base e o alvo.
    """
    sx = _safe_float(sat_xyz.get("x"))
    sy = _safe_float(sat_xyz.get("y"))
    sz = _safe_float(sat_xyz.get("z"))
    if sx is None or sy is None or sz is None:
        return 0.0

    lat, lon, h = _geodetic_lat_h(receiver_xyz)
    dx, dy, dz = sx - receiver_xyz[0], sy - receiver_xyz[1], sz - receiver_xyz[2]
    up = cos(lat) * cos(lon) * dx + cos(lat) * sin(lon) * dy + sin(lat) * dz
    dist = sqrt(dx * dx + dy * dy + dz * dz)
    if dist <= 0.0:
        return 0.0
    elev = asin(max(-1.0, min(1.0, up / dist)))
    sin_e = sin(max(elev, 0.05))  # ~3 graus

    h_clamped = max(-500.0, min(h, 9000.0))
    pressure = 1013.25 * (1.0 - 2.2557e-5 * h_clamped) ** 5.2568
    zhd = 0.0022768 * pressure / (
        1.0 - 0.00266 * cos(2.0 * lat) - 0.00028 * h_clamped / 1000.0
    )
    zwd = 0.25 * exp(-max(h_clamped, 0.0) / 2000.0)
    mapping = 1.001 / sqrt(0.002001 + sin_e * sin_e)
    return (zhd + zwd) * mapping


def _range_with_tropo(
    receiver_xyz: tuple[float, float, float], sat_xyz: dict
) -> float | None:
    rho = _geometric_range(receiver_xyz, sat_xyz)
    if rho is None:
        return None
    return rho + _slant_tropo_delay(receiver_xyz, sat_xyz)


def _median_step_seconds(
    base_numeric_data: list[dict], fallback: float = 15.0
) -> float:
    steps: list[float] = []

    for base in base_numeric_data or []:
        epochs = base.get("epochs") or []
        prev_dt: datetime | None = None

        for ep in epochs:
            dt = _ensure_utc_datetime(ep.get("epoch") or ep.get("epoch_utc"))
            if dt is None:
                continue

            if prev_dt is not None:
                step = (dt - prev_dt).total_seconds()
                if step > 0:
                    steps.append(step)

            prev_dt = dt

    if not steps:
        return float(fallback)

    try:
        return float(median(steps))
    except Exception:
        return float(fallback)


def _build_geometry_epoch_map(geometry_cache: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}

    for ep in geometry_cache.get("epochs") or []:
        epoch_iso = ep.get("epoch")
        sats = ep.get("satellites") or {}
        if epoch_iso:
            out[epoch_iso] = sats

    return out


def _extract_obs_map_from_epoch(
    epoch_payload: dict,
) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}

    if not isinstance(epoch_payload, dict):
        return result

    satellites_payload = epoch_payload.get("satellites")

    if isinstance(satellites_payload, list):
        for item in satellites_payload:
            if not isinstance(item, dict):
                continue

            sat_code = _normalize_satellite_code(
                item.get("satellite")
                or item.get("sat")
                or item.get("prn")
                or item.get("sv")
            )
            if not sat_code:
                continue

            obs_map = _extract_obs_values(item.get("observations"))
            if not obs_map:
                continue

            result[sat_code] = obs_map

        return result

    if isinstance(satellites_payload, dict):
        for raw_sat, obs_payload in satellites_payload.items():
            sat_code = _normalize_satellite_code(raw_sat)
            if not sat_code:
                continue

            obs_map = _extract_obs_values(obs_payload)
            if not obs_map:
                continue

            result[sat_code] = obs_map

        return result

    observations_payload = epoch_payload.get("observations")
    if isinstance(observations_payload, dict):
        for raw_sat, obs_payload in observations_payload.items():
            sat_code = _normalize_satellite_code(raw_sat)
            if not sat_code:
                continue

            obs_map = _extract_obs_values(obs_payload)
            if not obs_map:
                continue

            result[sat_code] = obs_map

    return result


def _normalize_base_epochs(base_numeric_data: list[dict]) -> list[dict]:
    normalized_bases: list[dict] = []

    for idx_base, base in enumerate(base_numeric_data or []):
        station_code = str(base.get("station_code") or "").strip().upper()
        label = base.get("label")

        bx = _safe_float(base.get("approx_x"))
        by = _safe_float(base.get("approx_y"))
        bz = _safe_float(base.get("approx_z"))

        if bx is None or by is None or bz is None:
            print(
                f"[ADV-INT-NORM] skip base idx={idx_base} station={station_code} "
                f"reason=missing_xyz approx_x={base.get('approx_x')} "
                f"approx_y={base.get('approx_y')} approx_z={base.get('approx_z')}",
                flush=True,
            )
            continue

        epochs_in = base.get("epochs") or []
        epochs_out: list[dict] = []

        for ep in epochs_in:
            if not isinstance(ep, dict):
                continue

            dt = _ensure_utc_datetime(ep.get("epoch") or ep.get("epoch_utc"))
            if dt is None:
                continue

            obs_map = _extract_obs_map_from_epoch(ep)
            if not obs_map:
                continue

            epochs_out.append(
                {
                    "epoch": dt,
                    "epoch_iso": _epoch_to_iso(dt),
                    "obs_map": obs_map,
                }
            )

        epochs_out.sort(key=lambda item: item["epoch"])

        print(
            f"[ADV-INT-NORM] base idx={idx_base} station={station_code} "
            f"raw_epochs={len(epochs_in)} usable_epochs={len(epochs_out)}",
            flush=True,
        )

        if epochs_out:
            sample_sats = list(sorted(epochs_out[0]["obs_map"].keys()))[:12]
            print(
                f"[ADV-INT-NORM] base station={station_code} "
                f"first_epoch={epochs_out[0]['epoch_iso']} "
                f"last_epoch={epochs_out[-1]['epoch_iso']} "
                f"sample_sats={sample_sats}",
                flush=True,
            )

        if not epochs_out:
            continue

        normalized_bases.append(
            {
                "station_code": station_code,
                "label": label,
                "xyz": (bx, by, bz),
                "epochs": epochs_out,
                "epoch_datetimes": [item["epoch"] for item in epochs_out],
            }
        )

    print(
        f"[ADV-INT-NORM] normalized_bases={len(normalized_bases)}",
        flush=True,
    )
    return normalized_bases


def _find_bracketing_epochs(
    epoch_datetimes: list[datetime],
    epochs: list[dict],
    target_epoch: datetime,
    max_gap_seconds: float,
) -> tuple[dict | None, dict | None, float]:
    if not epoch_datetimes:
        return None, None, 0.0

    idx = bisect_left(epoch_datetimes, target_epoch)

    if idx < len(epoch_datetimes) and epoch_datetimes[idx] == target_epoch:
        return epochs[idx], epochs[idx], 0.0

    left = epochs[idx - 1] if idx > 0 else None
    right = epochs[idx] if idx < len(epochs) else None

    if left is None or right is None:
        return None, None, 0.0

    t0 = left["epoch"]
    t1 = right["epoch"]

    gap = (t1 - t0).total_seconds()
    if gap <= 0 or gap > max_gap_seconds:
        return None, None, 0.0

    alpha = (target_epoch - t0).total_seconds() / gap
    if alpha < 0.0 or alpha > 1.0:
        return None, None, 0.0

    return left, right, alpha


def _interpolate_scalar(
    v0: float | None, v1: float | None, alpha: float
) -> float | None:
    if v0 is None or v1 is None:
        return None
    return v0 + (v1 - v0) * alpha


def _interpolate_observation_record(
    rec0: dict,
    rec1: dict,
    alpha: float,
    allow_phase: bool,
) -> dict:
    out: dict[str, float] = {}

    for key in ("C1", "P1", "C2", "P2", "S1", "S2"):
        value = _interpolate_scalar(
            _safe_float(rec0.get(key)),
            _safe_float(rec1.get(key)),
            alpha,
        )
        if value is not None:
            out[key] = value

    if allow_phase:
        for key in ("L1", "L2"):
            value = _interpolate_scalar(
                _safe_float(rec0.get(key)),
                _safe_float(rec1.get(key)),
                alpha,
            )
            if value is not None:
                out[key] = value

    return out


def _gps_wavelength(obs_code: str) -> float | None:
    if obs_code == "L1":
        return C_LIGHT / GPS_L1_HZ
    if obs_code == "L2":
        return C_LIGHT / GPS_L2_HZ
    return None


def _transport_record_to_target(
    sat: str,
    interpolated_record: dict,
    sat_xyz: dict,
    base_xyz: tuple[float, float, float],
    target_xyz: tuple[float, float, float],
    allow_phase: bool,
) -> dict:
    rho_base = _range_with_tropo(base_xyz, sat_xyz)
    rho_target = _range_with_tropo(target_xyz, sat_xyz)

    if rho_base is None or rho_target is None:
        return {}

    delta_rho = rho_target - rho_base
    system = sat[:1].upper()

    out: dict[str, float] = {}

    for key in ("C1", "P1", "C2", "P2"):
        obs = _safe_float(interpolated_record.get(key))
        if obs is not None:
            out[key] = obs + delta_rho

    for key in ("S1", "S2"):
        obs = _safe_float(interpolated_record.get(key))
        if obs is not None:
            out[key] = obs

    if allow_phase and system == "G":
        for key in ("L1", "L2"):
            cycles = _safe_float(interpolated_record.get(key))
            if cycles is None:
                continue

            wavelength = _gps_wavelength(key)
            if wavelength is None:
                continue

            meters = cycles * wavelength
            transported_cycles = (meters + delta_rho) / wavelength
            out[key] = transported_cycles

    return out


def _weighted_merge_records(weighted_records: list[tuple[float, dict]]) -> dict:
    if not weighted_records:
        return {}

    sums: dict[str, float] = {}
    weights: dict[str, float] = {}

    for weight, record in weighted_records:
        if weight <= 0.0:
            continue

        for key, value in record.items():
            v = _safe_float(value)
            if v is None:
                continue

            sums[key] = sums.get(key, 0.0) + weight * v
            weights[key] = weights.get(key, 0.0) + weight

    out = {}
    for key, total in sums.items():
        w = weights.get(key, 0.0)
        if w > 0.0:
            out[key] = total / w

    return out


def _base_weight(
    base_xyz: tuple[float, float, float],
    target_xyz: tuple[float, float, float],
) -> float:
    dist = _ecef_distance(
        base_xyz[0],
        base_xyz[1],
        base_xyz[2],
        target_xyz[0],
        target_xyz[1],
        target_xyz[2],
    )
    dist = max(dist, 1.0)
    return 1.0 / dist


def _obs_priority_has_code(record: dict) -> bool:
    return any(_safe_float(record.get(k)) is not None for k in ("C1", "P1", "C2", "P2"))


def _obs_has_dual_phase_gps(record: dict) -> bool:
    has_l1_code = (
        _safe_float(record.get("C1")) is not None
        or _safe_float(record.get("P1")) is not None
    )
    has_l2_code = (
        _safe_float(record.get("C2")) is not None
        or _safe_float(record.get("P2")) is not None
    )
    has_l1_phase = _safe_float(record.get("L1")) is not None
    has_l2_phase = _safe_float(record.get("L2")) is not None
    return has_l1_code and has_l2_code and has_l1_phase and has_l2_phase


def _count_base_quality(base: dict) -> tuple[int, int]:
    gps_dual_phase_obs = 0
    gps_code_obs = 0

    for ep in base.get("epochs") or []:
        obs_map = ep.get("obs_map") or {}
        for sat, rec in obs_map.items():
            if not str(sat).startswith("G"):
                continue
            if _obs_priority_has_code(rec):
                gps_code_obs += 1
            if _obs_has_dual_phase_gps(rec):
                gps_dual_phase_obs += 1

    return gps_dual_phase_obs, gps_code_obs


# Uma base só entra na disputa de mestra se tiver pelo menos esta fração das
# observações GPS de dupla fase da melhor base.
MASTER_MIN_QUALITY_RATIO = 0.6


def _choose_master_base(
    normalized_bases: list[dict],
    target_xyz: tuple[float, float, float] | None = None,
) -> dict | None:
    """Escolhe a base mestra do modo com fase.

    A fase transportada carrega os erros espacialmente correlacionados da mestra
    (troposfera, órbita, multicaminho), que crescem com a distância. Por isso,
    entre as bases com qualidade suficiente, vence a mais próxima do alvo.
    """
    if not normalized_bases:
        return None

    ranked = []
    for base in normalized_bases:
        gps_dual_phase_obs, gps_code_obs = _count_base_quality(base)
        dist_km = None
        if target_xyz is not None:
            dist_km = _ecef_distance(*base["xyz"], *target_xyz) / 1000.0
        print(
            f"[ADV-INT-MASTER] station={base.get('station_code')} "
            f"gps_dual_phase_obs={gps_dual_phase_obs} gps_code_obs={gps_code_obs} "
            f"dist_km={dist_km if dist_km is None else round(dist_km, 1)}",
            flush=True,
        )
        ranked.append((gps_dual_phase_obs, gps_code_obs, dist_km, base))

    best_quality = max(item[0] for item in ranked)
    if best_quality <= 0:
        return None

    if target_xyz is None:
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return ranked[0][3]

    eligible = [
        item for item in ranked if item[0] >= MASTER_MIN_QUALITY_RATIO * best_quality
    ]
    eligible.sort(key=lambda item: (item[2], -item[0]))
    chosen = eligible[0][3]
    print(
        f"[ADV-INT-MASTER] chosen={chosen.get('station_code')} "
        f"dist_km={round(eligible[0][2], 1)}",
        flush=True,
    )
    return chosen


def _build_master_time_grid(master_base: dict) -> list[datetime]:
    return [
        ep["epoch"]
        for ep in (master_base.get("epochs") or [])
        if ep.get("epoch") is not None
    ]


def _allowed_systems_from_config(config: dict, phase_mode: bool) -> set[str]:
    output_profile = config.get("output_profile") or {}
    allowed = output_profile.get("allowed_systems")

    if isinstance(allowed, (list, tuple, set)):
        normalized = {
            str(item).strip().upper() for item in allowed if str(item).strip()
        }
        if normalized:
            return normalized

    return {"G"} if phase_mode else {"G", "R"}


# Salto máximo aceito no resíduo (observável - distância geométrica) entre duas
# épocas da base mestra. Acima disso há perda de ciclo ou salto de relógio do
# receptor, e a interpolação no tempo seria inválida.
PHASE_RESIDUAL_MAX_JUMP_M = 1000.0
_DENSE_CODE_KEYS = ("C1", "P1", "C2", "P2")
_DENSE_PHASE_KEYS = ("L1", "L2")


def _residuals_at_master_epoch(rec: dict, sat_xyz: dict, master_xyz) -> dict | None:
    """Observáveis em metros menos a distância geométrica (com troposfera) base-satélite."""
    rho = _range_with_tropo(master_xyz, sat_xyz)
    if rho is None:
        return None
    out: dict[str, float] = {}
    for key in _DENSE_CODE_KEYS:
        value = _safe_float(rec.get(key))
        if value is not None:
            out[key] = value - rho
    for key in _DENSE_PHASE_KEYS:
        value = _safe_float(rec.get(key))
        wavelength = _gps_wavelength(key)
        if value is not None and wavelength:
            out[key] = value * wavelength - rho
    for key in ("S1", "S2"):
        value = _safe_float(rec.get(key))
        if value is not None:
            out[key] = value
    return out


def _build_output_epochs_phase_dense(
    master_base: dict,
    geometry_epoch_map: dict[str, dict],
    target_xyz: tuple[float, float, float],
    time_grid: list[datetime],
    max_gap_seconds: float,
    min_satellites_per_epoch: int,
) -> tuple[list[dict], int]:
    """Fase GPS na grade pedida (ex.: 1 s) a partir da base mestra (ex.: 15 s).

    Para cada satélite, o resíduo r = observável·λ - ρ(base, sat) varia devagar
    (relógios, atmosfera, ambiguidade constante). Ele é interpolado linearmente
    entre as duas épocas da mestra que cercam t, e o observável virtual é
    r(t) + ρ(alvo, sat, t), com a geometria do satélite no próprio instante t.
    """
    master_xyz = master_base["xyz"]
    master_epochs = [ep for ep in (master_base.get("epochs") or []) if ep.get("epoch")]
    epoch_times = [ep["epoch"] for ep in master_epochs]
    residual_cache: dict[int, dict[str, dict]] = {}

    def residuals(idx: int) -> dict[str, dict]:
        if idx not in residual_cache:
            ep = master_epochs[idx]
            geom = geometry_epoch_map.get(ep["epoch_iso"]) or {}
            out = {}
            for sat, rec in (ep.get("obs_map") or {}).items():
                if not str(sat).startswith("G") or sat not in geom:
                    continue
                if not _obs_has_dual_phase_gps(rec):
                    continue
                r = _residuals_at_master_epoch(rec, geom[sat], master_xyz)
                if r:
                    out[sat] = r
            residual_cache[idx] = out
            # mantém só uma janela pequena em memória
            for old in [k for k in residual_cache if k < idx - 2]:
                residual_cache.pop(old, None)
        return residual_cache[idx]

    output_epochs: list[dict] = []
    epochs_skipped = 0

    for target in time_grid:
        target_iso = _epoch_to_iso(target)
        geom_t = geometry_epoch_map.get(target_iso) or {}
        idx = bisect_left(epoch_times, target)

        if idx < len(epoch_times) and epoch_times[idx] == target:
            i0 = i1 = idx
            alpha = 0.0
        else:
            i0, i1 = idx - 1, idx
            if i0 < 0 or i1 >= len(epoch_times):
                epochs_skipped += 1
                continue
            gap = (epoch_times[i1] - epoch_times[i0]).total_seconds()
            if gap <= 0 or gap > max_gap_seconds:
                epochs_skipped += 1
                continue
            alpha = (target - epoch_times[i0]).total_seconds() / gap

        r0 = residuals(i0)
        r1 = residuals(i1)
        observations = []

        for sat in sorted(set(r0) & set(r1)):
            if sat not in geom_t:
                continue
            rho_t = _range_with_tropo(target_xyz, geom_t[sat])
            if rho_t is None:
                continue
            a, b = r0[sat], r1[sat]
            out: dict[str, float] = {}
            jump = False
            for key in _DENSE_CODE_KEYS + _DENSE_PHASE_KEYS:
                if key not in a or key not in b:
                    continue
                if abs(b[key] - a[key]) > PHASE_RESIDUAL_MAX_JUMP_M:
                    jump = True
                    break
                meters = a[key] + (b[key] - a[key]) * alpha + rho_t
                if key in _DENSE_PHASE_KEYS:
                    out[key] = meters / _gps_wavelength(key)
                else:
                    out[key] = meters
            if jump:
                continue
            for key in ("S1", "S2"):
                if key in a and key in b:
                    out[key] = a[key] + (b[key] - a[key]) * alpha
            if not _obs_has_dual_phase_gps(out):
                continue
            observations.append({"satellite": sat, **out})

        if len(observations) < min_satellites_per_epoch:
            epochs_skipped += 1
            continue

        output_epochs.append(
            {
                "epoch": target_iso,
                "observations": observations,
                "satellite_count": len(observations),
            }
        )

    return output_epochs, epochs_skipped


def _build_output_epochs_phase_mode(
    master_base: dict,
    geometry_epoch_map: dict[str, dict],
    target_xyz: tuple[float, float, float],
    min_satellites_per_epoch: int,
) -> tuple[list[dict], int]:
    output_epochs: list[dict] = []
    epochs_skipped = 0

    master_xyz = master_base["xyz"]
    master_epochs = master_base.get("epochs") or []

    for idx_epoch, ep in enumerate(master_epochs):
        target_epoch = ep["epoch"]
        epoch_iso = ep["epoch_iso"]
        sat_geometry = geometry_epoch_map.get(epoch_iso) or {}

        if idx_epoch < 3:
            print(
                f"[ADV-INT-EPOCH] idx={idx_epoch} target={epoch_iso} sat_geometry_count={len(sat_geometry)}",
                flush=True,
            )

        if not sat_geometry:
            epochs_skipped += 1
            continue

        obs_map = ep.get("obs_map") or {}
        epoch_observations = []

        for sat, rec in sorted(obs_map.items()):
            if not str(sat).startswith("G"):
                continue

            if sat not in sat_geometry:
                continue

            if not _obs_has_dual_phase_gps(rec):
                continue

            transported = _transport_record_to_target(
                sat=sat,
                interpolated_record=rec,
                sat_xyz=sat_geometry[sat],
                base_xyz=master_xyz,
                target_xyz=target_xyz,
                allow_phase=True,
            )

            if not transported:
                continue

            if not _obs_has_dual_phase_gps(transported):
                continue

            epoch_observations.append(
                {
                    "satellite": sat,
                    **transported,
                }
            )

        if idx_epoch < 3:
            print(
                f"[ADV-INT-EPOCH] idx={idx_epoch} target={epoch_iso} "
                f"epoch_observations={len(epoch_observations)} "
                f"sats={[item['satellite'] for item in epoch_observations[:12]]}",
                flush=True,
            )

        if len(epoch_observations) < min_satellites_per_epoch:
            epochs_skipped += 1
            continue

        output_epochs.append(
            {
                "epoch": epoch_iso,
                "observations": epoch_observations,
                "satellite_count": len(epoch_observations),
            }
        )

    return output_epochs, epochs_skipped


def _build_output_epochs_code_mode(
    normalized_time_grid: list[datetime],
    normalized_bases: list[dict],
    geometry_epoch_map: dict[str, dict],
    target_xyz: tuple[float, float, float],
    max_gap_seconds: float,
    min_satellites_per_epoch: int,
    allowed_systems: set[str],
) -> tuple[list[dict], int]:
    output_epochs: list[dict] = []
    epochs_skipped = 0

    for idx_epoch, target_epoch in enumerate(normalized_time_grid):
        epoch_iso = _epoch_to_iso(target_epoch)
        sat_geometry = geometry_epoch_map.get(epoch_iso) or {}

        if idx_epoch < 3:
            print(
                f"[ADV-INT-EPOCH] idx={idx_epoch} target={epoch_iso} sat_geometry_count={len(sat_geometry)}",
                flush=True,
            )

        if not sat_geometry:
            epochs_skipped += 1
            continue

        epoch_observations = []

        for sat, sat_xyz in sat_geometry.items():
            sat = str(sat).strip().upper()
            if not sat or sat[:1] not in allowed_systems:
                continue

            per_base_weighted_records: list[tuple[float, dict]] = []

            for base in normalized_bases:
                left_ep, right_ep, alpha = _find_bracketing_epochs(
                    epoch_datetimes=base["epoch_datetimes"],
                    epochs=base["epochs"],
                    target_epoch=target_epoch,
                    max_gap_seconds=max_gap_seconds,
                )

                if left_ep is None or right_ep is None:
                    continue

                rec0 = left_ep["obs_map"].get(sat)
                rec1 = right_ep["obs_map"].get(sat)

                if rec0 is None or rec1 is None:
                    continue

                interpolated_record = _interpolate_observation_record(
                    rec0=rec0,
                    rec1=rec1,
                    alpha=alpha,
                    allow_phase=False,
                )

                if not _obs_priority_has_code(interpolated_record):
                    continue

                transported = _transport_record_to_target(
                    sat=sat,
                    interpolated_record=interpolated_record,
                    sat_xyz=sat_xyz,
                    base_xyz=base["xyz"],
                    target_xyz=target_xyz,
                    allow_phase=False,
                )

                if not transported or not _obs_priority_has_code(transported):
                    continue

                weight = _base_weight(base["xyz"], target_xyz)
                per_base_weighted_records.append((weight, transported))

            merged = _weighted_merge_records(per_base_weighted_records)
            if not merged or not _obs_priority_has_code(merged):
                continue

            epoch_observations.append(
                {
                    "satellite": sat,
                    **merged,
                }
            )

        epoch_observations.sort(key=lambda item: item["satellite"])

        if idx_epoch < 3:
            print(
                f"[ADV-INT-EPOCH] idx={idx_epoch} target={epoch_iso} "
                f"epoch_observations={len(epoch_observations)} "
                f"sats={[item['satellite'] for item in epoch_observations[:12]]}",
                flush=True,
            )

        if len(epoch_observations) < min_satellites_per_epoch:
            epochs_skipped += 1
            continue

        output_epochs.append(
            {
                "epoch": epoch_iso,
                "observations": epoch_observations,
                "satellite_count": len(epoch_observations),
            }
        )

    return output_epochs, epochs_skipped


def interpolate_advanced_observations(
    time_grid: list,
    base_numeric_data: list[dict],
    geometry_cache: dict,
    config: dict,
) -> dict:
    try:
        requested_time_grid = []
        for t in time_grid or []:
            dt = _ensure_utc_datetime(t)
            if dt is not None:
                requested_time_grid.append(dt)

        requested_time_grid.sort()

        print(
            f"[ADV-INT] normalized_time_grid={len(requested_time_grid)}",
            flush=True,
        )

        if requested_time_grid:
            print(
                f"[ADV-INT] first_target={_epoch_to_iso(requested_time_grid[0])} "
                f"last_target={_epoch_to_iso(requested_time_grid[-1])}",
                flush=True,
            )

        if not requested_time_grid:
            return {
                "status": "failed",
                "message": "Time grid vazio.",
                "epochs_generated": 0,
                "epochs_skipped": 0,
                "interval_seconds": float(config.get("target_step_seconds", 1) or 1),
                "epochs": [],
                "summary": {},
            }

        target_x = _safe_float(config.get("target_x"))
        target_y = _safe_float(config.get("target_y"))
        target_z = _safe_float(config.get("target_z"))

        print(
            f"[ADV-INT] target_xyz_raw=({target_x}, {target_y}, {target_z})",
            flush=True,
        )

        if target_x is None or target_y is None or target_z is None:
            return {
                "status": "failed",
                "message": "Configuração do alvo sem target_x/target_y/target_z.",
                "epochs_generated": 0,
                "epochs_skipped": 0,
                "interval_seconds": float(config.get("target_step_seconds", 1) or 1),
                "epochs": [],
                "summary": {},
            }

        target_xyz = (target_x, target_y, target_z)

        geometry_epoch_map = _build_geometry_epoch_map(geometry_cache)
        normalized_bases = _normalize_base_epochs(base_numeric_data)

        print(
            f"[ADV-INT] geometry_epochs={len(geometry_epoch_map)} normalized_bases={len(normalized_bases)}",
            flush=True,
        )

        if geometry_epoch_map:
            geom_keys = sorted(geometry_epoch_map.keys())
            print(
                f"[ADV-INT] geometry_first={geom_keys[0]} geometry_last={geom_keys[-1]}",
                flush=True,
            )
            first_sats = list(
                sorted((geometry_epoch_map.get(geom_keys[0]) or {}).keys())
            )[:12]
            print(
                f"[ADV-INT] geometry_sample_sats={first_sats}",
                flush=True,
            )

        if not normalized_bases:
            return {
                "status": "failed",
                "message": "Nenhuma base normalizada com épocas observacionais válidas.",
                "epochs_generated": 0,
                "epochs_skipped": 0,
                "interval_seconds": float(config.get("target_step_seconds", 1) or 1),
                "epochs": [],
                "summary": {},
            }

        native_interval = _median_step_seconds(base_numeric_data, fallback=15.0)
        phase_mode = bool(config.get("interpolate_phase_observables", False))
        min_satellites_per_epoch = int(config.get("min_satellites_per_epoch", 4) or 4)
        max_gap_seconds = float(
            config.get(
                "max_interpolation_gap_seconds",
                max(30.0, native_interval * 2.5),
            )
        )
        allowed_systems = _allowed_systems_from_config(config, phase_mode=phase_mode)

        effective_real_epochs = min(len(b["epochs"]) for b in normalized_bases)

        if phase_mode:
            master_base = _choose_master_base(normalized_bases, target_xyz)
            if master_base is None:
                return {
                    "status": "failed",
                    "message": "Não foi possível selecionar base-mestra para o modo com fase.",
                    "epochs_generated": 0,
                    "epochs_skipped": 0,
                    "interval_seconds": native_interval,
                    "epochs": [],
                    "summary": {},
                }

            requested_step = float(config.get("target_step_seconds", native_interval) or native_interval)
            dense_phase = requested_step < native_interval
            normalized_time_grid = (
                requested_time_grid if dense_phase else _build_master_time_grid(master_base)
            )
            output_step_seconds = requested_step if dense_phase else native_interval

            print(
                f"[ADV-INT] requested_time_grid={len(requested_time_grid)} "
                f"effective_real_epochs={effective_real_epochs} "
                f"usable_bases={len(normalized_bases)} "
                f"target_xyz={target_xyz} internal_step={output_step_seconds:g} "
                f"native_interval={native_interval:g} output_step={output_step_seconds:g}",
                flush=True,
            )

            if dense_phase:
                output_epochs, epochs_skipped = _build_output_epochs_phase_dense(
                    master_base=master_base,
                    geometry_epoch_map=geometry_epoch_map,
                    target_xyz=target_xyz,
                    time_grid=requested_time_grid,
                    max_gap_seconds=max_gap_seconds,
                    min_satellites_per_epoch=min_satellites_per_epoch,
                )
            else:
                output_epochs, epochs_skipped = _build_output_epochs_phase_mode(
                    master_base=master_base,
                    geometry_epoch_map=geometry_epoch_map,
                    target_xyz=target_xyz,
                    min_satellites_per_epoch=min_satellites_per_epoch,
                )

            summary = {
                "mode": "phase_master_dense" if dense_phase else "phase_master_only",
                "master_station_code": master_base.get("station_code"),
                "master_distance_km": round(
                    _ecef_distance(*master_base["xyz"], *target_xyz) / 1000.0, 3
                ),
                "native_interval_seconds": native_interval,
                "target_step_seconds": output_step_seconds,
                "max_interpolation_gap_seconds": max_gap_seconds,
                "min_satellites_per_epoch": min_satellites_per_epoch,
                "geometry_status": geometry_cache.get("status"),
                "geometry_source": geometry_cache.get("source"),
                "geometry_epochs_with_satellites": geometry_cache.get(
                    "epochs_with_geometry", 0
                ),
                "bases_used": len(normalized_bases),
                "allowed_systems": sorted(allowed_systems),
            }

            obs_types = ["C1", "P1", "L1", "C2", "P2", "L2", "S1", "S2"]

        else:
            normalized_time_grid = requested_time_grid
            output_step_seconds = float(config.get("target_step_seconds", 1) or 1)

            print(
                f"[ADV-INT] requested_time_grid={len(requested_time_grid)} "
                f"effective_real_epochs={effective_real_epochs} "
                f"usable_bases={len(normalized_bases)} "
                f"target_xyz={target_xyz} internal_step={output_step_seconds:g} "
                f"native_interval={native_interval:g} output_step={output_step_seconds:g}",
                flush=True,
            )

            output_epochs, epochs_skipped = _build_output_epochs_code_mode(
                normalized_time_grid=normalized_time_grid,
                normalized_bases=normalized_bases,
                geometry_epoch_map=geometry_epoch_map,
                target_xyz=target_xyz,
                max_gap_seconds=max_gap_seconds,
                min_satellites_per_epoch=min_satellites_per_epoch,
                allowed_systems=allowed_systems,
            )

            summary = {
                "mode": "code_network_dense",
                "native_interval_seconds": native_interval,
                "target_step_seconds": output_step_seconds,
                "max_interpolation_gap_seconds": max_gap_seconds,
                "min_satellites_per_epoch": min_satellites_per_epoch,
                "geometry_status": geometry_cache.get("status"),
                "geometry_source": geometry_cache.get("source"),
                "geometry_epochs_with_satellites": geometry_cache.get(
                    "epochs_with_geometry", 0
                ),
                "bases_used": len(normalized_bases),
                "allowed_systems": sorted(allowed_systems),
            }

            obs_types = ["C1", "P1", "C2", "P2", "S1", "S2"]

        print(f"[ADV-INT] virtual_epochs_raw={len(output_epochs)}", flush=True)
        print(f"[ADV-INT] virtual_epochs_after_filter={len(output_epochs)}", flush=True)
        print(
            f"[ADV-INT] virtual_epochs={len(output_epochs)} "
            f"epochs_debug={len(output_epochs)} "
            f"output_step_seconds={output_step_seconds:g}",
            flush=True,
        )

        return {
            "status": "completed" if output_epochs else "failed",
            "message": (
                "Interpolação avançada concluída."
                if output_epochs
                else "A interpolação avançada não produziu épocas utilizáveis."
            ),
            "epochs_generated": len(output_epochs),
            "epochs_skipped": epochs_skipped,
            "interval_seconds": output_step_seconds,
            "obs_types": obs_types,
            "approx_x": target_x,
            "approx_y": target_y,
            "approx_z": target_z,
            "epochs": output_epochs,
            "summary": summary,
        }

    except Exception as exc:
        print(
            f"[ADV-INT-ERROR] {type(exc).__name__}: {exc}",
            flush=True,
        )
        return {
            "status": "failed",
            "message": f"Erro na interpolação avançada: {type(exc).__name__}: {exc}",
            "epochs_generated": 0,
            "epochs_skipped": 0,
            "interval_seconds": float(config.get("target_step_seconds", 1) or 1),
            "epochs": [],
            "summary": {},
        }
