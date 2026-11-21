from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from datetime import timedelta
from urllib.parse import urlparse
import gzip
import json
import os
import shutil
import subprocess

import requests
from bs4 import BeautifulSoup

DEFAULT_TIMEOUT_SECONDS = 20
CONNECT_TIMEOUT_SECONDS = 10
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)

# Semana GPS a partir da qual o IGS passou a publicar apenas nomes longos
# (IGS0OPSFIN_YYYYDDD0000_01D_15M_ORB.SP3.gz).
IGS_LONG_NAME_FIRST_WEEK = 2238

EARTHDATA_AUTH_HOST = "urs.earthdata.nasa.gov"

# Espelhos dos produtos IGS. {week} é a semana GPS.
# CDDIS exige Earthdata; BKG e IGN são anônimos.
# Pode ser sobrescrito por ORBITBASE_SP3_MIRRORS="nome=url_base,nome2=url_base2".
DEFAULT_SP3_MIRRORS: list[tuple[str, str, bool]] = [
    ("cddis", "https://cddis.nasa.gov/archive/gnss/products/{week}/", True),
    ("bkg", "https://igs.bkg.bund.de/root_ftp/IGS/products/{week}/", False),
    ("ign", "https://igs.ign.fr/pub/igs/products/{week}/", False),
]

# Navegação broadcast diária combinada (RINEX 3, mista) — último recurso
# quando não há SP3 nem NAV dentro do ZIP RBMC.
DEFAULT_BRDC_MIRRORS: list[tuple[str, str, bool]] = [
    ("bkg", "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{yyyy}/{doy}/", False),
    ("cddis", "https://cddis.nasa.gov/archive/gnss/data/daily/{yyyy}/brdc/", True),
]


def _mirrors_from_env(env_name: str, default: list[tuple[str, str, bool]]):
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return list(default)
    mirrors = []
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        name, url = chunk.split("=", 1)
        name = name.strip()
        url = url.strip()
        if not name or not url:
            continue
        mirrors.append((name, url, "cddis.nasa.gov" in url))
    return mirrors or list(default)


class EarthdataSession(requests.Session):
    """
    O CDDIS redireciona para urs.earthdata.nasa.gov para autenticar.
    O ``requests`` padrão REMOVE o cabeçalho Authorization ao trocar de host,
    então o login nunca chegava ao Earthdata e o servidor devolvia a página
    HTML de login (causa do erro do job 141). Esta sessão mantém a
    autenticação apenas entre CDDIS e Earthdata (receita oficial da NASA).
    """

    _TRUSTED_HOSTS = {EARTHDATA_AUTH_HOST, "cddis.nasa.gov"}

    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        url = prepared_request.url
        if "Authorization" in headers:
            original = urlparse(response.request.url).hostname
            redirect = urlparse(url).hostname
            if (
                original != redirect
                and not (
                    original in self._TRUSTED_HOSTS and redirect in self._TRUSTED_HOSTS
                )
            ):
                del headers["Authorization"]
        return


class HtmlInsteadOfFileError(RuntimeError):
    pass


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
        if header[:2] in (b"\x1f\x8b", b"\x1f\x9d"):
            return False
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

    if upper.endswith(".GZ"):
        return downloaded_name[:-3]
    if upper.endswith(".Z"):
        return downloaded_name[:-2]
    return downloaded_name


def _sanitize_filename(name: str) -> str:
    return Path(name).name.replace("/", "_").replace("\\", "_").strip()


