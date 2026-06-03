from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from sqlalchemy.orm import Session

from app.models import GnssStation
from app.services.rbmc_station_report_parser_service import (
    parse_rbmc_station_report_text,
)
from app.services.rbmc_station_report_parser_service import (
    parse_rbmc_station_report_text,
    infer_station_code_from_filename,
)

RBMC_DOWNLOADS_URL = (
    "https://www.ibge.gov.br/geociencias/informacoes-sobre-posicionamento-geodesico/"
    "rede-geodesica/16258-rede-brasileira-de-monitoramento-continuo-dos-sistemas-gnss-rbmc.html?=&t=downloads"
)

RBMC_FILES_BASE_URL = (
    "https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/"
    "rbmc/relatorio/"
)

REQUEST_HEADERS = {"User-Agent": "OrbitBase-RBMC-Sync/1.0"}

RBMC_REPORT_FILES = [
    "Descritivo_ALAR.pdf",
    "Descritivo_ALMA.pdf",
    "Descritivo_ALMC.pdf",
    "Descritivo_AMBC.pdf",
    "Descritivo_AMCO.pdf",
    "Descritivo_AMCR.pdf",
    "Descritivo_AMHA.pdf",
    "Descritivo_AMHU.pdf",
    "Descritivo_AMMU.pdf",
    "Descritivo_AMPR.pdf",
    "Descritivo_AMPT.pdf",
    "Descritivo_AMTA.pdf",
    "Descritivo_AMTE.pdf",
    "Descritivo_AMTG.pdf",
    "Descritivo_AMUA.pdf",
    "Descritivo_APLJ.pdf",
    "Descritivo_APMA.pdf",
    "Descritivo_APS1.pdf",
    "Descritivo_APSA.pdf",
    "Descritivo_BABJ.pdf",
    "Descritivo_BABR.pdf",
    "Descritivo_BAIC.pdf",
    "Descritivo_BAIL.pdf",
    "Descritivo_BAIR.pdf",
    "Descritivo_BAIT.pdf",
    "Descritivo_BAPA.pdf",
    "Descritivo_BATF.pdf",
    "Descritivo_BAVC.pdf",
    "Descritivo_BELE.pdf",
    "Descritivo_BEPA.pdf",
    "Descritivo_BOAV.pdf",
    "Descritivo_BOMJ.pdf",
    "Descritivo_BRAZ.pdf",
    "Descritivo_BRFT.pdf",
    "Descritivo_CEEU.pdf",
    "Descritivo_CEFE.pdf",
    "Descritivo_CEFT.pdf",
    "Descritivo_CESB.pdf",
    "Descritivo_CHPI.pdf",
    "Descritivo_COAM.pdf",
    "Descritivo_CORU.pdf",
    "Descritivo_CRAT.pdf",
    "Descritivo_CRUZ.pdf",
    "Descritivo_CUIB.pdf",
    "Descritivo_EACH.pdf",
    "Descritivo_EESC.pdf",
    "Descritivo_ESNV.pdf",
    "Descritivo_FORT.pdf",
    "Descritivo_GOGY.pdf",
    "Descritivo_GOJA.pdf",
    "Descritivo_GOUR.pdf",
    "Descritivo_GUAI.pdf",
    "Descritivo_GVA1.pdf",
    "Descritivo_GVAL.pdf",
    "Descritivo_IFSC.pdf",
    "Descritivo_ILHA.pdf",
    "Descritivo_IMBT.pdf",
    "Descritivo_IMPZ.pdf",
    "Descritivo_ITAI.pdf",
    "Descritivo_ITAM.pdf",
    "Descritivo_JAMG.pdf",
    "Descritivo_MABA.pdf",
    "Descritivo_MABB.pdf",
    "Descritivo_MABS.pdf",
    "Descritivo_MANA.pdf",
    "Descritivo_MAPA.pdf",
    "Descritivo_MCL1.pdf",
    "Descritivo_MCLA.pdf",
    "Descritivo_MGBH.pdf",
    "Descritivo_MGGV.pdf",
    "Descritivo_MGIN.pdf",
    "Descritivo_MGJA.pdf",
    "Descritivo_MGJF.pdf",
    "Descritivo_MGJP.pdf",
    "Descritivo_MGLA.pdf",
    "Descritivo_MGMC.pdf",
    "Descritivo_MGMT.pdf",
    "Descritivo_MGRP.pdf",
    "Descritivo_MGTO.pdf",
    "Descritivo_MGUB.pdf",
    "Descritivo_MGV1.pdf",
    "Descritivo_MGVA.pdf",
    "Descritivo_MSAQ.pdf",
    "Descritivo_MSBL.pdf",
    "Descritivo_MSCB.pdf",
    "Descritivo_MSCG.pdf",
    "Descritivo_MSCO.pdf",
    "Descritivo_MSDO.pdf",
    "Descritivo_MSDR.pdf",
    "Descritivo_MSGR.pdf",
    "Descritivo_MSJR.pdf",
    "Descritivo_MSMJ.pdf",
    "Descritivo_MSMN.pdf",
    "Descritivo_MSNV.pdf",
    "Descritivo_MSPM.pdf",
    "Descritivo_MSPP.pdf",
    "Descritivo_MTBA.pdf",
    "Descritivo_MTCA.pdf",
    "Descritivo_MTCN.pdf",
    "Descritivo_MTCO.pdf",
    "Descritivo_MTGA.pdf",
    "Descritivo_MTIT.pdf",
    "Descritivo_MTJI.pdf",
    "Descritivo_MTJU.pdf",
    "Descritivo_MTLA.pdf",
    "Descritivo_MTLE.pdf",
    "Descritivo_MTNM.pdf",
    "Descritivo_MTNX.pdf",
    "Descritivo_MTSC.pdf",
    "Descritivo_MTSF.pdf",
    "Descritivo_MTSR.pdf",
    "Descritivo_MTVB.pdf",
    "Descritivo_NAUS.pdf",
    "Descritivo_NEIA.pdf",
    "Descritivo_OBRA.pdf",
    "Descritivo_ONRJ.pdf",
    "Descritivo_OURI.pdf",
    "Descritivo_PAAR.pdf",
    "Descritivo_PAAT.pdf",
    "Descritivo_PACO.pdf",
    "Descritivo_PAIT.pdf",
    "Descritivo_PARA.pdf",
    "Descritivo_PASM.pdf",
    "Descritivo_PAST.pdf",
    "Descritivo_PBCG.pdf",
    "Descritivo_PBJP.pdf",
    "Descritivo_PBPT.pdf",
    "Descritivo_PEAF.pdf",
    "Descritivo_PECA.pdf",
    "Descritivo_PEPE.pdf",
    "Descritivo_PERC.pdf",
    "Descritivo_PICR.pdf",
    "Descritivo_PIFL.pdf",
    "Descritivo_PISR.pdf",
    "Descritivo_PITN.pdf",
    "Descritivo_POAL.pdf",
    "Descritivo_POLI.pdf",
    "Descritivo_POVE.pdf",
    "Descritivo_PPTE.pdf",
    "Descritivo_PRCL.pdf",
    "Descritivo_PRCV.pdf",
    "Descritivo_PRGU.pdf",
    "Descritivo_PRMA.pdf",
    "Descritivo_PRPI.pdf",
    "Descritivo_PRUR.pdf",
    "Descritivo_RECF.pdf",
    "Descritivo_RIOB.pdf",
    "Descritivo_RIOD.pdf",
    "Descritivo_RJCG.pdf",
    "Descritivo_RJFB.pdf",
    "Descritivo_RJNI.pdf",
    "Descritivo_RJRE.pdf",
    "Descritivo_RJVA.pdf",
    "Descritivo_RNMO.pdf",
    "Descritivo_RNNA.pdf",
    "Descritivo_RNPF.pdf",
    "Descritivo_ROCD.pdf",
    "Descritivo_ROCO.pdf",
    "Descritivo_ROGM.pdf",
    "Descritivo_ROGU.pdf",
    "Descritivo_ROJI.pdf",
    "Descritivo_ROSA.pdf",
    "Descritivo_ROVI.pdf",
    "Descritivo_RSAL.pdf",
    "Descritivo_RSCL.pdf",
    "Descritivo_RSPE.pdf",
    "Descritivo_RSPF.pdf",
    "Descritivo_RSSL.pdf",
    "Descritivo_SAGA.pdf",
    "Descritivo_SALU.pdf",
    "Descritivo_SALV.pdf",
    "Descritivo_SAVO.pdf",
    "Descritivo_SCAQ.pdf",
    "Descritivo_SCBL.pdf",
    "Descritivo_SCCA.pdf",
    "Descritivo_SCCH.pdf",
    "Descritivo_SCCR.pdf",
    "Descritivo_SCFL.pdf",
    "Descritivo_SCIM.pdf",
    "Descritivo_SCLA.pdf",
    "Descritivo_SEAJ.pdf",
    "Descritivo_SJRP.pdf",
    "Descritivo_SJSP.pdf",
    "Descritivo_SMAR.pdf",
    "Descritivo_SPAR.pdf",
    "Descritivo_SPBO.pdf",
    "Descritivo_SPBP.pdf",
    "Descritivo_SPC1.pdf",
    "Descritivo_SPCA.pdf",
    "Descritivo_SPDR.pdf",
    "Descritivo_SPFE.pdf",
    "Descritivo_SPFR.pdf",
    "Descritivo_SPJA.pdf",
    "Descritivo_SPLI.pdf",
    "Descritivo_SPOR.pdf",
    "Descritivo_SPPI.pdf",
    "Descritivo_SPS1.pdf",
    "Descritivo_SPSO.pdf",
    "Descritivo_SPTU.pdf",
    "Descritivo_SSA1.pdf",
    "Descritivo_STHA.pdf",
    "Descritivo_SVIC.pdf",
    "Descritivo_TOGU.pdf",
    "Descritivo_TOPL.pdf",
    "Descritivo_UBA1.pdf",
    "Descritivo_UBAT.pdf",
    "Descritivo_UBE1.pdf",
    "Descritivo_UBER.pdf",
    "Descritivo_UEPP.pdf",
    "Descritivo_UFPR.pdf",
    "Descritivo_UYAR.pdf",
    "Descritivo_UYBU.pdf",
    "Descritivo_UYRB.pdf",
    "Descritivo_UYRI.pdf",
    "Descritivo_VARG.pdf",
    "Descritivo_VICO.pdf",
]


