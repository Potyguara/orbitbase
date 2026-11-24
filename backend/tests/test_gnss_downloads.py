import gzip
import zipfile
from datetime import date, datetime

import pytest

from app.services import gnss_downloads_service as dl
from app.services.rinex_nav_satpos_service import parse_rinex_nav_gps
from app.services.vrs_unified.glonass_nav import parse_glonass_nav
from tests.gnss_fixtures import make_record, write_rinex2_nav, write_rinex3_nav

TOC = datetime(2026, 6, 3, 12, 0, 0)

ION_HEADER = (
    "GPSA   1.1176E-08  7.4506E-09 -5.9605E-08 -5.9605E-08       IONOSPHERIC CORR\n"
    "GPSB   8.8064E+04  4.9152E+04 -1.3107E+05 -3.2768E+05       IONOSPHERIC CORR\n"
    "GPUT  1.8626451492E-09 3.996802889E-15 319488 2422          TIME SYSTEM CORR\n"
    "    18                                                      LEAP SECONDS\n"
)


def _mixed_nav(tmp_path):
    path = write_rinex3_nav(
        tmp_path / "BRDC00IGS_R_20261540000_01D_MN.rnx",
        [(5, TOC, make_record(TOC)), (12, TOC, make_record(TOC, m0=1.1))],
    )
    text = path.read_text()
    text = text.replace("                                                            END OF HEADER",
                        ION_HEADER.rstrip("\n") + "\n" + " " * 60 + "END OF HEADER")
    # Um Galileo no fim: não pode ir para o .n
    text += "E11 2026 06 03 12 00 00" + " 1.000000000000E-04" * 3 + "\n"
    text += ("    " + " 1.000000000000E+00" * 4 + "\n") * 7
    path.write_text(text)
    return path


def test_converts_mixed_rinex3_into_rinex2_gps_and_glonass(tmp_path):
    src = _mixed_nav(tmp_path)
    out = dl.rinex3_nav_to_rinex2(src.read_text(), src.name)

    n_path = tmp_path / "brdc1540.26n"
    n_path.write_text(out["n"])
    lines = out["n"].splitlines()
    assert lines[0].startswith("     2.11           N: GPS NAV DATA")
    assert all(len(line) <= 80 for line in lines)
    assert any(line.endswith("ION ALPHA") for line in lines)
    assert any(line.endswith("DELTA-UTC: A0,A1,T,W") and " 319488     2422" in line for line in lines)
    assert any(line.endswith("LEAP SECONDS") for line in lines)
    assert " 5 26  6  3 12  0  0.0" in out["n"]

    original = parse_rinex_nav_gps(str(src))
    converted = parse_rinex_nav_gps(str(n_path))
    assert sorted(converted) == ["G05", "G12"]
    for sat in converted:
        a, b = original[sat][0], converted[sat][0]
        for key in ("sqrt_a", "m0", "toe", "af0", "omega0", "i0", "w"):
            assert a[key] == pytest.approx(b[key], rel=1e-12, abs=1e-15)

    g_path = tmp_path / "brdc1540.26g"
    g_path.write_text(out["g"])
    assert out["g"].splitlines()[0].startswith("     2.11           G: GLONASS NAV DATA")
    glo = parse_glonass_nav(str(g_path))
    assert list(glo) == ["R01"]
    assert len(out["g"].splitlines()) == 5 + 4  # cabeçalho (5) + 1 registro de 4 linhas
    assert dl._rinex2_nav_kind(n_path) == "n"
    assert dl._rinex2_nav_kind(g_path) == "g"


def test_converts_rinex4_only_lnav_and_fdma():
    rec = " 1.000000000000E-04" * 3
    orbit7 = ("    " + " 2.000000000000E+00" * 4 + "\n") * 7
    orbit5 = ("    " + " 3.000000000000E+00" * 4 + "\n") * 5
    text = (
        "     4.01           N: GNSS NAV DATA    M: MIXED            RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "> EPH G07 LNAV\n" "G07 2026 06 03 10 00 00" + rec + "\n" + orbit7
        + "> EPH G07 CNAV\n" "G07 2026 06 03 10 00 00" + rec + "\n" + orbit7
        + "> EPH R03 FDMA\n" "R03 2026 06 03 10 15 00" + rec + "\n" + orbit5
        + "> STO G01 GPUT\n" "    2026 06 03 00 00 00 GPUT\n"
    )
    out = dl.rinex3_nav_to_rinex2(text)
    assert out["n"].count(" 7 26  6  3 10  0  0.0") == 1
    g_lines = out["g"].splitlines()
    assert g_lines[-4].startswith(" 3 26  6  3 10 15  0.0")
    assert len(g_lines) == 3 + 4  # sem a 4ª linha extra do RINEX 3.05/4


def test_date_range_limits():
    assert dl.date_range("2026-10-03", "2026-10-01") == [
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]
    with pytest.raises(ValueError):
        dl.date_range("2026-01-01", "2026-03-01")
    with pytest.raises(ValueError):
        dl.date_range("2999-01-01")
    assert dl.brdc_rinex2_name(date(2026, 10, 1), "g") == "brdc2740.26g"


