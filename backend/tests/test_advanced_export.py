import io
import zipfile

from app.routers.vrs_advanced import advanced_rinex_export_name, build_advanced_export_zip


def test_rinex_name():
    assert advanced_rinex_export_name("BASE_ELMER", "2026-08-20") == "BASE2320.26o"
    assert advanced_rinex_export_name(None, "2026-08-20") == "ADVB2320.26o"
    assert advanced_rinex_export_name("ab", "2026-01-01") == "AB000010.26o"


def test_export_zip(tmp_path):
    assert build_advanced_export_zip(tmp_path, "X.26o") is None
    results = tmp_path / "results"
    results.mkdir()
    (results / "ADVANCED_BASE.obs").write_text("RINEX")
    (results / "advanced_debug_report.json").write_text("{}")
    data = build_advanced_export_zip(tmp_path, "BASE2320.26o")
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert names == ["BASE2320.26o", "relatorios/advanced_debug_report.json"]