def _download_file(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    response = requests.get(url, headers=REQUEST_HEADERS, timeout=120)
    response.raise_for_status()

    destination.write_bytes(response.content)
    return destination


def _extract_pdf_text_pypdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError(
            "A biblioteca 'pypdf' não está instalada. Instale com: pip install pypdf"
        ) from exc

    reader = PdfReader(str(pdf_path))
    parts: list[str] = []

    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)

    return "\n".join(parts).strip()


def _extract_station_lists_from_filename_list() -> dict:
    return {
        "inactive_codes": set(),
        "warning_codes": set(),
        "rinex_1s_codes": set(),
        "rinex_15s_codes": set(),
        "rinex_2_codes": set(),
        "rinex_3_codes": set(),
    }


def _upsert_station(
    db: Session,
    station_data: dict,
    report_url: str,
    downloads_url: str,
    flags: dict,
) -> GnssStation:
    station_code = (station_data.get("station_code") or "").strip().upper()
    if not station_code:
        raise ValueError("Relatório sem station_code.")

    station = (
        db.query(GnssStation).filter(GnssStation.station_code == station_code).first()
    )

    is_new = station is None

    if station is None:
        station = GnssStation(
            station_code=station_code,
            created_at=datetime.utcnow(),
        )
        db.add(station)

    station.station_name = (
        station_data.get("station_name") or station.station_name or station_code
    )
    station.city = station_data.get("city")
    station.state = station_data.get("state")
    station.sat_code = station_data.get("sat_code")
    station.international_code = station_data.get("international_code")

    station.latitude = station_data.get("latitude")
    station.longitude = station_data.get("longitude")
    station.ellipsoidal_height = station_data.get("ellipsoidal_height")

    station.x = station_data.get("x")
    station.y = station_data.get("y")
    station.z = station_data.get("z")

    station.utm_n = station_data.get("utm_n")
    station.utm_e = station_data.get("utm_e")
    station.mc = station_data.get("mc")

    station.receiver_model = station_data.get("receiver_model")
    station.receiver_serial = station_data.get("receiver_serial")
    station.receiver_firmware = station_data.get("receiver_firmware")

    station.antenna_model = station_data.get("antenna_model")
    station.antenna_serial = station_data.get("antenna_serial")
    station.antenna_height = station_data.get("antenna_height")

    station.last_firmware_update = station_data.get("last_firmware_update")
    station.report_reference_date = station_data.get("report_reference_date")

    station.report_url = report_url
    station.downloads_page_url = downloads_url

    station.is_active = station_code not in flags["inactive_codes"]
    station.is_in_warning = station_code in flags["warning_codes"]
    station.is_operational = station_code not in flags["inactive_codes"]

    station.supports_rinex_1s = station_code in flags["rinex_1s_codes"]
    station.supports_rinex_15s = (
        station_code in flags["rinex_15s_codes"] if flags["rinex_15s_codes"] else True
    )
    station.supports_rinex_2 = station_code in flags["rinex_2_codes"]
    station.supports_rinex_3 = (
        station_code in flags["rinex_3_codes"] if flags["rinex_3_codes"] else True
    )

    station.sync_status = "synced"
    station.sync_message = None
    station.last_synced_at = datetime.utcnow()
    station.updated_at = datetime.utcnow()

    if is_new:
        db.flush()

    return station


