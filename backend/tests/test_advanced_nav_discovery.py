"""Regressão do job 141: NAV .26n existia, mas o modo avançado recebia nav_path=None."""

from datetime import datetime

from app.services.vrs_advanced.advanced_nav_service import (
    resolve_advanced_navigation_files,
)
from tests.gnss_fixtures import make_record, write_rinex2_nav

SESSION = "2026-06-03"
TOC = datetime(2026, 6, 3, 12, 0, 0)


def _make_rbmc_day(root, station):
    day = root / "rbmc" / station.lower() / SESSION
    (day / "converted").mkdir(parents=True)
    (day / "extracted").mkdir(parents=True)
    (day / "zip").mkdir(parents=True)
    obs = day / "converted" / f"{station.lower()}1541.26o"
    obs.write_text("obs", encoding="utf-8")
    (day / "extracted" / f"{station.lower()}1541.26d").write_text("crx", encoding="utf-8")
    (day / "extracted" / f"{station.lower()}1541.26g").write_text("glo", encoding="utf-8")
    nav = write_rinex2_nav(
        day / "extracted" / f"{station.lower()}1541.26n", [(1, TOC, make_record(TOC))]
    )
    return obs, nav


def test_job141_layout_resolves_nav_from_extracted_folder(tmp_path):
    stations = ["APS1", "APMA", "BELE", "BEPA", "APLJ"]
    inputs = tmp_path / "processing" / "job_141_advanced" / "inputs"
    inputs.mkdir(parents=True)

    context = []
    expected = {}
    for idx, code in enumerate(stations, start=1):
        obs, nav = _make_rbmc_day(tmp_path, code)
        copied = inputs / f"cand{idx}_{obs.name}"
        copied.write_text("obs", encoding="utf-8")
        expected[code] = nav
        # Exatamente o que o pipeline antigo passava: só caminhos de observação.
        context.append({"station_code": code, "path": str(obs), "source": "candidate_paths"})
        context.append(
            {
                "station_code": code,
                "obs_path": str(copied),
                "copied_path": str(copied),
                "original_path": str(obs),
                "source": "prepared_candidates",
            }
        )

    resolved = resolve_advanced_navigation_files(context, session_date=SESSION)

    assert len(resolved) == 5  # um item por estação
    for item in resolved:
        assert item["status"] == "resolved"
        assert item["nav_path"] == str(expected[item["station_code"]])
        assert all(not p.endswith((".26g", ".26o", ".26d")) for p in item["nav_paths"])


def test_nav_found_by_station_and_date_when_only_copy_is_known(tmp_path):
    _, nav = _make_rbmc_day(tmp_path, "BELE")
    copy = tmp_path / "elsewhere" / "cand1_bele1541.26o"
    copy.parent.mkdir()
    copy.write_text("obs", encoding="utf-8")

    resolved = resolve_advanced_navigation_files(
        [{"station_code": "BELE", "copied_path": str(copy)}],
        session_date=SESSION,
        rbmc_root=tmp_path / "rbmc",
    )

    assert resolved[0]["nav_path"] == str(nav)


def test_explicit_nav_path_from_rbmc_sync_is_used(tmp_path):
    nav = write_rinex2_nav(tmp_path / "x" / "brdc1540.26n", [(1, TOC, make_record(TOC))])
    resolved = resolve_advanced_navigation_files(
        [{"station_code": "APLJ", "path": str(tmp_path / "missing.26o"), "nav_path": str(nav)}],
        session_date=SESSION,
        rbmc_root=tmp_path / "rbmc",
    )
    assert resolved[0]["nav_path"] == str(nav)


def test_missing_nav_is_reported_not_crashing(tmp_path):
    resolved = resolve_advanced_navigation_files(
        [{"station_code": "APLJ", "path": str(tmp_path / "missing.26o")}],
        session_date=SESSION,
        rbmc_root=tmp_path / "rbmc",
    )
    assert resolved[0]["nav_path"] is None
    assert resolved[0]["status"] == "nav_not_found"
