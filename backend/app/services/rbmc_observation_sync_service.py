from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
import shutil
import zipfile
import re

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.models import GnssStation
from app.services.hatanaka_service import convert_hatanaka_to_rinex
from app.services.rinex_nav_satpos_service import (
    is_gps_navigation_filename,
    navigation_file_priority,
)
import os
from urllib.request import urlretrieve

REQUEST_HEADERS = {"User-Agent": "OrbitBase-RBMC-Observation-Sync/1.0"}

RBMC_DOWNLOADS_BASE_URL = (
    "https://www.ibge.gov.br/geociencias/informacoes-sobre-posicionamento-geodesico/"
    "rede-geodesica/16258-rede-brasileira-de-monitoramento-continuo-dos-sistemas-gnss-rbmc.html"
)

RBMC_DAILY_PAGE_URL = f"{RBMC_DOWNLOADS_BASE_URL}?=&t=downloads"


def _rbmc_storage_root() -> Path:
    return Path(__file__).resolve().parents[2] / "storage" / "rbmc"


def _station_day_root(station_code: str, session_date: str) -> Path:
    return _rbmc_storage_root() / station_code.lower() / session_date


def _build_ibge_zip_candidates(station_code: str, session_date: str) -> list[str]:
    """
    Gera candidatos de URL do ZIP da RBMC para a estação/data.
    Ajuste fino pode ser necessário conforme o padrão exato do IBGE
    que você já utiliza no projeto.
    """
    dt = datetime.fromisoformat(session_date)
    year_2 = dt.strftime("%y")
    doy = dt.strftime("%j")
    station = station_code.lower()

    # nomes internos mais comuns no padrão RINEX diário
    base_name = f"{station}{doy}1.{year_2}"

    # candidatos ZIP
    return [
        # zip do conjunto da estação
        f"https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/rbmc/dados/{dt.year}/{doy}/{station}/{base_name}.zip",
        f"https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/rbmc/dados/{dt.year}/{doy}/{station}/{station}{doy}1.zip",
        f"https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/rbmc/dados/{dt.year}/{doy}/{station}.zip",
    ]


def _download_rbmc_zip(station_code: str, session_date: str) -> Path:
    day_root = _station_day_root(station_code, session_date)
    zip_root = day_root / "zip"
    zip_root.mkdir(parents=True, exist_ok=True)

    zip_path = zip_root / f"{station_code.lower()}_{session_date}.zip"
    if zip_path.exists() and zip_path.stat().st_size > 0:
        return zip_path

    last_error = None
    for url in _build_ibge_zip_candidates(station_code, session_date):
        try:
            print(f"[RBMC-DOWNLOAD] tentando {url}", flush=True)
            urlretrieve(url, zip_path)
            if zip_path.exists() and zip_path.stat().st_size > 0:
                print(f"[RBMC-DOWNLOAD] ok => {zip_path}", flush=True)
                return zip_path
        except Exception as exc:
            last_error = exc
            print(f"[RBMC-DOWNLOAD] falhou {url} => {exc}", flush=True)

    raise FileNotFoundError(
        f"Não foi possível baixar o ZIP RBMC para {station_code} em {session_date}. "
        f"Último erro: {last_error}"
    )


def _extract_station_zip(zip_path: Path, extracted_root: Path) -> list[Path]:
    extracted_root.mkdir(parents=True, exist_ok=True)

    extracted_files = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue

            filename = Path(member).name
            if not filename:
                continue

            target_path = extracted_root / filename
            if not target_path.exists():
                with zf.open(member) as src, open(target_path, "wb") as dst:
                    dst.write(src.read())

            extracted_files.append(target_path)

    print(
        f"[RBMC-EXTRACT] {zip_path.name} => {len(extracted_files)} arquivos extraídos em {extracted_root}",
        flush=True,
    )
    return extracted_files


