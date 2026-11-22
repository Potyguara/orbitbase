"""Avaliação do motor VRS básico em pontos conhecidos.

Gera uma base virtual exatamente sobre uma estação RBMC (deixada fora da
rede) e compara as observações virtuais com as observações reais gravadas por
ela. Nada é gravado no banco: o job é um objeto em memória e a pasta de saída
(storage/processing/job_<id>) é apagada no fim, a menos que --keep seja usado.

Métricas (GPS, combinação livre da ionosfera, elevação >= 10°):
- code_bias_enu_m: deslocamento de posição que o código virtual "codifica"
  em relação à estação real (mínimos quadrados com relógio por época).
- code_rms_m: resíduo RMS do código após esse ajuste.
- phase_rate_rms_cm: RMS das diferenças entre épocas da fase (virtual - real),
  centradas por época; mede ruído/inconsistência da fase transportada.
- phase_drift_max_cm: maior deriva acumulada da fase num arco contínuo.

Uso (dentro do container backend):
  python -m tools.vrs_known_point_eval --target APS1 --date 2026-08-20 \
      --bases APMA,APLJ,BELE,BEPA,BOAV --start 08:00 --minutes 5,10,20,60
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

F1 = 1575.42e6
F2 = 1227.60e6
C = 299792458.0
L1_WL = C / F1
L2_WL = C / F2
IF_A = F1**2 / (F1**2 - F2**2)
IF_B = F2**2 / (F1**2 - F2**2)
WGS_A = 6378137.0
WGS_E2 = 6.69437999014e-3
LOCAL_TZ = ZoneInfo("America/Belem")
# salto na taxa centrada da fase livre da ionosfera (m) tratado como perda de ciclo;
# 1 ciclo em L1 vira ~0,48 m na combinação, então 0,10 m pega saltos pequenos
SLIP_IF_RATE_M = 0.10
# salto na segunda diferença da combinação geometry-free (m) entre épocas
GF_SLIP_M = 0.05


def ecef_to_geodetic(x, y, z):
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS_E2))
    h = 0.0
    for _ in range(6):
        n = WGS_A / math.sqrt(1 - WGS_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def enu_basis(lat_deg, lon_deg):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    e = (-math.sin(lon), math.cos(lon), 0.0)
    n = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat))
    u = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
    return e, n, u


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm_sat(code: str) -> str:
    s = (code or "").strip().upper()
    if s.isdigit():
        return f"G{int(s):02d}"
    if len(s) == 2 and s[0].isalpha():
        return f"{s[0]}0{s[1]}"
    return s


def parse_rinex2(path: Path) -> tuple[dict, dict]:
    """Retorna (header, {epoch_naive_iso: {sat: {obs: valor}}}) para RINEX 2.x."""
    from app.services.vrs_advanced.advanced_rinex_observation_reader_service import (
        extract_rinex_observations_for_window,
    )
    from datetime import timezone

    data = extract_rinex_observations_for_window(
        path,
        datetime(1980, 1, 1, tzinfo=timezone.utc),
        datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    out: dict[str, dict] = {}
    for ep in data["epochs"]:
        key = ep["epoch_utc"].replace("Z", "").split("+")[0]
        out[key] = {
            norm_sat(item["satellite"]): item["observations"] for item in ep["satellites"]
        }
    return {"antenna_delta_hen": data.get("antenna_delta_hen") or [0, 0, 0]}, out


def iono_free(obs: dict) -> tuple[float | None, float | None]:
    c1 = obs.get("C1") or obs.get("P1")
    p2 = obs.get("P2") or obs.get("C2")
    l1 = obs.get("L1")
    l2 = obs.get("L2")
    pc = IF_A * c1 - IF_B * p2 if c1 and p2 else None
    lc = IF_A * l1 * L1_WL - IF_B * l2 * L2_WL if l1 and l2 else None
    return pc, lc


def solve3(ata, atb):
    # Cramer 3x3
    def det(m):
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    d = det(ata)
    if abs(d) < 1e-12:
        return None
    sol = []
    for k in range(3):
        m = [row[:] for row in ata]
        for i in range(3):
            m[i][k] = atb[i]
        sol.append(det(m) / d)
    return sol


def _sat_velocity(sat_cache, key, sat):
    t = datetime.fromisoformat(key)
    for step in (1, 15, 30):
        a = (sat_cache.get((t - timedelta(seconds=step)).isoformat()) or {}).get(sat)
        b = (sat_cache.get((t + timedelta(seconds=step)).isoformat()) or {}).get(sat)
        if a and b:
            return tuple((b[k] - a[k]) / (2 * step) for k in ("x", "y", "z"))
    return None


def compare(vrs_epochs, real_epochs, real_header, marker_xyz, sat_cache, min_elev=10.0,
            clock_rate_corr=True):
    """clock_rate_corr: remove o efeito do relógio de cada receptor na etiqueta de
    tempo (-ρ̇·Δδt). A VRS herda o relógio da mestra e a estação real tem o seu;
    Δδt vem da média por época da diferença de código."""
    lat, lon, _ = ecef_to_geodetic(*marker_xyz)
    e_ax, n_ax, u_ax = enu_basis(lat, lon)
    dh = float(real_header["antenna_delta_hen"][0] or 0.0)

    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0] * 3
    rows_all = []
    phase_prev: dict[str, tuple[float, float]] = {}
    phase_rates = []
    drift: dict[str, float] = {}
    drift_max = 0.0
    slips = 0
    epochs_used = 0
    sats_seen = set()
    key_prev = None
    phase_rows = []
    slip_marks = set()
    clock_diffs = []

    for key in sorted(set(vrs_epochs) & set(real_epochs)):
        geom = sat_cache.get(key) or {}
        sd_code, sd_phase, ep_rows = [], [], []
        rho_dot = {}
        for sat, v_obs in vrs_epochs[key].items():
            if not sat.startswith("G") or sat not in real_epochs[key] or sat not in geom:
                continue
            sxyz = geom[sat]
            d = (sxyz["x"] - marker_xyz[0], sxyz["y"] - marker_xyz[1], sxyz["z"] - marker_xyz[2])
            r = math.sqrt(dot(d, d))
            u = (d[0] / r, d[1] / r, d[2] / r)
            elev = math.degrees(math.asin(dot(u, u_ax)))
            if elev < min_elev:
                continue
            vel = _sat_velocity(sat_cache, key, sat)
            rho_dot[sat] = dot(u, vel) if vel else 0.0
            pc_v, lc_v = iono_free(v_obs)
            pc_r, lc_r = iono_free(real_epochs[key][sat])
            # obs reais no ARP (marco + dh): distância menor em dh*sin(elev)
            arp_corr = dh * math.sin(math.radians(elev))
            if pc_v is not None and pc_r is not None:
                sd_code.append((sat, pc_v - pc_r - arp_corr, u))
            if lc_v is not None and lc_r is not None:
                sd_phase.append((sat, lc_v - lc_r - arp_corr))
                ep_rows.append((key, sat, lc_v - lc_r - arp_corr, u))
            sats_seen.add(sat)

        if clock_rate_corr and sd_code:
            dtd = sum(x[1] for x in sd_code) / len(sd_code) / C
            clock_diffs.append(dtd)
            sd_code = [(sat, sd + rho_dot[sat] * dtd, u) for sat, sd, u in sd_code]
            sd_phase = [(sat, sd + rho_dot[sat] * dtd) for sat, sd in sd_phase]
            ep_rows = [(k, sat, sd + rho_dot[sat] * dtd, u) for k, sat, sd, u in ep_rows]
        phase_rows.extend(ep_rows)

        if len(sd_code) >= 4:
            epochs_used += 1
            mean_sd = sum(x[1] for x in sd_code) / len(sd_code)
            mean_u = [sum(x[2][i] for x in sd_code) / len(sd_code) for i in range(3)]
            for sat, sd, u in sd_code:
                a = [-(u[i] - mean_u[i]) for i in range(3)]
                y = sd - mean_sd
                rows_all.append((a, y))
                for i in range(3):
                    atb[i] += a[i] * y
                    for j in range(3):
                        ata[i][j] += a[i] * a[j]

        rates = []
        for sat, sd in sd_phase:
            if sat in phase_prev and phase_prev[sat][0] == key_prev:
                rates.append((sat, sd - phase_prev[sat][1]))
        if len(rates) >= 3:
            mean_rate = sum(x[1] for x in rates) / len(rates)
            for sat, rate in rates:
                centered = rate - mean_rate
                if abs(centered) > SLIP_IF_RATE_M:
                    slips += 1
                    slip_marks.add((key, sat))
                    drift[sat] = 0.0
                    continue
                phase_rates.append(centered)
                drift[sat] = drift.get(sat, 0.0) + centered
                drift_max = max(drift_max, abs(drift[sat]))
        current = {sat for sat, _ in sd_phase}
        for sat in list(drift):
            if sat not in current:
                drift.pop(sat)
        phase_prev = {sat: (key, sd) for sat, sd in sd_phase}
        key_prev = key

    dx = solve3(ata, atb) if rows_all else None
    result = {
        "epochs_compared": epochs_used,
        "satellites_compared": len(sats_seen),
        "code_bias_enu_m": None,
        "code_rms_m": None,
        "phase_rate_rms_cm": None,
        "phase_drift_max_cm": round(drift_max * 100, 1),
        "phase_slips": slips,
    }
    if dx:
        res = [y - sum(a[i] * dx[i] for i in range(3)) for a, y in rows_all]
        result["code_rms_m"] = round(math.sqrt(sum(r * r for r in res) / len(res)), 3)
        result["code_bias_enu_m"] = [round(dot(dx, ax), 3) for ax in (e_ax, n_ax, u_ax)]
    pb = phase_position_bias(phase_rows, slip_marks, key_order=sorted(set(vrs_epochs) & set(real_epochs)))
    if pb:
        result["phase_bias_enu_m"] = [round(dot(pb["dx"], ax), 3) for ax in (e_ax, n_ax, u_ax)]
        result["phase_bias_rms_cm"] = pb["rms_cm"]
        if pb.get("cov"):
            q = pb["cov"]
            result["phase_bias_sigma_enu_m"] = [
                round(math.sqrt(max(0.0, sum(ax[i] * q[i][j] * ax[j]
                                             for i in range(3) for j in range(3)))), 3)
                for ax in (e_ax, n_ax, u_ax)
            ]
    if clock_diffs:
        result["clock_diff_us_mean"] = round(1e6 * sum(clock_diffs) / len(clock_diffs), 2)
        result["clock_diff_us_maxabs"] = round(1e6 * max(abs(x) for x in clock_diffs), 2)
    result["clock_rate_corr"] = clock_rate_corr
    common_keys = sorted(set(vrs_epochs) & set(real_epochs))
    result["gf_slips_vrs"] = gf_slip_count(vrs_epochs, common_keys)
    result["gf_slips_real"] = gf_slip_count(real_epochs, common_keys)
    if phase_rates:
        result["phase_rate_rms_cm"] = round(
            100 * math.sqrt(sum(r * r for r in phase_rates) / len(phase_rates)), 2
        )
    return result



def phase_position_bias(rows, slip_marks, key_order, iterations=30):
    """Deslocamento de posição (m, ECEF) que a fase virtual codifica.

    Modelo: sd = -u·dx + relógio(época) + ambiguidade(arco) + ruído. Relógio e
    ambiguidades são eliminados por projeções alternadas (centragem por época e
    por arco contínuo); depois resolve dx por mínimos quadrados.
    """
    if len(rows) < 20:
        return None
    index = {k: i for i, k in enumerate(key_order)}
    last_seen: dict[str, int] = {}
    arc_of: dict[str, int] = {}
    arc_counter = 0
    data = []
    for key, sat, sd, u in sorted(rows, key=lambda r: index[r[0]]):
        i = index[key]
        if sat not in arc_of or (key, sat) in slip_marks or last_seen.get(sat, -9) != i - 1:
            arc_counter += 1
            arc_of[sat] = arc_counter
        last_seen[sat] = i
        data.append([key, arc_of[sat], sd, -u[0], -u[1], -u[2]])

    def demean(col_idx, group_idx):
        sums, counts = {}, {}
        for r in data:
            g = r[group_idx]
            sums[g] = sums.get(g, 0.0) + r[col_idx]
            counts[g] = counts.get(g, 0) + 1
        for r in data:
            r[col_idx] -= sums[r[group_idx]] / counts[r[group_idx]]

    for _ in range(iterations):
        for c in (2, 3, 4, 5):
            demean(c, 0)
            demean(c, 1)
    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0] * 3
    for r in data:
        a = r[3:6]
        for i in range(3):
            atb[i] += a[i] * r[2]
            for j in range(3):
                ata[i][j] += a[i] * a[j]
    dx = solve3(ata, atb)
    if not dx:
        return None
    res = [r[2] - sum(r[3 + i] * dx[i] for i in range(3)) for r in data]
    rms = math.sqrt(sum(x * x for x in res) / len(res))
    return {"dx": dx, "rms_cm": round(rms * 100, 2), "cov": _cov3(ata, rms)}


def _cov3(ata, sigma):
    m = ata
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if abs(det) < 1e-18:
        return None
    inv = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            r = [k for k in range(3) if k != j]
            c = [k for k in range(3) if k != i]
            minor = m[r[0]][c[0]] * m[r[1]][c[1]] - m[r[0]][c[1]] * m[r[1]][c[0]]
            inv[i][j] = ((-1) ** (i + j)) * minor / det
    return [[inv[i][j] * sigma * sigma for j in range(3)] for i in range(3)]


def gf_slip_count(epochs: dict, keys) -> int:
    """Conta saltos na combinação geometry-free (L1·λ1 − L2·λ2) de um receptor."""
    hist: dict[str, list] = {}
    slips = 0
    for k in keys:
        for sat, obs in (epochs.get(k) or {}).items():
            if not sat.startswith("G") or not obs.get("L1") or not obs.get("L2"):
                hist.pop(sat, None)
                continue
            gf = obs["L1"] * L1_WL - obs["L2"] * L2_WL
            h = hist.setdefault(sat, [])
            h.append(gf)
            if len(h) >= 3:
                second = h[-1] - 2 * h[-2] + h[-3]
                if abs(second) > GF_SLIP_M:
                    slips += 1
                    hist[sat] = [gf]
                else:
                    del h[0]
        for sat in list(hist):
            if sat not in (epochs.get(k) or {}):
                hist.pop(sat)
    return slips


def run_case(job_id, target_code, base_codes, date, start_local, minutes, keep=False,
             engine="basic", network_mode="master", step_seconds=15):
    from app.db import SessionLocal
    from app.models import GnssStation
    from app.services.hatanaka_service import convert_hatanaka_to_rinex
    from app.services.rbmc_observation_sync_service import ensure_rbmc_station_files
    from app.services.vrs_processing_service import process_real_vrs
    from app.services.vrs_relative_correction_service import (
        build_satellite_xyz_cache_from_rinex_nav,
    )

    db = SessionLocal()
    try:
        st = db.query(GnssStation).filter(GnssStation.station_code == target_code).first()
        if st is None or st.x is None:
            raise SystemExit(f"Estação {target_code} sem coordenadas no banco")
        marker_xyz = (st.x, st.y, st.z)
    finally:
        db.close()

    lat, lon, h = ecef_to_geodetic(*marker_xyz)
    start_dt = datetime.fromisoformat(f"{date}T{start_local}:00")
    end_dt = start_dt + timedelta(minutes=minutes)
    job = SimpleNamespace(
        id=job_id,
        name=f"EVAL_{target_code}_{minutes}",
        session_date=date,
        start_time=start_dt.strftime("%H:%M"),
        end_time=end_dt.strftime("%H:%M"),
        occupation_minutes=minutes,
        requested_minutes=minutes,
        target_lat=lat,
        target_lon=lon,
        target_height=h,
        selected_station_codes_json=json.dumps(base_codes),
        network_validation_status="completed",
        network_validation_summary="eval",
        output_rinex_name="EVAL",
        output_marker_name=target_code,
    )

    case = {"target": target_code, "minutes": minutes, "start_local": start_local,
            "engine": engine if engine == "basic" else f"unified-{network_mode}",
            "bases_offered": base_codes}
    storage_root = Path(__file__).resolve().parents[1] / "storage"
    if engine == "basic":
        try:
            result = process_real_vrs(job, profile_config=None)
        except Exception as exc:  # noqa: BLE001
            case["error"] = f"{type(exc).__name__}: {exc}"
            return case
        job_root = storage_root / "processing" / f"job_{job_id}"
        vrs_path = job_root / "results" / "base_ppp.obs"
        if not vrs_path.exists():
            candidates = sorted((job_root / "results").glob("*.obs"))
            vrs_path = candidates[0] if candidates else vrs_path
    else:
        from app.services.vrs_unified.unified_service import process_unified_vrs, unified_root

        job.end_time = end_dt.strftime("%H:%M:%S")
        job.start_time = start_dt.strftime("%H:%M:%S")
        try:
            result = process_unified_vrs(
                job, storage_root=storage_root, step_seconds=step_seconds,
                network_mode=network_mode,
            )
        except Exception as exc:  # noqa: BLE001
            case["error"] = f"{type(exc).__name__}: {exc}"
            shutil.rmtree(unified_root(storage_root, job_id), ignore_errors=True)
            return case
        job_root = unified_root(storage_root, job_id)
        vrs_path = Path(result["rinex_path"])
        for k in ("master_station", "master_distance_km", "epochs_written", "epochs_skipped"):
            if k in result:
                case[k] = result[k]

    summary = json.dumps(result, default=str)
    for k in ("ppp_final_source_mode", "master_station", "reference_base"):
        if k in result:
            case[k] = result[k]
    case["fallback_to_reference_base"] = "reference_base_fallback" in summary

    state = ensure_rbmc_station_files(target_code, date) or {}
    real_obs = state.get("best_observation_file")
    nav = state.get("best_navigation_file")
    if not real_obs or not nav:
        case["error"] = "arquivos reais da estação alvo indisponíveis"
        return case
    real_path = Path(convert_hatanaka_to_rinex(str(real_obs)))

    vrs_header, vrs_epochs = parse_rinex2(vrs_path)
    real_header, real_epochs = parse_rinex2(real_path)
    utc0 = start_dt.replace(tzinfo=LOCAL_TZ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    utc1 = end_dt.replace(tzinfo=LOCAL_TZ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    sat_cache = build_satellite_xyz_cache_from_rinex_nav(
        nav_file_path=str(nav), start_dt=utc0 - timedelta(minutes=1),
        end_dt=utc1 + timedelta(minutes=1), step_seconds=1 if minutes <= 20 else 15,
    )
    case["vrs_epochs"] = len(vrs_epochs)
    case.update(compare(vrs_epochs, real_epochs, real_header, marker_xyz, sat_cache))
    raw = compare(vrs_epochs, real_epochs, real_header, marker_xyz, sat_cache,
                  clock_rate_corr=False)
    case["phase_bias_enu_m_sem_corr_relogio"] = raw.get("phase_bias_enu_m")

    if not keep:
        shutil.rmtree(job_root, ignore_errors=True)
    return case


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="Estações-alvo, separadas por vírgula")
    ap.add_argument("--bases", required=True, help="Estações da rede (o alvo é removido)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--start", default="08:00", help="Hora local HH:MM")
    ap.add_argument("--minutes", default="5,10,20,60")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--engine", default="basic", choices=["basic", "unified"])
    ap.add_argument("--network-mode", default="master", choices=["master", "basic"])
    ap.add_argument("--step", type=int, default=15)
    ap.add_argument("--out", default=None, help="Arquivo JSON de saída")
    args = ap.parse_args(argv)

    targets = [t.strip().upper() for t in args.target.split(",") if t.strip()]
    bases = [b.strip().upper() for b in args.bases.split(",") if b.strip()]
    results = []
    job_id = 990001
    for target in targets:
        network = [b for b in bases if b != target]
        for minutes in [int(m) for m in args.minutes.split(",")]:
            case = run_case(job_id, target, network, args.date, args.start, minutes, args.keep,
                            engine=args.engine, network_mode=args.network_mode,
                            step_seconds=args.step)
            job_id += 1
            results.append(case)
            print("[EVAL]", json.dumps(case, default=str, ensure_ascii=False), flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