def sync_rbmc_stations(db: Session, storage_root: str) -> dict:
    flags = _extract_station_lists_from_filename_list()

    reports_dir = Path(storage_root) / "rbmc_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    inserted = 0
    updated = 0
    failed = 0
    errors: list[dict] = []

    for file_name in RBMC_REPORT_FILES:
        report_url = urljoin(RBMC_FILES_BASE_URL, file_name)

        try:
            local_path = reports_dir / file_name
            _download_file(report_url, local_path)

            raw_text = _extract_pdf_text_pypdf(local_path)
            if not raw_text.strip():
                raise ValueError("Não foi possível extrair texto do PDF.")

            station_data = parse_rbmc_station_report_text(raw_text)

            station_code = (station_data.get("station_code") or "").strip().upper()
            if not station_code:
                raise ValueError("Relatório sem station_code.")

            # fallback obrigatório para não violar NOT NULL
            station_name = (station_data.get("station_name") or "").strip()
            if not station_name:
                station_data["station_name"] = station_code

            existing = (
                db.query(GnssStation)
                .filter(GnssStation.station_code == station_code)
                .first()
            )

            _upsert_station(
                db=db,
                station_data=station_data,
                report_url=report_url,
                downloads_url=RBMC_DOWNLOADS_URL,
                flags=flags,
            )

            db.flush()

            if existing:
                updated += 1
            else:
                inserted += 1

        except Exception as exc:
            db.rollback()
            failed += 1
            errors.append(
                {
                    "file_name": file_name,
                    "report_url": report_url,
                    "error": str(exc),
                }
            )

    db.commit()

    return {
        "downloads_url": RBMC_DOWNLOADS_URL,
        "reports_found": len(RBMC_REPORT_FILES),
        "inserted": inserted,
        "updated": updated,
        "failed": failed,
        "errors": errors[:50],
    }