def _find_station_files(station_code: str, session_date: str) -> dict:
    day_root = _station_day_root(station_code, session_date)
    extracted_root = day_root / "extracted"
    converted_root = day_root / "converted"

    obs_candidates = []
    nav_candidates = []

    search_roots = [converted_root, extracted_root, day_root]
    for root in search_roots:
        if not root.exists():
            continue

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            lower = path.name.lower()

            # navegação GPS (.yyn, .yyp, _GN.rnx, _MN.rnx, brdc*, .nav)
            if is_gps_navigation_filename(path.name):
                nav_candidates.append(path)
                continue

            # observação (inclui _MO.rnx; exclui NAV .rnx de outros sistemas)
            if lower.endswith((".obs", ".o", ".d")) or re.search(
                r"\.\d{2}[od]$", lower
            ):
                obs_candidates.append(path)
            elif lower.endswith(".rnx") and re.search(r"_[a-z]o\.rnx$", lower):
                obs_candidates.append(path)

    def _dedupe(paths: list[Path]) -> list[Path]:
        seen = set()
        out = []
        for p in paths:
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    obs_candidates = _dedupe(obs_candidates)
    nav_candidates = _dedupe(nav_candidates)

    # prioriza observação convertida (.o) antes da compactada (.d)
    obs_candidates.sort(
        key=lambda p: (
            0 if re.search(r"\.\d{2}o$", p.name.lower()) else 1,
            0 if "converted" in str(p).lower() else 1,
            len(p.name),
        )
    )

    nav_candidates.sort(key=navigation_file_priority)

    return {
        "day_root": day_root,
        "extracted_root": extracted_root,
        "converted_root": converted_root,
        "observation_files": [str(p) for p in obs_candidates],
        "navigation_files": [str(p) for p in nav_candidates],
        "best_observation_file": str(obs_candidates[0]) if obs_candidates else None,
        "best_navigation_file": str(nav_candidates[0]) if nav_candidates else None,
    }


def ensure_rbmc_station_files(station_code: str, session_date: str) -> dict:
    """
    Garante que a estação/data tenha:
    - ZIP baixado
    - extração concluída
    - observação convertida, se houver .d
    - NAV detectado
    """
    day_root = _station_day_root(station_code, session_date)
    extracted_root = day_root / "extracted"
    converted_root = day_root / "converted"
    converted_root.mkdir(parents=True, exist_ok=True)

    file_state = _find_station_files(station_code, session_date)
    if file_state["best_observation_file"] and file_state["best_navigation_file"]:
        return file_state

    zip_path = _download_rbmc_zip(station_code, session_date)
    _extract_station_zip(zip_path, extracted_root)

    # converte Hatanaka se existir
    for path in extracted_root.rglob("*"):
        if not path.is_file():
            continue

        lower = path.name.lower()
        if lower.endswith(".d") or re.search(r"\.\d{2}d$", lower):
            try:
                converted = Path(convert_hatanaka_to_rinex(str(path)))
                target = converted_root / converted.name
                if converted.exists() and not target.exists():
                    target.write_bytes(converted.read_bytes())
            except Exception as exc:
                print(f"[RBMC-CONVERT] falhou {path.name} => {exc}", flush=True)

        elif lower.endswith(".o") or re.search(r"\.\d{2}o$", lower):
            target = converted_root / path.name
            if not target.exists():
                target.write_bytes(path.read_bytes())

    file_state = _find_station_files(station_code, session_date)

    print(
        f"[RBMC-ENSURE] {station_code}/{session_date} => "
        f"obs={file_state['best_observation_file']} | nav={file_state['best_navigation_file']}",
        flush=True,
    )

    return file_state


def _fetch_html(url: str) -> str:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=60)
    response.raise_for_status()
    return response.text