def _manual_upload_root(day_root: Path) -> Path:
    path = day_root / "manual_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manual_valid_sp3_dir(day_root: Path) -> Path:
    path = day_root / "manual_sp3"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sp3_candidate_priority(name: str) -> tuple[int, int, int, str]:
    lower = name.lower()

    if "igs" in lower:
        family_rank = 0
    elif "igr" in lower:
        family_rank = 1
    elif "igu" in lower:
        family_rank = 2
    elif "igv" in lower:
        family_rank = 3
    else:
        family_rank = 9

    hourly_rank = 9
    for token_rank, token in enumerate(["_00", "_06", "_12", "_18"]):
        if token in lower:
            hourly_rank = token_rank
            break

    if lower.endswith(".sp3"):
        ext_rank = 0
    elif lower.endswith(".sp3.z"):
        ext_rank = 1
    elif lower.endswith(".sp3.gz"):
        ext_rank = 2
    else:
        ext_rank = 9

    return (family_rank, hourly_rank, ext_rank, lower)


def _pick_best_sp3(paths: list[Path]) -> Path | None:
    valid = [
        p for p in paths if p.exists() and p.is_file() and _looks_like_valid_sp3(p)
    ]
    if not valid:
        return None
    valid.sort(key=lambda p: _sp3_candidate_priority(p.name))
    return valid[0]


def save_manual_sp3_uploads(
    session_date: str,
    storage_root: Path,
    uploads: list[tuple[str, bytes]],
) -> dict:
    day_root = storage_root / "precise_products" / session_date
    day_root.mkdir(parents=True, exist_ok=True)

    raw_dir = _manual_upload_root(day_root)
    valid_dir = _manual_valid_sp3_dir(day_root)
    manifest_path = day_root / "manifest.json"
    previous_manifest = _load_json(manifest_path) or {}

    stored_raw: list[str] = []
    valid_sp3: list[str] = []
    rejected: list[str] = []
    errors: list[str] = []

    for original_name, content in uploads or []:
        safe_name = _sanitize_filename(original_name)
        if not safe_name:
            rejected.append("arquivo_sem_nome")
            continue

        raw_path = raw_dir / safe_name
        final_name = _final_sp3_output_name(safe_name)
        final_path = valid_dir / final_name

        _safe_unlink(raw_path)
        _safe_unlink(final_path)

        try:
            raw_path.write_bytes(content)
            stored_raw.append(str(raw_path))

            if _looks_like_html_response(raw_path):
                rejected.append(f"{safe_name} :: conteúdo HTML/login inválido")
                _safe_unlink(raw_path)
                continue

            lower = safe_name.lower()

            if lower.endswith(".gz"):
                _gunzip_file(raw_path, final_path)
            elif lower.endswith(".z"):
                _decompress_Z_file(raw_path, final_path)
            elif lower.endswith(".sp3"):
                shutil.copyfile(raw_path, final_path)
            else:
                rejected.append(f"{safe_name} :: extensão não suportada")
                _safe_unlink(raw_path)
                continue

            if _looks_like_html_response(final_path):
                rejected.append(f"{safe_name} :: descompressão resultou em HTML/login")
                _safe_unlink(final_path)
                continue

            if not _looks_like_valid_sp3(final_path):
                rejected.append(f"{safe_name} :: arquivo não validado como SP3")
                _safe_unlink(final_path)
                continue

            valid_sp3.append(str(final_path))

        except Exception as exc:
            errors.append(f"{safe_name} :: {type(exc).__name__}: {exc}")
            _safe_unlink(final_path)

    best_sp3 = _pick_best_sp3([Path(p) for p in valid_sp3])

    payload = {
        "session_date": session_date,
        "sp3_available": best_sp3 is not None,
        "clk_available": False,
        "sp3_path": str(best_sp3) if best_sp3 else None,
        "clk_path": None,
        "sp3_source_url": None,
        "sp3_source_filename": best_sp3.name if best_sp3 else None,
        "status": "manual_upload" if best_sp3 else "manual_upload_invalid",
        "sp3_cached": False,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "download_errors": previous_manifest.get("download_errors", []),
        "manual_upload_used": best_sp3 is not None,
        "manual_upload_count": len(uploads or []),
        "valid_sp3_count": len(valid_sp3),
        "manual_sp3_files": valid_sp3,
        "manual_raw_files": stored_raw,
        "rejected_files": rejected,
        "manual_upload_errors": errors,
        "auth_mode": previous_manifest.get("auth_mode", "manual"),
        "earthdata_username_masked": previous_manifest.get("earthdata_username_masked"),
    }

    _write_json(manifest_path, payload)
    return payload