def test_daily_ephemeris_from_igs_brdc_and_cache(monkeypatch, tmp_path):
    from app.services.vrs_advanced import advanced_ephemeris_service as eph

    src = _mixed_nav(tmp_path / "src")
    calls = []

    def fake_download(url, dest, timeout_seconds=20, username=None, password=None):
        calls.append(url)
        dest.write_bytes(gzip.compress(src.read_bytes()))

    monkeypatch.setattr(eph, "_download_file", fake_download)
    monkeypatch.setattr(dl, "_rbmc_nav_files", lambda *a: pytest.fail("não deveria ir à RBMC"))
    day = date(2026, 6, 3)
    result = dl.ensure_daily_ephemeris(day, tmp_path / "storage")
    assert result["missing"] == []
    assert result["files"]["n"].endswith("brdc1540.26n")
    assert result["files"]["g"].endswith("brdc1540.26g")
    assert "IGS BRDC" in result["sources"]["g"]
    assert "igs.bkg.bund.de" in calls[0]

    calls.clear()
    again = dl.ensure_daily_ephemeris(day, tmp_path / "storage")
    assert again["files"] == result["files"] and not calls


def test_daily_ephemeris_falls_back_to_rbmc(monkeypatch, tmp_path):
    from app.services.vrs_advanced import advanced_ephemeris_service as eph

    def offline(*a, **k):
        raise eph.requests.ConnectionError("sem rede")

    monkeypatch.setattr(eph, "_download_file", offline)
    rbmc = tmp_path / "rbmc"
    n = write_rinex2_nav(rbmc / "apma1541.26n", [(5, TOC, make_record(TOC))])
    g = rbmc / "apma1541.26g"
    g.write_text("     2.11           G: GLONASS NAV DATA".ljust(60) + "RINEX VERSION / TYPE\n"
                 + " " * 60 + "END OF HEADER\n")
    asked = []

    def fake_rbmc(code, day):
        asked.append(code)
        return {"n": n, "g": g} if code == "APMA" else {}

    monkeypatch.setattr(dl, "_rbmc_nav_files", fake_rbmc)
    result = dl.ensure_daily_ephemeris(date(2026, 6, 3), tmp_path / "storage", fallback_stations=["apma"])
    assert asked == ["APMA"]
    assert result["sources"] == {"n": "RBMC APMA (apma1541.26n)", "g": "RBMC APMA (apma1541.26g)"}
    assert result["files"]["n"].endswith("brdc1540.26n")


def test_build_package_zip_layout(monkeypatch, tmp_path):
    eph_dir = tmp_path / "eph"
    eph_dir.mkdir()
    (eph_dir / "brdc2740.26n").write_text("n")
    (eph_dir / "brdc2740.26g").write_text("g")
    obs = tmp_path / "braz2741.26o"
    obs.write_text("o")

    monkeypatch.setattr(dl, "ensure_daily_ephemeris", lambda day, *a, **k: {
        "files": {"n": str(eph_dir / "brdc2740.26n"), "g": str(eph_dir / "brdc2740.26g")},
        "sources": {"n": "IGS BRDC", "g": "IGS BRDC"}, "missing": [], "rinex3": None})

    def fake_rbmc(code, day, convert):
        if code == "XXXX":
            raise FileNotFoundError("404")
        return [obs], []

    monkeypatch.setattr(dl, "collect_rbmc_day", fake_rbmc)
    task = tmp_path / "task"
    task.mkdir()
    out = dl.build_package(task, tmp_path, [date(2026, 10, 1)], True, False, ["BRAZ", "XXXX"], True)
    assert out.name == "orbitbase_20261001.zip"
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        readme = zf.read("LEIAME.txt").decode()
    assert names == {
        "2026-10-01 (dia 274)/Efemerides/brdc2740.26n",
        "2026-10-01 (dia 274)/Efemerides/brdc2740.26g",
        "2026-10-01 (dia 274)/RBMC/BRAZ/braz2741.26o",
        "LEIAME.txt",
    }
    assert "XXXX: sem dados" in readme


def test_collect_rbmc_day_swaps_hatanaka_for_converted(monkeypatch, tmp_path):
    from app.services import rbmc_observation_sync_service as rbmc

    extracted, converted = tmp_path / "extracted", tmp_path / "converted"
    extracted.mkdir()
    converted.mkdir()
    for name in ("braz2741.26d", "braz2741.26n", "braz2741.26g"):
        (extracted / name).write_text(name)
    (converted / "braz2741.26o").write_text("o")
    monkeypatch.setattr(rbmc, "ensure_rbmc_station_files", lambda code, d: {
        "extracted_root": str(extracted), "converted_root": str(converted), "day_root": str(tmp_path)})

    files, notes = dl.collect_rbmc_day("BRAZ", date(2026, 10, 1), True)
    assert sorted(p.name for p in files) == ["braz2741.26g", "braz2741.26n", "braz2741.26o"]
    assert not notes
    raw, _ = dl.collect_rbmc_day("BRAZ", date(2026, 10, 1), False)
    assert sorted(p.name for p in raw) == ["braz2741.26d", "braz2741.26g", "braz2741.26n"]