def _safe_station_code(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip().upper()


def _year_2_digits(year: int) -> str:
    return str(year)[-2:]


def _day_of_year(target_date: date) -> str:
    return f"{target_date.timetuple().tm_yday:03d}"


def _build_day_folder_url(target_date: date) -> str:
    year = target_date.year
    doy = _day_of_year(target_date)
    return (
        "https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/"
        f"rbmc/dados/{year}/{doy}/"
    )


def _extract_download_links_from_page(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        links.append(full_url)

    unique = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)

    return unique


def _download_file(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    with requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=180,
        stream=True,
    ) as response:
        response.raise_for_status()
        with destination.open("wb") as file_obj:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    file_obj.write(chunk)

    return destination


def _extract_zip(zip_path: Path, destination_dir: Path) -> list[Path]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    extracted_paths: list[Path] = []

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(destination_dir)
        for member in zip_ref.namelist():
            member_path = destination_dir / member
            if (
                member.endswith("/")
                or not member_path.exists()
                or not member_path.is_file()
            ):
                continue
            extracted_paths.append(member_path)

    return extracted_paths


def _is_observation_candidate(path: Path, station_code: str, target_date: date) -> bool:
    name = path.name.lower()
    code = station_code.lower()
    yy = _year_2_digits(target_date.year)

    if name.endswith(".obs") or name.endswith(".rnx"):
        return True

    if name.endswith(".o") or name.endswith(".d"):
        return True

    if re.search(rf"\.{yy}o$", name):
        return True

    if re.search(rf"\.{yy}d$", name):
        return True

    if code in name and (
        ".o" in name or ".d" in name or ".obs" in name or ".rnx" in name
    ):
        return True

    return False


def _is_navigation_candidate(path: Path, target_date: date) -> bool:
    name = path.name.lower()
    yy = _year_2_digits(target_date.year)

    if name.endswith(".nav"):
        return True

    if name.endswith(".n") or name.endswith(".g") or name.endswith(".l"):
        return True

    if re.search(rf"\.{yy}n$", name):
        return True

    if re.search(rf"\.{yy}g$", name):
        return True

    if re.search(rf"\.{yy}l$", name):
        return True

    return False


def _list_existing_station_files(
    storage_root: str,
    station_code: str,
    target_date: date,
) -> dict:
    root = Path(storage_root) / "rbmc" / station_code.lower() / target_date.isoformat()
    extracted_dir = root / "extracted"
    converted_dir = root / "converted"

    observation_files = []
    navigation_files = []

    if converted_dir.exists():
        for path in converted_dir.rglob("*"):
            if path.is_file() and _is_observation_candidate(
                path, station_code, target_date
            ):
                observation_files.append(
                    {
                        "file_name": path.name,
                        "final_path": str(path),
                    }
                )

    if extracted_dir.exists():
        for path in extracted_dir.rglob("*"):
            if not path.is_file():
                continue

            if _is_navigation_candidate(path, target_date):
                navigation_files.append(
                    {
                        "file_name": path.name,
                        "final_path": str(path),
                    }
                )

    return {
        "observation_files": observation_files,
        "navigation_files": navigation_files,
    }


def _match_station_zip_links(
    available_links: list[str],
    station_code: str,
    target_date: date,
) -> list[str]:
    code = station_code.lower()
    doy = _day_of_year(target_date)
    yy = _year_2_digits(target_date.year)

    patterns = [
        f"{code}{doy}{yy}.zip",
        f"{code}{doy}1.zip",
        f"{code}{doy}0.zip",
        f"{code}{doy}.zip",
    ]

    matched = []
    for link in available_links:
        lower_link = link.lower()
        if any(pattern in lower_link for pattern in patterns):
            matched.append(link)

    unique = []
    seen = set()
    for link in matched:
        if link not in seen:
            seen.add(link)
            unique.append(link)

    return unique


def _convert_extracted_observations(
    extracted_paths: list[Path],
    station_code: str,
    target_date: date,
    converted_dir: Path,
) -> list[dict]:
    converted_dir.mkdir(parents=True, exist_ok=True)
    converted_outputs: list[dict] = []

    for path in extracted_paths:
        if not path.is_file():
            continue

        if not _is_observation_candidate(path, station_code, target_date):
            continue

        try:
            converted_source = Path(convert_hatanaka_to_rinex(str(path)))
            target_path = converted_dir / converted_source.name

            if converted_source.resolve() != target_path.resolve():
                shutil.copy2(converted_source, target_path)

            converted_outputs.append(
                {
                    "file_name": target_path.name,
                    "final_path": str(target_path),
                }
            )
        except Exception:
            continue

    unique = []
    seen = set()
    for item in converted_outputs:
        key = item["final_path"]
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


def _update_station_sync_status(
    db: Session,
    station: GnssStation,
    status_value: str,
    message: str | None,
) -> None:
    station.sync_status = status_value
    station.sync_message = message
    station.last_synced_at = datetime.utcnow()
    station.updated_at = datetime.utcnow()
    db.add(station)


def _download_and_prepare_station_day(
    storage_root: str,
    station_code: str,
    target_date: date,
    zip_url: str,
) -> dict:
    root = Path(storage_root) / "rbmc" / station_code.lower() / target_date.isoformat()
    raw_dir = root / "raw"
    extracted_dir = root / "extracted"
    converted_dir = root / "converted"

    raw_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    converted_dir.mkdir(parents=True, exist_ok=True)

    zip_name = zip_url.split("/")[-1]
    zip_path = raw_dir / zip_name

    if not zip_path.exists():
        _download_file(zip_url, zip_path)

    extracted_paths = _extract_zip(zip_path, extracted_dir)
    converted_observations = _convert_extracted_observations(
        extracted_paths=extracted_paths,
        station_code=station_code,
        target_date=target_date,
        converted_dir=converted_dir,
    )

    navigation_files = []
    for path in extracted_dir.rglob("*"):
        if path.is_file() and _is_navigation_candidate(path, target_date):
            navigation_files.append(
                {
                    "file_name": path.name,
                    "final_path": str(path),
                }
            )

    return {
        "zip_url": zip_url,
        "zip_path": str(zip_path),
        "observation_files": converted_observations,
        "navigation_files": navigation_files,
    }


# ORBITBASE_RBMC_AUTOFIX_V1
# Prioriza o nome real publicado no indice diario do IBGE; sem inferir subpastas.
def _orbitbase_match_daily_zip_links(available_links, station_code, target_date):
    from urllib.parse import urlparse, unquote
    import re

    station = str(station_code).strip().lower()
    doy = f"{target_date.timetuple().tm_yday:03d}"
    exact_name = f"{station}{doy}1.zip"
    matched = []
    for link in available_links or []:
        filename = unquote(urlparse(str(link)).path.rsplit('/', 1)[-1]).lower()
        # Ex.: aplj1571.zip, apma1571.zip, aps11571.zip
        if filename == exact_name or re.fullmatch(
            re.escape(station + doy) + r"[0-9]\.zip", filename
        ):
            matched.append(str(link))
    # Preferir a sessao diaria 1, preservando demais sessoes como alternativas.
    return sorted(dict.fromkeys(matched), key=lambda x: (
        0 if x.lower().endswith('/' + exact_name) else 1, x
    ))


def sync_rbmc_observations_for_date(
    db: Session,
    storage_root: str,
    target_date: date,
    station_codes: list[str] | None = None,
    only_active: bool = True,
) -> dict:
    if station_codes:
        normalized_codes = []
        seen = set()

        for code in station_codes:
            safe_code = _safe_station_code(code)
            if not safe_code or safe_code in seen:
                continue
            seen.add(safe_code)
            normalized_codes.append(safe_code)

        stations = (
            db.query(GnssStation)
            .filter(GnssStation.station_code.in_(normalized_codes))
            .all()
        )
    else:
        query = db.query(GnssStation)
        if only_active:
            query = query.filter(GnssStation.is_active.is_(True))
        stations = query.all()

    if not stations:
        return {
            "target_date": target_date.isoformat(),
            "stations_requested": 0,
            "stations_processed": 0,
            "stations_with_observation": 0,
            "stations_with_navigation": 0,
            "failed": 0,
            "results": [],
        }

    day_folder_url = _build_day_folder_url(target_date)
    try:
        html = _fetch_html(day_folder_url)
        available_links = _extract_download_links_from_page(html, day_folder_url)
    except requests.RequestException as exc:
        # Não impedir a busca em cache de outras estações por falha do índice.
        print(f"[RBMC-SYNC] Índice diário inacessível: {exc}", flush=True)
        available_links = []

    results = []
    failed = 0
    stations_with_observation = 0
    stations_with_navigation = 0

    for station in stations:
        code = _safe_station_code(station.station_code)

        try:
            existing = _list_existing_station_files(
                storage_root=storage_root,
                station_code=code,
                target_date=target_date,
            )

            observation_files = existing["observation_files"]
            navigation_files = existing["navigation_files"]
            matched_zip_links = _orbitbase_match_daily_zip_links(
                available_links=available_links,
                station_code=code,
                target_date=target_date,
            )

            if not observation_files:
                if not matched_zip_links:
                    _update_station_sync_status(
                        db=db,
                        station=station,
                        status_value="warning",
                        message=f"Nenhum ZIP diário encontrado para {code} em {target_date.isoformat()}.",
                    )

                    results.append(
                        {
                            "station_code": code,
                            "target_date": target_date.isoformat(),
                            "observation_found": False,
                            "navigation_found": False,
                            "matched_zip_links": [],
                            "error": "ZIP diário não encontrado.",
                        }
                    )
                    continue

                prepared = _download_and_prepare_station_day(
                    storage_root=storage_root,
                    station_code=code,
                    target_date=target_date,
                    zip_url=matched_zip_links[0],
                )

                observation_files = prepared["observation_files"]
                navigation_files = prepared["navigation_files"]

            if observation_files:
                stations_with_observation += 1

            if navigation_files:
                stations_with_navigation += 1

            if observation_files:
                _update_station_sync_status(
                    db=db,
                    station=station,
                    status_value="synced",
                    message=f"Observação sincronizada para {target_date.isoformat()}.",
                )
            else:
                _update_station_sync_status(
                    db=db,
                    station=station,
                    status_value="warning",
                    message=f"Nenhum arquivo de observação válido foi preparado para {target_date.isoformat()}.",
                )

            results.append(
                {
                    "station_code": code,
                    "target_date": target_date.isoformat(),
                    "observation_found": bool(observation_files),
                    "navigation_found": bool(navigation_files),
                    "observation_files": observation_files,
                    "navigation_files": navigation_files,
                    "matched_zip_links": matched_zip_links,
                }
            )

        except Exception as exc:
            failed += 1

            _update_station_sync_status(
                db=db,
                station=station,
                status_value="failed",
                message=str(exc)[:1000],
            )

            results.append(
                {
                    "station_code": code,
                    "target_date": target_date.isoformat(),
                    "observation_found": False,
                    "navigation_found": False,
                    "error": str(exc),
                }
            )

    db.commit()

    return {
        "target_date": target_date.isoformat(),
        "day_folder_url": day_folder_url,
        "stations_requested": len(stations),
        "stations_processed": len(results),
        "stations_with_observation": stations_with_observation,
        "stations_with_navigation": stations_with_navigation,
        "failed": failed,
        "results": results[:200],
    }


def sync_rbmc_observations_for_job_date(
    db: Session,
    storage_root: str,
    session_date: str,
    selected_station_codes: list[str],
) -> dict:
    target_date = date.fromisoformat(session_date)

    return sync_rbmc_observations_for_date(
        db=db,
        storage_root=storage_root,
        target_date=target_date,
        station_codes=selected_station_codes,
        only_active=False,
    )
