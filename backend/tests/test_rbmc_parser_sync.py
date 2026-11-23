import threading

import pytest

from app.services import rbmc_sync_service
from app.services.rbmc_station_report_parser_service import (
    _parse_state,
    parse_rbmc_station_report_text,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Paraná", "PR"),
        ("Pará", "PA"),
        ("Minas Gerais", "MG"),
        ("Mato Grosso do Sul", "MS"),
        ("Mato Grosso", "MT"),
        ("São Paulo", "SP"),
        ("SP", "SP"),
        ("AL ", "AL"),
        ("Uruguai", None),
        ("Intendencia de Rivera", None),
        (None, None),
    ],
)
def test_parse_state(value, expected):
    assert _parse_state(value) == expected


def test_station_code_with_hyphen_layout():
    text = "Ident. da Estação - FORT\nNome da Estação: Fortaleza\nEstado: Ceará\n"
    data = parse_rbmc_station_report_text(text, file_name="Descritivo_FORT.pdf")
    assert data["station_code"] == "FORT"
    assert data["state"] == "CE"


def test_station_code_falls_back_to_file_name():
    data = parse_rbmc_station_report_text("Relatório sem código", file_name="Descritivo_UBAT.pdf")
    assert data["station_code"] == "UBAT"


def test_concurrent_sync_is_rejected(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_sync(db, storage_root):
        started.set()
        release.wait(5)
        return {}

    monkeypatch.setattr(rbmc_sync_service, "_sync_rbmc_stations", slow_sync)
    t = threading.Thread(target=rbmc_sync_service.sync_rbmc_stations, args=(None, "/tmp"))
    t.start()
    assert started.wait(5)
    with pytest.raises(rbmc_sync_service.RbmcSyncInProgress):
        rbmc_sync_service.sync_rbmc_stations(None, "/tmp")
    release.set()
    t.join()


def test_failure_does_not_undo_previous_stations(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import GnssStation

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    texts = {
        "Descritivo_AAAA.pdf": "Ident. da Estação: AAAA\nNome da Estação: A\n",
        "Descritivo_BBBB.pdf": "",  # falha: PDF sem texto
        "Descritivo_CCCC.pdf": "Ident. da Estação - CCCC\nNome da Estação: C\n",
    }
    monkeypatch.setattr(rbmc_sync_service, "RBMC_REPORT_FILES", list(texts))
    monkeypatch.setattr(rbmc_sync_service, "_download_file", lambda url, dest: dest)
    monkeypatch.setattr(
        rbmc_sync_service, "_extract_pdf_text_pypdf", lambda path: texts[path.name]
    )

    result = rbmc_sync_service.sync_rbmc_stations(db, str(tmp_path))

    assert result["inserted"] == 2 and result["failed"] == 1
    codes = {s.station_code for s in db.query(GnssStation).all()}
    assert codes == {"AAAA", "CCCC"}
