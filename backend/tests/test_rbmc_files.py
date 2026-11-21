from types import SimpleNamespace
import json

from app.services import rbmc_observation_sync_service as rbmc
from app.services import vrs_processing_service as vps


def test_find_station_files_separates_obs_and_gps_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(rbmc, "_rbmc_storage_root", lambda: tmp_path)
    day = tmp_path / "aplj" / "2026-06-03"
    (day / "extracted").mkdir(parents=True)
    (day / "converted").mkdir(parents=True)
    for name in (
        "extracted/aplj1541.26d",
        "extracted/aplj1541.26n",
        "extracted/aplj1541.26g",
        "extracted/APLJ00BRA_R_20261540000_01D_MN.rnx",
        "extracted/APLJ00BRA_R_20261540000_01D_30S_MO.rnx",
        "converted/aplj1541.26o",
    ):
        (day / name).write_text("x", encoding="utf-8")

    state = rbmc._find_station_files("APLJ", "2026-06-03")

    assert state["best_observation_file"].endswith("converted/aplj1541.26o")
    assert state["best_navigation_file"].endswith("aplj1541.26n")
    assert not any(p.endswith(("_MN.rnx", ".26n", ".26g")) for p in state["observation_files"])
    assert any(p.endswith("_MN.rnx") for p in state["navigation_files"])
    assert not any(p.endswith(".26g") for p in state["navigation_files"])


def test_candidate_base_paths_carry_nav_paths(monkeypatch):
    def fake_ensure(code, date):
        return {
            "day_root": f"/s/rbmc/{code.lower()}/{date}",
            "best_observation_file": f"/s/rbmc/{code.lower()}/{date}/converted/x.26o",
            "best_navigation_file": f"/s/rbmc/{code.lower()}/{date}/extracted/x.26n",
            "navigation_files": [f"/s/rbmc/{code.lower()}/{date}/extracted/x.26n"],
        }

    monkeypatch.setattr(vps, "ensure_rbmc_station_files", fake_ensure)
    job = SimpleNamespace(
        selected_station_codes_json=json.dumps(["APS1", "BELE", "APLJ"]),
        session_date="2026-06-03",
    )

    candidates = vps._resolve_candidate_base_paths(job)

    assert [c["station_code"] for c in candidates] == ["APS1", "BELE", "APLJ"]
    assert all(c["nav_path"].endswith(".26n") for c in candidates)
    assert all(c["nav_paths"] for c in candidates)
