import gzip
from types import SimpleNamespace

import pytest
import requests

from app.services.vrs_advanced import advanced_ephemeris_service as eph

VALID_SP3 = (
    "#dP2026  6  3  0  0  0.00000000      96 ORBIT IGS20 HLM  IGS\n"
    + "## 2400 259200.00000000   900.00000000 61194 0.0000000000000\n" * 3
)


def _prepared(url, auth=True):
    req = requests.Request("GET", url, headers={"Authorization": "Basic x"} if auth else {})
    return req.prepare()


def _response_from(url):
    return SimpleNamespace(request=SimpleNamespace(url=url))


def test_earthdata_session_keeps_auth_on_cddis_to_urs_redirect():
    session = eph.EarthdataSession()
    prep = _prepared("https://urs.earthdata.nasa.gov/oauth/authorize?x=1")
    session.rebuild_auth(prep, _response_from("https://cddis.nasa.gov/archive/x"))
    assert "Authorization" in prep.headers


def test_earthdata_session_drops_auth_for_other_hosts():
    session = eph.EarthdataSession()
    prep = _prepared("https://evil.example.com/steal")
    session.rebuild_auth(prep, _response_from("https://cddis.nasa.gov/archive/x"))
    assert "Authorization" not in prep.headers


def test_html_detection_ignores_gzip_and_flags_login(tmp_path):
    gz = tmp_path / "a.sp3.gz"
    gz.write_bytes(gzip.compress(b"<!DOCTYPE html> not really"))
    assert eph._looks_like_html_response(gz) is False

    html = tmp_path / "b.sp3.gz"
    html.write_text("<!DOCTYPE html><title>Earthdata Login</title>", encoding="utf-8")
    assert eph._looks_like_html_response(html) is True


def test_download_rejects_html_content_type(monkeypatch, tmp_path):
    class FakeResp:
        url = "https://urs.earthdata.nasa.gov/oauth/authorize"
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(eph.EarthdataSession, "get", lambda self, *a, **k: FakeResp())
    with pytest.raises(eph.HtmlInsteadOfFileError):
        eph._download_file("https://cddis.nasa.gov/x.gz", tmp_path / "x.gz", username="u", password="p")


def test_product_names_for_recent_dates_use_igs_long_names():
    names = eph.sp3_product_filenames("2026-06-03")
    assert names[0] == "IGS0OPSFIN_20261540000_01D_15M_ORB.SP3.gz"
    assert names[1] == "IGS0OPSRAP_20261540000_01D_15M_ORB.SP3.gz"
    assert any(n.startswith("IGS0OPSULT_2026154") for n in names)
    assert not any(n.startswith("igs") for n in names)

    legacy = eph.sp3_product_filenames("2020-01-01")
    assert any(n.startswith("igs2086") for n in legacy)


def test_candidates_skip_cddis_without_credentials():
    cands = eph._build_sp3_candidates("2026-06-03")
    assert cands
    assert all("cddis" not in c.url for c in cands)
    assert {c.source for c in cands} >= {"bkg", "ign"}

    with_auth = eph._build_sp3_candidates("2026-06-03", "user", "pass")
    assert with_auth[0].source == "cddis"


def test_mirrors_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("ORBITBASE_SP3_MIRRORS", "local=https://mirror.local/{week}/")
    cands = eph._build_sp3_candidates("2026-06-03")
    assert all(c.url.startswith("https://mirror.local/2421/") for c in cands)


def test_try_download_skips_dead_hosts_and_uses_next_mirror(monkeypatch, tmp_path):
    calls = []

    def fake_download(url, dest, timeout_seconds=20, username=None, password=None):
        calls.append(url)
        if "cddis" in url:
            raise eph.HtmlInsteadOfFileError("login page")
        if "ign.fr" in url:
            raise requests.ConnectTimeout("timed out")
        if "bkg" in url and "OPSFIN" in url:
            resp = SimpleNamespace(status_code=404)
            raise requests.HTTPError(response=resp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.compress(VALID_SP3.encode()))

    monkeypatch.setattr(eph, "_download_file", fake_download)
    # BKG antes de IGN para exercitar o 404 -> próximo produto.
    monkeypatch.setenv(
        "ORBITBASE_SP3_MIRRORS",
        "cddis=https://cddis.nasa.gov/archive/gnss/products/{week}/,"
        "ign=https://igs.ign.fr/pub/igs/products/{week}/,"
        "bkg=https://igs.bkg.bund.de/root_ftp/IGS/products/{week}/",
    )

    result = eph.ensure_precise_ephemeris_products(
        session_date="2026-06-03",
        storage_root=tmp_path,
        earthdata_username="user",
        earthdata_password="pass",
    )

    assert result["sp3_available"] is True
    assert "OPSRAP" in result["sp3_source_filename"]
    assert result["earthdata_auth_failed"] is True
    assert sum("cddis" in u for u in calls) == 1  # não insiste após o login falhar
    assert sum("ign.fr" in u for u in calls) == 1  # não insiste após timeout
    assert "igs.ign.fr" in result["unreachable_hosts"]


def test_brdc_download_validates_rinex_nav(monkeypatch, tmp_path):
    nav_text = (
        "     3.04           N: GNSS NAV DATA    M: Mixed            RINEX VERSION / TYPE\n"
        + "x" * 300
        + "\n"
    )

    def fake_download(url, dest, timeout_seconds=20, username=None, password=None):
        dest.write_bytes(gzip.compress(nav_text.encode()))

    monkeypatch.setattr(eph, "_download_file", fake_download)
    result = eph.ensure_broadcast_navigation("2026-06-03", tmp_path)
    assert result["nav_available"] is True
    assert result["nav_path"].endswith("BRDC00WRD_R_20261540000_01D_MN.rnx")

    # Segunda chamada usa cache.
    monkeypatch.setattr(eph, "_download_file", lambda *a, **k: pytest.fail("não deveria baixar"))
    assert eph.ensure_broadcast_navigation("2026-06-03", tmp_path)["cached"] is True
