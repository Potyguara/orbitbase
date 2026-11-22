"""Diagnóstico GLONASS do motor único (somente leitura).

Uso (no container backend, em /app/backend):
  python -m tools.glonass_diag --station APMA --date 2026-08-20 --utc 11:00 --minutes 20
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--utc", default="11:00")
    ap.add_argument("--minutes", type=int, default=20)
    args = ap.parse_args(argv)

    from app.services.vrs_advanced.advanced_observation_interpolation_service import (
        PHASE_RESIDUAL_MAX_JUMP_M, _obs_has_dual_phase_gps, _residuals_at_master_epoch,
    )
    from app.services.vrs_unified import glonass_nav as gn
    from app.services.vrs_unified.unified_service import (
        _glonass_nav_files, _load_station_epochs, _master_from_reader, _station_xyz_map,
        _wavelength_selector, add_glonass_geometry,
    )

    start = datetime.fromisoformat(f"{args.date}T{args.utc}:00").replace(tzinfo=timezone.utc)
    end = start + timedelta(minutes=args.minutes)
    code = args.station.upper()
    data = _load_station_epochs(code, args.date, start, end)
    xyz = _station_xyz_map([code])[code]
    master = _master_from_reader(data, xyz, code)
    report: dict = {"nav_paths": data.get("nav_paths")}

    files = _glonass_nav_files(data.get("nav_paths") or [])
    records = gn.merge_glonass_nav(files)
    report["glonass_files"] = files
    report["ephemeris_per_sat"] = {s: len(r) for s, r in sorted(records.items())}
    sample = next(iter(sorted(records.items())), (None, []))
    if sample[0]:
        r0 = sample[1][0]
        report["sample_ephemeris"] = {k: (str(v) if k == "toc" else v) for k, v in r0.items()}

    # consistência entre efemérides vizinhas: propaga a anterior até o toc da seguinte
    gaps = {}
    for sat, recs in sorted(records.items()):
        for a, b in zip(recs, recs[1:]):
            dt = (b["toc"] - a["toc"]).total_seconds()
            if 0 < dt <= 3600:
                st = gn.propagate(list(a["pos"]) + list(a["vel"]), a["acc"], dt)
                gaps.setdefault(sat, []).append(round(
                    sum((st[i] - b["pos"][i]) ** 2 for i in range(3)) ** 0.5, 1))
    report["ephemeris_handover_error_m"] = {s: v[:6] for s, v in list(gaps.items())[:8]}

    geometry: dict = {}
    channels = add_glonass_geometry(geometry, data.get("nav_paths") or [], start, end)
    report["channels"] = channels
    lam_for = _wavelength_selector(channels)

    per_epoch = []
    obs_types = Counter()
    reasons = Counter()
    series: dict[str, list] = {}
    for ep in master["epochs"]:
        if not (start <= ep["epoch"] <= end):
            continue
        geom = geometry.get(ep["epoch_iso"]) or {}
        r_obs = {s: rec for s, rec in ep["obs_map"].items() if s.startswith("R")}
        row = {"epoch": ep["epoch_iso"], "R_obs": len(r_obs), "R_geom": len(geom), "R_ok": 0}
        for sat, rec in r_obs.items():
            obs_types.update(k for k, v in rec.items() if v)
            if not _obs_has_dual_phase_gps(rec):
                reasons["sem dupla fase/código"] += 1
                continue
            if sat not in geom:
                reasons["sem geometria"] += 1
                continue
            lam = lam_for(sat)
            if lam is None:
                reasons["sem canal"] += 1
                continue
            res = _residuals_at_master_epoch(rec, geom[sat], master["xyz"], lam)
            if not res:
                reasons["resíduo vazio"] += 1
                continue
            row["R_ok"] += 1
            series.setdefault(sat, []).append((ep["epoch_iso"], res))
        per_epoch.append(row)

    jumps = Counter()
    sample_series = {}
    for sat, items in series.items():
        for (_, a), (_, b) in zip(items, items[1:]):
            for key in ("C1", "P2", "L1", "L2"):
                if key in a and key in b and abs(b[key] - a[key]) > PHASE_RESIDUAL_MAX_JUMP_M:
                    jumps[f"{sat}:{key}"] += 1
        if len(sample_series) < 4:
            sample_series[sat] = [
                {"t": t, **{k: round(v, 2) for k, v in r.items() if k in ("C1", "L1", "L2", "P2")}}
                for t, r in items[:6]
            ]
    report.update({
        "epochs": per_epoch[:8],
        "epochs_total": len(per_epoch),
        "obs_types_R": dict(obs_types),
        "drop_reasons": dict(reasons),
        "residual_jumps_gt_limit": dict(jumps),
        "residual_series_sample": sample_series,
    })
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