def _build_requests_session(
    username: str | None = None,
    password: str | None = None,
) -> requests.Session:
    session = EarthdataSession()

    if username and password:
        session.auth = (username, password)

    session.headers.update(
        {
            "User-Agent": "OrbitBase/0.2 advanced-ephemeris",
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
        timeout=(min(CONNECT_TIMEOUT_SECONDS, timeout_seconds), timeout_seconds),
        allow_redirects=True,
        stream=True,
    )
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    final_host = urlparse(response.url).hostname or ""
    if "text/html" in content_type or final_host == EARTHDATA_AUTH_HOST:
        response.close()
        raise HtmlInsteadOfFileError(
            f"Servidor devolveu HTML (provável página de login Earthdata) em vez do arquivo: {url}"
        )

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


def sp3_product_filenames(session_date: str) -> list[str]:
    """
    Nomes de SP3 IGS, do mais preciso ao menos preciso:
    final (latência ~2 semanas), rápido (~1 dia) e ultra-rápido (horas).
    """
    gps_week, gps_dow = _gps_week_and_dow(session_date)
    yyyy = _year_4d(session_date)
    doy3 = f"{_day_of_year(session_date):03d}"

    names = [
        f"IGS0OPSFIN_{yyyy}{doy3}0000_01D_15M_ORB.SP3.gz",
        f"IGS0OPSRAP_{yyyy}{doy3}0000_01D_15M_ORB.SP3.gz",
    ]
    for hh in ("18", "12", "06", "00"):
        names.append(f"IGS0OPSULT_{yyyy}{doy3}{hh}00_02D_15M_ORB.SP3.gz")

    if gps_week < IGS_LONG_NAME_FIRST_WEEK:
        names.extend(
            [
                f"igs{gps_week}{gps_dow}.sp3.Z",
                f"igr{gps_week}{gps_dow}.sp3.Z",
                f"igu{gps_week}{gps_dow}_18.sp3.Z",
                f"igu{gps_week}{gps_dow}_12.sp3.Z",
            ]
        )

    return names


def _build_sp3_candidates(
    session_date: str,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[DownloadCandidate]:
    gps_week, _ = _gps_week_and_dow(session_date)
    has_credentials = bool(earthdata_username and earthdata_password)

    candidates: list[DownloadCandidate] = []
    seen_urls: set[str] = set()

    def add_candidate(candidate: DownloadCandidate) -> None:
        if candidate.url in seen_urls:
            return
        seen_urls.add(candidate.url)
        candidates.append(candidate)

    mirrors = _mirrors_from_env("ORBITBASE_SP3_MIRRORS", DEFAULT_SP3_MIRRORS)

    # Para cada nível de qualidade, tenta todos os espelhos antes de cair
    # para um produto menos preciso.
    for name in sp3_product_filenames(session_date):
        for mirror_name, base_url, needs_auth in mirrors:
            if needs_auth and not has_credentials:
                continue
            add_candidate(
                DownloadCandidate(
                    url=base_url.format(week=gps_week) + name,
                    filename=name,
                    compressed=name.lower().endswith((".gz", ".z")),
                    source=mirror_name,
                )
            )

    return candidates


def brdc_product_candidates(
    session_date: str, has_credentials: bool
) -> list[DownloadCandidate]:
    dt = datetime.strptime(session_date, "%Y-%m-%d")
    yyyy = dt.strftime("%Y")
    yy = dt.strftime("%y")
    doy = dt.strftime("%j")

    names = [
        f"BRDC00WRD_R_{yyyy}{doy}0000_01D_MN.rnx.gz",
        f"brdc{doy}0.{yy}n.gz",
    ]

    out: list[DownloadCandidate] = []
    for mirror_name, base_url, needs_auth in _mirrors_from_env(
        "ORBITBASE_BRDC_MIRRORS", DEFAULT_BRDC_MIRRORS
    ):
        if needs_auth and not has_credentials:
            continue
        base = base_url.format(yyyy=yyyy, doy=doy, yy=yy)
        for name in names:
            out.append(
                DownloadCandidate(
                    url=base + name, filename=name, compressed=True, source=mirror_name
                )
            )
    return out


def _host_of(url: str) -> str:
    return urlparse(url).hostname or url


def _is_unreachable_error(exc: Exception) -> bool:
    return isinstance(
        exc, (requests.ConnectTimeout, requests.ConnectionError, requests.ReadTimeout)
    )


def _find_cached_sp3(day_root: Path) -> Path | None:
    candidates: list[Path] = []

    manual_dir = day_root / "manual_sp3"
    if manual_dir.exists():
        candidates.extend(sorted(manual_dir.glob("*.sp3")))
        candidates.extend(sorted(manual_dir.glob("*.SP3")))

    candidates.extend(sorted(day_root.glob("*.sp3")))
    candidates.extend(sorted(day_root.glob("*.SP3")))

    best = _pick_best_sp3(candidates)
    return best


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

    dead_hosts: set[str] = set()
    auth_failed = False

    if not candidates:
        errors.append("Nenhum espelho SP3 disponível para tentar.")

    for candidate in candidates:
        host = _host_of(candidate.url)
        if host in dead_hosts:
            continue

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
                if "cddis.nasa.gov" in candidate.url:
                    auth_failed = True
                    dead_hosts.add(host)
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
                    "manual_upload_used": False,
                    "earthdata_auth_failed": auth_failed,
                    "unreachable_hosts": sorted(dead_hosts),
                }

            errors.append(f"Arquivo baixado mas inválido como SP3: {candidate.url}")
            _safe_unlink(final_path)

        except HtmlInsteadOfFileError as exc:
            if "cddis.nasa.gov" in candidate.url:
                auth_failed = True
                # Login recusado: os demais arquivos do CDDIS falhariam igual.
                dead_hosts.add(host)
            errors.append(f"Resposta HTML/login em vez de SP3 :: {candidate.url} :: {exc}")
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "?"
            if status_code in (401, 403) and "cddis.nasa.gov" in candidate.url:
                auth_failed = True
                dead_hosts.add(host)
            errors.append(f"HTTP {status_code} :: {candidate.url}")
        except requests.RequestException as exc:
            if _is_unreachable_error(exc):
                dead_hosts.add(host)
            errors.append(f"Request error (timeout/conexão) :: {candidate.url} :: {exc}")
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
        "manual_upload_used": False,
        "earthdata_auth_failed": auth_failed,
        "unreachable_hosts": sorted(dead_hosts),
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
            is_manual = "manual_sp3" in str(cached_sp3)

            payload = {
                "session_date": session_date,
                "sp3_available": True,
                "clk_available": False,
                "sp3_path": str(cached_sp3),
                "clk_path": None,
                "sp3_source_url": previous_manifest.get("sp3_source_url"),
                "sp3_source_filename": cached_sp3.name,
                "status": "manual_cached" if is_manual else "cached",
                "sp3_cached": True,
                "checked_at": datetime.utcnow().isoformat() + "Z",
                "download_errors": previous_manifest.get("download_errors", []),
                "auth_mode": auth_mode,
                "earthdata_username_masked": masked_username,
                "manual_upload_used": is_manual,
                "manual_sp3_files": previous_manifest.get("manual_sp3_files", []),
                "manual_upload_count": previous_manifest.get("manual_upload_count", 0),
                "valid_sp3_count": previous_manifest.get("valid_sp3_count", 0),
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
        "earthdata_auth_failed": download_result.get("earthdata_auth_failed", False),
        "unreachable_hosts": download_result.get("unreachable_hosts", []),
        "auth_mode": auth_mode,
        "earthdata_username_masked": masked_username,
        "manual_upload_used": False,
        "manual_sp3_files": previous_manifest.get("manual_sp3_files", []),
        "manual_upload_count": previous_manifest.get("manual_upload_count", 0),
        "valid_sp3_count": previous_manifest.get("valid_sp3_count", 0),
    }

    _write_json(manifest_path, payload)
    return payload


def _looks_like_valid_nav(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 200:
            return False
        head = path.read_bytes()[:2048].decode("utf-8", errors="ignore").upper()
        return "RINEX VERSION / TYPE" in head and "NAV" in head
    except Exception:
        return False


def ensure_broadcast_navigation(
    session_date: str,
    storage_root: Path,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """
    Baixa (ou reaproveita do cache) o BRDC diário combinado do IGS em
    ``storage/precise_products/<data>/brdc``. Usado só quando nem SP3 nem os
    NAV do ZIP RBMC estão disponíveis.
    """
    brdc_dir = storage_root / "precise_products" / session_date / "brdc"
    brdc_dir.mkdir(parents=True, exist_ok=True)

    for cached in sorted(brdc_dir.iterdir()):
        if cached.is_file() and _looks_like_valid_nav(cached):
            return {"nav_available": True, "nav_path": str(cached), "cached": True,
                    "download_errors": [], "brdc_dir": str(brdc_dir)}

    errors: list[str] = []
    dead_hosts: set[str] = set()
    has_credentials = bool(earthdata_username and earthdata_password)

    for candidate in brdc_product_candidates(session_date, has_credentials):
        host = _host_of(candidate.url)
        if host in dead_hosts:
            continue

        tmp_path = brdc_dir / (candidate.filename + ".part")
        final_path = brdc_dir / _final_sp3_output_name(candidate.filename)
        try:
            print(f"[ADV-BRDC] tentando {candidate.url}", flush=True)
            needs_auth = "cddis.nasa.gov" in candidate.url
            _download_file(
                candidate.url,
                tmp_path,
                timeout_seconds=timeout_seconds,
                username=earthdata_username if needs_auth else None,
                password=earthdata_password if needs_auth else None,
            )
            if _looks_like_html_response(tmp_path):
                raise HtmlInsteadOfFileError(f"HTML em vez de NAV: {candidate.url}")

            raw = tmp_path.read_bytes()
            if raw[:2] == b"\x1f\x8b":
                final_path.write_bytes(gzip.decompress(raw))
            elif raw[:2] == b"\x1f\x9d":
                _decompress_Z_file(tmp_path, final_path)
            else:
                final_path.write_bytes(raw)

            if _looks_like_valid_nav(final_path):
                print(f"[ADV-BRDC] NAV válido: {final_path}", flush=True)
                return {"nav_available": True, "nav_path": str(final_path),
                        "cached": False, "source_url": candidate.url,
                        "download_errors": errors, "brdc_dir": str(brdc_dir)}

            errors.append(f"Arquivo baixado não é NAV RINEX válido :: {candidate.url}")
            _safe_unlink(final_path)
        except HtmlInsteadOfFileError as exc:
            dead_hosts.add(host)
            errors.append(str(exc))
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            errors.append(f"HTTP {code} :: {candidate.url}")
        except requests.RequestException as exc:
            if _is_unreachable_error(exc):
                dead_hosts.add(host)
            errors.append(f"Request error (timeout/conexão) :: {candidate.url} :: {exc}")
        except Exception as exc:
            errors.append(f"Erro :: {candidate.url} :: {type(exc).__name__}: {exc}")
        finally:
            _safe_unlink(tmp_path)

    return {"nav_available": False, "nav_path": None, "cached": False,
            "download_errors": errors, "brdc_dir": str(brdc_dir)}
