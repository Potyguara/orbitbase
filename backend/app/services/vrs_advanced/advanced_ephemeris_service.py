from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import gzip
import json
import shutil
import subprocess

import requests
from bs4 import BeautifulSoup

DEFAULT_TIMEOUT_SECONDS = 60

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)


@dataclass
class DownloadCandidate:
    url: str
    filename: str
    compressed: bool = False
    source: str = "unknown"


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_json(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _day_of_year(session_date: str) -> int:
    dt = datetime.strptime(session_date, "%Y-%m-%d")
    return int(dt.strftime("%j"))


def _year_4d(session_date: str) -> str:
    dt = datetime.strptime(session_date, "%Y-%m-%d")
    return dt.strftime("%Y")


def _gps_week_and_dow(session_date: str) -> tuple[int, int]:
    dt = datetime.strptime(session_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    delta_days = (dt - GPS_EPOCH).days
    gps_week = delta_days // 7
    gps_dow = delta_days % 7
    return gps_week, gps_dow


def _mask_username(username: str | None) -> str | None:
    if not username:
        return None

    value = str(username).strip()
    if not value:
        return None

    if "@" in value:
        local, domain = value.split("@", 1)
        if len(local) <= 3:
            masked_local = local[:1] + "***"
        else:
            masked_local = local[:3] + "*" * max(3, len(local) - 3)
        return f"{masked_local}@{domain}"

    if len(value) <= 6:
        return value[:1] + "***"

    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def _looks_like_html_response(path: Path) -> bool:
    try:
        if not path.exists() or not path.is_file():
            return False

        header = path.read_bytes()[:4096]
        text = header.decode("utf-8", errors="ignore").lstrip().lower()

        return (
            text.startswith("<!doctype")
            or text.startswith("<html")
            or text.startswith("<!")
            or "earthdata login" in text
            or "urs.earthdata.nasa.gov" in text
            or "<title>login" in text
            or ("<form" in text and "password" in text)
        )
    except Exception:
        return False


def _decompress_Z_file(src_Z: Path, dest_file: Path) -> None:
    commands = [
        ["gunzip", "-c", str(src_Z)],
        ["gzip", "-dc", str(src_Z)],
    ]

    last_error = None

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            dest_file.write_bytes(result.stdout)
            return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        f"Falha ao descomprimir .Z com utilitário do sistema: {last_error}"
    )


def _gunzip_file(src_gz: Path, dest_file: Path) -> None:
    with gzip.open(src_gz, "rb") as f_in, open(dest_file, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def _looks_like_valid_sp3(path: Path) -> bool:
    try:
        if not path.exists() or not path.is_file():
            return False

        if path.stat().st_size < 100:
            return False

        with open(path, "rb") as fh:
            header = fh.read(512)

        text = header.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        first_line = lines[0] if lines else ""

        return (
            first_line.startswith("#")
            or first_line.startswith("/*")
            or "SP3" in first_line.upper()
        )
    except Exception:
        return False


def _final_sp3_output_name(downloaded_name: str) -> str:
    upper = downloaded_name.upper()

    if upper.endswith(".SP3.GZ"):
        return downloaded_name[:-3]
    if downloaded_name.endswith(".GZ"):
        return downloaded_name[:-3]
    if upper.endswith(".SP3.Z") or downloaded_name.endswith(".Z"):
        return downloaded_name[:-2]
    return downloaded_name


def _build_requests_session(
    username: str | None = None,
    password: str | None = None,
) -> requests.Session:
    session = requests.Session()
    if username and password:
        session.auth = (username, password)

    session.headers.update(
        {
            "User-Agent": "OrbitBase/0.1 advanced-ephemeris",
            "Accept": "*/*",
        }
    )
    return session


def _download_file(
    url: str,
    dest: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    username: str | None = None,
    password: str | None = None,
) -> None:
    session = _build_requests_session(username=username, password=password)

    response = session.get(
        url,
        timeout=timeout_seconds,
        allow_redirects=True,
        stream=True,
    )
    response.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                fh.write(chunk)


def _discover_sp3_candidates_from_cddis_listing(
    session_date: str,
    gps_week: int,
    earthdata_username: str,
    earthdata_password: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[DownloadCandidate]:
    session = _build_requests_session(
        username=earthdata_username,
        password=earthdata_password,
    )

    url = f"https://cddis.nasa.gov/archive/gnss/products/{gps_week}/"

    response = session.get(
        url,
        headers={
            "User-Agent": "OrbitBase/0.1 advanced-ephemeris",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout_seconds,
        allow_redirects=True,
    )
    response.raise_for_status()

    html = response.text
    lower_html = html.lower()

    if "earthdata login" in lower_html or "urs.earthdata.nasa.gov" in lower_html:
        raise RuntimeError(
            "CDDIS retornou página de login em vez da listagem autenticada."
        )

    soup = BeautifulSoup(html, "html.parser")

    target_date_listing = session_date.replace("-", ":")
    candidates: list[DownloadCandidate] = []

    for link in soup.find_all("a"):
        href = (link.get("href") or "").strip()
        if not href:
            continue

        name = href.split("/")[-1].strip()
        if not name:
            continue

        lower_name = name.lower()
        if not (lower_name.endswith(".sp3.z") or lower_name.endswith(".sp3.gz")):
            continue

        row_text = ""
        parent = link.parent
        if parent is not None:
            row_text = parent.get_text(" ", strip=True)
        if not row_text:
            row_text = link.get_text(" ", strip=True)

        if target_date_listing not in row_text:
            continue

        candidates.append(
            DownloadCandidate(
                url=f"https://cddis.nasa.gov/archive/gnss/products/{gps_week}/{name}",
                filename=name,
                compressed=lower_name.endswith(".z") or lower_name.endswith(".gz"),
                source="cddis_listing",
            )
        )

    return candidates


def _sp3_candidate_priority(name: str) -> tuple[int, int, int, str]:
    lower = name.lower()

    if "igs" in lower:
        family_rank = 0
    elif "igr" in lower:
        family_rank = 1
    elif "igu" in lower:
        family_rank = 2
    else:
        family_rank = 9

    hourly_rank = 9
    for token_rank, token in enumerate(["_00", "_06", "_12", "_18"]):
        if token in lower:
            hourly_rank = token_rank
            break

    if lower.endswith(".sp3.z"):
        ext_rank = 0
    elif lower.endswith(".sp3.gz"):
        ext_rank = 1
    else:
        ext_rank = 9

    return (family_rank, hourly_rank, ext_rank, lower)


def _build_sp3_candidates(
    session_date: str,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[DownloadCandidate]:
    gps_week, gps_dow = _gps_week_and_dow(session_date)
    gps_week_str = str(gps_week)
    yyyy = _year_4d(session_date)
    doy3 = f"{_day_of_year(session_date):03d}"

    candidates: list[DownloadCandidate] = []
    seen_urls: set[str] = set()

    def add_candidate(candidate: DownloadCandidate) -> None:
        if candidate.url in seen_urls:
            return
        seen_urls.add(candidate.url)
        candidates.append(candidate)

    if earthdata_username and earthdata_password:
        try:
            discovered = _discover_sp3_candidates_from_cddis_listing(
                session_date=session_date,
                gps_week=gps_week,
                earthdata_username=earthdata_username,
                earthdata_password=earthdata_password,
                timeout_seconds=timeout_seconds,
            )
            discovered.sort(key=lambda c: _sp3_candidate_priority(c.filename))
            for item in discovered:
                add_candidate(item)
        except Exception as exc:
            print(f"[ADV-EPH] falha ao descobrir listagem CDDIS: {exc}", flush=True)

    short_names = [
        f"igs{gps_week}{gps_dow}.sp3.Z",
        f"igr{gps_week}{gps_dow}.sp3.Z",
        f"igu{gps_week}{gps_dow}.sp3.Z",
        f"igs{gps_week}{gps_dow}.sp3.gz",
        f"igr{gps_week}{gps_dow}.sp3.gz",
        f"igu{gps_week}{gps_dow}.sp3.gz",
    ]

    for name in short_names:
        add_candidate(
            DownloadCandidate(
                url=f"https://cddis.nasa.gov/archive/gnss/products/{gps_week_str}/{name}",
                filename=name,
                compressed=True,
                source="cddis_classic",
            )
        )
        add_candidate(
            DownloadCandidate(
                url=f"https://igs.ign.fr/pub/igs/products/{gps_week_str}/{name}",
                filename=name,
                compressed=True,
                source="ign_classic",
            )
        )
        add_candidate(
            DownloadCandidate(
                url=f"https://igs.bkg.bund.de/root_ftp/IGS/products/{gps_week_str}/{name}",
                filename=name,
                compressed=True,
                source="bkg_classic",
            )
        )

    long_names = [
        f"IGS0OPSFIN_{yyyy}{doy3}0000_01D_15M_ORB.SP3.gz",
        f"IGS0OPSRAP_{yyyy}{doy3}0000_01D_15M_ORB.SP3.gz",
        f"IGS0OPSULT_{yyyy}{doy3}0000_01D_15M_ORB.SP3.gz",
    ]

    for name in long_names:
        add_candidate(
            DownloadCandidate(
                url=f"https://cddis.nasa.gov/archive/gnss/products/{gps_week_str}/{name}",
                filename=name,
                compressed=True,
                source="cddis_long",
            )
        )
        add_candidate(
            DownloadCandidate(
                url=f"https://igs.ign.fr/pub/igs/products/{gps_week_str}/{name}",
                filename=name,
                compressed=True,
                source="ign_long",
            )
        )
        add_candidate(
            DownloadCandidate(
                url=f"https://igs.bkg.bund.de/root_ftp/IGS/products/{gps_week_str}/{name}",
                filename=name,
                compressed=True,
                source="bkg_long",
            )
        )

    return candidates


def _find_cached_sp3(day_root: Path) -> Path | None:
    candidates = sorted(day_root.glob("*.sp3")) + sorted(day_root.glob("*.SP3"))

    for path in candidates:
        if _looks_like_valid_sp3(path):
            return path

    return None


def _try_download_sp3(
    session_date: str,
    day_root: Path,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    downloads_dir = day_root / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    candidates = _build_sp3_candidates(
        session_date=session_date,
        earthdata_username=earthdata_username,
        earthdata_password=earthdata_password,
        timeout_seconds=timeout_seconds,
    )

    print(f"[ADV-EPH] total_candidates={len(candidates)}", flush=True)

    for candidate in candidates:
        tmp_path = downloads_dir / candidate.filename
        final_name = _final_sp3_output_name(candidate.filename)
        final_path = day_root / final_name

        _safe_unlink(tmp_path)
        _safe_unlink(final_path)

        try:
            print(
                f"[ADV-EPH] tentando source={candidate.source} url={candidate.url}",
                flush=True,
            )

            if "cddis.nasa.gov" in candidate.url:
                if not earthdata_username or not earthdata_password:
                    errors.append(
                        f"Credenciais Earthdata ausentes para acesso ao CDDIS: {candidate.url}"
                    )
                    continue

                _download_file(
                    candidate.url,
                    tmp_path,
                    username=earthdata_username,
                    password=earthdata_password,
                    timeout_seconds=timeout_seconds,
                )
            else:
                _download_file(
                    candidate.url,
                    tmp_path,
                    timeout_seconds=timeout_seconds,
                )

            print(f"[ADV-EPH] baixado {candidate.url} -> {tmp_path}", flush=True)
            print(
                f"[ADV-EPH] size={tmp_path.stat().st_size if tmp_path.exists() else 'missing'}",
                flush=True,
            )

            if _looks_like_html_response(tmp_path):
                print(f"[ADV-EPH] HTML detectado para {candidate.url}", flush=True)
                errors.append(f"Resposta HTML em vez de arquivo SP3: {candidate.url}")
                _safe_unlink(tmp_path)
                continue

            if candidate.filename.lower().endswith(".gz"):
                _gunzip_file(tmp_path, final_path)
            elif candidate.filename.lower().endswith(".z"):
                _decompress_Z_file(tmp_path, final_path)
            else:
                shutil.move(str(tmp_path), str(final_path))

            if _looks_like_html_response(final_path):
                errors.append(
                    f"Arquivo final após descompressão parece HTML/login: {candidate.url}"
                )
                _safe_unlink(final_path)
                continue

            if _looks_like_valid_sp3(final_path):
                _safe_unlink(tmp_path)
                print(f"[ADV-EPH] SP3 válido encontrado: {final_path}", flush=True)
                return {
                    "sp3_available": True,
                    "sp3_path": str(final_path),
                    "sp3_source_url": candidate.url,
                    "sp3_source_filename": candidate.filename,
                    "sp3_cached": False,
                    "download_errors": errors,
                }

            errors.append(f"Arquivo baixado mas inválido como SP3: {candidate.url}")
            _safe_unlink(final_path)

        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "?"
            errors.append(f"HTTP {status_code} :: {candidate.url}")
        except requests.RequestException as exc:
            errors.append(f"Request error :: {candidate.url} :: {exc}")
        except Exception as exc:
            errors.append(f"Erro :: {candidate.url} :: {type(exc).__name__}: {exc}")
        finally:
            _safe_unlink(tmp_path)

    return {
        "sp3_available": False,
        "sp3_path": None,
        "sp3_source_url": None,
        "sp3_source_filename": None,
        "sp3_cached": False,
        "download_errors": errors,
    }


def ensure_precise_ephemeris_products(
    session_date: str,
    storage_root: Path,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    force_refresh: bool = False,
) -> dict:
    day_root = storage_root / "precise_products" / session_date
    day_root.mkdir(parents=True, exist_ok=True)

    manifest_path = day_root / "manifest.json"
    previous_manifest = _load_json(manifest_path) or {}

    auth_mode = "earthdata" if earthdata_username else "anonymous"
    masked_username = _mask_username(earthdata_username)

    if not force_refresh:
        cached_sp3 = _find_cached_sp3(day_root)
        if cached_sp3 is not None:
            payload = {
                "session_date": session_date,
                "sp3_available": True,
                "clk_available": False,
                "sp3_path": str(cached_sp3),
                "clk_path": None,
                "sp3_source_url": previous_manifest.get("sp3_source_url"),
                "sp3_source_filename": cached_sp3.name,
                "status": "cached",
                "sp3_cached": True,
                "checked_at": datetime.utcnow().isoformat() + "Z",
                "download_errors": previous_manifest.get("download_errors", []),
                "auth_mode": auth_mode,
                "earthdata_username_masked": masked_username,
            }
            _write_json(manifest_path, payload)
            return payload

    download_result = _try_download_sp3(
        session_date=session_date,
        day_root=day_root,
        earthdata_username=earthdata_username,
        earthdata_password=earthdata_password,
        timeout_seconds=timeout_seconds,
    )

    payload = {
        "session_date": session_date,
        "sp3_available": bool(download_result["sp3_available"]),
        "clk_available": False,
        "sp3_path": download_result["sp3_path"],
        "clk_path": None,
        "sp3_source_url": download_result["sp3_source_url"],
        "sp3_source_filename": download_result["sp3_source_filename"],
        "status": "downloaded" if download_result["sp3_available"] else "not_available",
        "sp3_cached": bool(download_result["sp3_cached"]),
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "download_errors": download_result["download_errors"],
        "auth_mode": auth_mode,
        "earthdata_username_masked": masked_username,
    }

    _write_json(manifest_path, payload)
    return payload
