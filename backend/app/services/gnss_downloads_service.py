"""Central de downloads: efemérides transmitidas e RINEX da RBMC por data.

Efemérides: baixa o BRDC diário combinado do IGS (espelho aberto do BKG; o
CDDIS entra só se houver login Earthdata) e converte o arquivo misto RINEX 3
para RINEX 2.11 nos dois arquivos que o Topcon Tools e afins esperam:
``brdcDDD0.YYn`` (GPS) e ``brdcDDD0.YYg`` (GLONASS). Se o BRDC do IGS não
estiver disponível, usa a navegação do ZIP diário de uma estação RBMC.

RBMC: reaproveita o mesmo cache do processamento VRS
(``storage/rbmc/<estação>/<data>``), com o ZIP publicado pelo IBGE.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MAX_DAYS = 31
MAX_STATIONS = 10
TASK_TTL_SECONDS = 24 * 3600

# Estações usadas para tirar a navegação do ZIP da RBMC quando o BRDC do IGS
# não estiver disponível (depois das escolhidas pelo usuário).
DEFAULT_NAV_FALLBACK_STATIONS = ["BRAZ", "POAL", "ONRJ", "SALU"]

_RECORD_START = re.compile(r"^([A-Z])([ \d]\d) (\d{4}) ")


# --------------------------------------------------------------------------
# Datas
# --------------------------------------------------------------------------

def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise ValueError(f"Data inválida: {value!r} (use AAAA-MM-DD).") from exc


def date_range(start: str, end: str | None = None) -> list[date]:
    first = parse_day(start)
    last = parse_day(end) if end else first
    if last < first:
        first, last = last, first
    days = (last - first).days + 1
    if days > MAX_DAYS:
        raise ValueError(f"Escolha no máximo {MAX_DAYS} dias por vez ({days} pedidos).")
    if last > datetime.now(timezone.utc).date():
        raise ValueError("A data final está no futuro.")
    return [first + timedelta(days=i) for i in range(days)]


def day_label(day: date) -> str:
    """Pasta do dia no ZIP: '2026-10-01 (dia 274)'."""
    return f"{day.isoformat()} (dia {day.strftime('%j')})"


def brdc_rinex2_name(day: date, kind: str) -> str:
    return f"brdc{day.strftime('%j')}0.{day.strftime('%y')}{kind}"


# --------------------------------------------------------------------------
# RINEX 3/4 misto -> RINEX 2.11 (.n GPS e .g GLONASS)
# --------------------------------------------------------------------------

def _num(text: str) -> float | None:
    text = text.strip().replace("D", "E").replace("d", "e")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _d19(value: float | None) -> str:
    return f"{(value or 0.0):19.12E}".replace("E", "D")


def _d12(value: float | None) -> str:
    return f"{(value or 0.0):12.4E}".replace("E", "D")


def _fields(text: str, count: int, width: int = 19) -> list[float | None]:
    text = text.rstrip("\n").ljust(count * width)
    return [_num(text[i * width:(i + 1) * width]) for i in range(count)]


def _header_line(content: str, label: str) -> str:
    return content[:60].ljust(60) + label


def _read_rinex3_nav(text: str) -> tuple[dict, dict[str, list[tuple]]]:
    """Lê o cabeçalho útil e os registros GPS (LNAV) e GLONASS (FDMA)."""
    lines = text.splitlines()
    header = {"version": 0.0, "ion_alpha": None, "ion_beta": None, "gput": None,
              "leap": None}
    i = 0
    for i, line in enumerate(lines):
        label = line[60:].strip()
        if label == "RINEX VERSION / TYPE":
            header["version"] = _num(line[:9]) or 0.0
        elif label == "IONOSPHERIC CORR" and line[:4] in ("GPSA", "GPSB"):
            vals = [_num(line[5 + k * 12:17 + k * 12]) for k in range(4)]
            header["ion_alpha" if line[:4] == "GPSA" else "ion_beta"] = vals
        elif label == "TIME SYSTEM CORR" and line[:4] == "GPUT":
            header["gput"] = (_num(line[5:22]), _num(line[22:38]),
                              int(_num(line[38:45]) or 0), int(_num(line[45:50]) or 0))
        elif label == "LEAP SECONDS":
            header["leap"] = int(_num(line[:6]) or 0)
        elif label == "END OF HEADER":
            break
    else:
        raise ValueError("Arquivo de navegação sem END OF HEADER.")

    if header["version"] < 3:
        raise ValueError("O arquivo não é RINEX 3/4 de navegação.")

    records: dict[str, list[tuple]] = {"G": [], "R": []}
    accept = True
    k = i + 1
    while k < len(lines):
        line = lines[k]
        if line.startswith(">"):
            tok = line[1:].split()
            accept = (len(tok) >= 3 and tok[0] == "EPH" and (
                (tok[1].startswith("G") and tok[2] == "LNAV")
                or (tok[1].startswith("R") and tok[2] == "FDMA")))
            k += 1
            continue
        m = _RECORD_START.match(line)
        if not m:
            k += 1
            continue
        system = m.group(1)
        body = []
        k += 1
        while k < len(lines) and lines[k].startswith("    ") and not _RECORD_START.match(lines[k]):
            body.append(lines[k])
            k += 1
        need = {"G": 7, "R": 3}.get(system)
        if not accept or need is None or len(body) < need:
            continue
        try:
            prn = int(m.group(2))
            stamp = datetime(int(line[4:8]), int(line[9:11]), int(line[12:14]),
                             int(line[15:17]), int(line[18:20]), int(line[21:23]))
        except ValueError:
            continue
        first = _fields(line[23:], 3)
        orbit = [_fields(row[4:], 4) for row in body[:need]]
        records[system].append((stamp, prn, first, orbit))
    return header, records


def _rinex2_record(stamp: datetime, prn: int, first: list, orbit: list) -> str:
    out = (f"{prn:2d} {stamp.year % 100:02d} {stamp.month:2d} {stamp.day:2d} "
           f"{stamp.hour:2d} {stamp.minute:2d}{stamp.second:5.1f}"
           + "".join(_d19(v) for v in first) + "\n")
    for row in orbit:
        out += "   " + "".join(_d19(v) for v in row) + "\n"
    return out


def _dedupe_sorted(recs: list[tuple]) -> list[tuple]:
    seen = set()
    out = []
    for rec in sorted(recs, key=lambda r: (r[0], r[1])):
        key = (rec[0], rec[1], tuple(rec[2]), tuple(tuple(r) for r in rec[3]))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def rinex3_nav_to_rinex2(text: str, source_name: str = "") -> dict[str, str | None]:
    """Converte navegação RINEX 3/4 mista em textos RINEX 2.11 {'n': GPS, 'g': GLONASS}."""
    header, records = _read_rinex3_nav(text)
    now = datetime.now(timezone.utc).strftime("%Y%m%d %H%M%S UTC")
    pgm = _header_line(f"{'OrbitBase':<20}{'':<20}{now:<20}", "PGM / RUN BY / DATE")
    comment = (_header_line(f"Convertido de {source_name}"[:60], "COMMENT")
               if source_name else None)
    leap = (_header_line(f"{header['leap']:6d}", "LEAP SECONDS")
            if header["leap"] is not None else None)

    out: dict[str, str | None] = {"n": None, "g": None}

    gps = _dedupe_sorted(records["G"])
    if gps:
        lines = [_header_line("     2.11           N: GPS NAV DATA", "RINEX VERSION / TYPE"), pgm]
        if comment:
            lines.append(comment)
        if header["ion_alpha"]:
            lines.append(_header_line("  " + "".join(_d12(v) for v in header["ion_alpha"]), "ION ALPHA"))
        if header["ion_beta"]:
            lines.append(_header_line("  " + "".join(_d12(v) for v in header["ion_beta"]), "ION BETA"))
        if header["gput"]:
            a0, a1, tot, week = header["gput"]
            lines.append(_header_line(f"   {_d19(a0)}{_d19(a1)}{tot:9d}{week:9d}", "DELTA-UTC: A0,A1,T,W"))
        if leap:
            lines.append(leap)
        lines.append(_header_line("", "END OF HEADER"))
        out["n"] = "\n".join(lines) + "\n" + "".join(_rinex2_record(*r) for r in gps)

    glo = _dedupe_sorted(records["R"])
    if glo:
        lines = [_header_line("     2.11           G: GLONASS NAV DATA", "RINEX VERSION / TYPE"), pgm]
        if comment:
            lines.append(comment)
        if leap:
            lines.append(leap)
        lines.append(_header_line("", "END OF HEADER"))
        out["g"] = "\n".join(lines) + "\n" + "".join(_rinex2_record(*r) for r in glo)

    return out


def _rinex2_nav_kind(path: Path) -> str | None:
    """'n' ou 'g' para um arquivo RINEX 2 de navegação (pelo cabeçalho)."""
    try:
        head = path.read_bytes()[:400].decode("ascii", errors="ignore")
    except OSError:
        return None
    first = head.splitlines()[0] if head else ""
    if "RINEX VERSION / TYPE" not in first:
        return None
    version = _num(first[:9]) or 0.0
    if version >= 3:
        return None
    letter = first[20:21].upper()
    return {"N": "n", "G": "g"}.get(letter)


# --------------------------------------------------------------------------
# Efemérides de um dia
# --------------------------------------------------------------------------

def _read_text_maybe_gz(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("ascii", errors="ignore")


def _rbmc_nav_files(code: str, day: date) -> dict[str, Path]:
    from app.services.rbmc_observation_sync_service import ensure_rbmc_station_files

    state = ensure_rbmc_station_files(code, day.isoformat())
    found: dict[str, Path] = {}
    for root in (Path(state["extracted_root"]), Path(state["day_root"])):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() in (".zip", ".gz", ".z"):
                continue
            kind = _rinex2_nav_kind(path)
            if kind and kind not in found:
                found[kind] = path
    return found


def ensure_daily_ephemeris(
    day: date,
    storage_root: Path,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    fallback_stations: list[str] | None = None,
    include_rinex3: bool = False,
) -> dict:
    """Garante brdcDDD0.YYn e brdcDDD0.YYg do dia em cache e devolve os caminhos."""
    from app.services.vrs_advanced.advanced_ephemeris_service import (
        ensure_broadcast_navigation,
    )

    out_dir = storage_root / "precise_products" / day.isoformat() / "rinex2"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {k: out_dir / brdc_rinex2_name(day, k) for k in ("n", "g")}
    source_file = out_dir / "fonte.json"

    result: dict = {"files": {}, "sources": {}, "errors": [], "rinex3": None}
    if source_file.exists():
        try:
            result["sources"] = json.loads(source_file.read_text(encoding="utf-8"))
        except Exception:
            result["sources"] = {}

    need = [k for k, p in targets.items() if not (p.exists() and p.stat().st_size > 0)]

    if need or include_rinex3:
        brdc = ensure_broadcast_navigation(
            day.isoformat(), storage_root,
            earthdata_username=earthdata_username,
            earthdata_password=earthdata_password,
        )
        nav_path = Path(brdc["nav_path"]) if brdc.get("nav_path") else None
        if nav_path is None:
            result["errors"].append(
                "BRDC do IGS indisponível para o dia: "
                + "; ".join(brdc.get("download_errors", [])[:3]))
        else:
            text = _read_text_maybe_gz(nav_path)
            version = _num(text[:9]) or 0.0
            if version >= 3:
                if include_rinex3:
                    result["rinex3"] = str(nav_path)
                if need:
                    try:
                        converted = rinex3_nav_to_rinex2(text, nav_path.name)
                        for kind in list(need):
                            if converted.get(kind):
                                targets[kind].write_text(converted[kind], encoding="ascii")
                                result["sources"][kind] = f"IGS BRDC ({nav_path.name})"
                                need.remove(kind)
                    except Exception as exc:  # noqa: BLE001
                        result["errors"].append(f"Falha ao converter {nav_path.name}: {exc}")
            elif "n" in need and _rinex2_nav_kind(nav_path) == "n":
                shutil.copyfile(nav_path, targets["n"])
                result["sources"]["n"] = f"IGS BRDC ({nav_path.name})"
                need.remove("n")

    if need:
        tried = []
        for code in list(dict.fromkeys(
            [c.upper() for c in (fallback_stations or [])] + DEFAULT_NAV_FALLBACK_STATIONS
        )):
            if not need:
                break
            tried.append(code)
            try:
                navs = _rbmc_nav_files(code, day)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"RBMC {code}: {exc}")
                continue
            for kind in list(need):
                if kind in navs:
                    shutil.copyfile(navs[kind], targets[kind])
                    result["sources"][kind] = f"RBMC {code} ({navs[kind].name})"
                    need.remove(kind)

    try:
        source_file.write_text(json.dumps(result["sources"], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    for kind, path in targets.items():
        if path.exists() and path.stat().st_size > 0:
            result["files"][kind] = str(path)
    result["missing"] = need
    return result


# --------------------------------------------------------------------------
# Órbitas precisas (SP3)
# --------------------------------------------------------------------------

# (rótulo, prefixo do nome curto, nome longo, URL base, exige Earthdata).
# Ordem: final antes de rápida; dentro de cada nível, CODE (GPS + GLONASS)
# antes do IGS combinado (só GPS). {yyyy} {doy} {week} são preenchidos.
SP3_PRODUCTS: list[tuple[str, str, str, str, bool]] = [
    ("CODE final", "cod", "COD0OPSFIN_{yyyy}{doy}0000_01D_05M_ORB.SP3.gz",
     "https://ftp.aiub.unibe.ch/CODE/{yyyy}/", False),
    ("IGS final", "igs", "IGS0OPSFIN_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
     "https://igs.bkg.bund.de/root_ftp/IGS/products/{week}/", False),
    ("IGS final", "igs", "IGS0OPSFIN_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
     "https://cddis.nasa.gov/archive/gnss/products/{week}/", True),
    ("CODE rápida", "cor", "COD0OPSRAP_{yyyy}{doy}0000_01D_05M_ORB.SP3",
     "https://ftp.aiub.unibe.ch/CODE/", False),
    ("IGS rápida", "igr", "IGS0OPSRAP_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
     "https://igs.bkg.bund.de/root_ftp/IGS/products/{week}/", False),
    ("IGS rápida", "igr", "IGS0OPSRAP_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
     "https://cddis.nasa.gov/archive/gnss/products/{week}/", True),
]


def gps_week_day(day: date) -> tuple[int, int]:
    days = (day - date(1980, 1, 6)).days
    return days // 7, days % 7


def sp3_short_name(prefix: str, day: date) -> str:
    """Nome curto clássico (aceito pelo Topcon Tools): igs24384.sp3."""
    week, dow = gps_week_day(day)
    return f"{prefix}{week}{dow}.sp3"


def sp3_systems(path: Path) -> list[str]:
    """Sistemas presentes no SP3 pelas linhas '+' do cabeçalho (G, R, E...)."""
    found: list[str] = []
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("* "):
                    break
                if line.startswith("+ ") or line.startswith("+\t"):
                    for k in range(9, 60, 3):
                        letter = line[k:k + 1]
                        if letter.isalpha() and letter not in found:
                            found.append(letter)
    except OSError:
        pass
    return found


def _decompress_to(raw_path: Path, dest: Path) -> None:
    raw = raw_path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        dest.write_bytes(gzip.decompress(raw))
    elif raw[:2] == b"\x1f\x9d":
        from app.services.vrs_advanced.advanced_ephemeris_service import _decompress_Z_file

        _decompress_Z_file(raw_path, dest)
    else:
        dest.write_bytes(raw)


def ensure_daily_precise_orbit(
    day: date,
    storage_root: Path,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
) -> dict:
    """Baixa o melhor SP3 disponível do dia e grava com o nome curto.

    Cache em ``precise_products/<data>/sp3_curto``. Um SP3 final substitui um
    rápido guardado antes, quando o final já foi publicado.
    """
    from app.services.vrs_advanced import advanced_ephemeris_service as eph

    out_dir = storage_root / "precise_products" / day.isoformat() / "sp3_curto"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "fonte.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    cached = out_dir / meta.get("file", "-")
    if cached.is_file() and "final" in meta.get("product", ""):
        return {**meta, "path": str(cached), "errors": [], "cached": True}

    week, _ = gps_week_day(day)
    fields = {"yyyy": day.strftime("%Y"), "doy": day.strftime("%j"), "week": week}
    has_login = bool(earthdata_username and earthdata_password)
    errors: list[str] = []
    dead_hosts: set[str] = set()

    for label, prefix, name_tpl, base_tpl, needs_login in SP3_PRODUCTS:
        if needs_login and not has_login:
            continue
        if cached.is_file() and "final" not in label:
            # já temos uma rápida e o final ainda não saiu
            return {**meta, "path": str(cached), "errors": errors, "cached": True}
        url = base_tpl.format(**fields) + name_tpl.format(**fields)
        host = eph._host_of(url)
        if host in dead_hosts:
            continue
        tmp = out_dir / (name_tpl.format(**fields) + ".part")
        final = out_dir / sp3_short_name(prefix, day)
        try:
            eph._download_file(
                url, tmp,
                username=earthdata_username if needs_login else None,
                password=earthdata_password if needs_login else None,
            )
            if eph._looks_like_html_response(tmp):
                raise eph.HtmlInsteadOfFileError(f"HTML em vez de SP3: {url}")
            _decompress_to(tmp, final)
            if not eph._looks_like_valid_sp3(final):
                errors.append(f"Arquivo não é SP3 válido :: {url}")
                final.unlink(missing_ok=True)
                continue
            systems = sp3_systems(final)
            meta = {"file": final.name, "product": label, "source_name": name_tpl.format(**fields),
                    "source_url": url, "systems": systems}
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            if cached.is_file() and cached != final:
                cached.unlink(missing_ok=True)
            return {**meta, "path": str(final), "errors": errors, "cached": False}
        except eph.HtmlInsteadOfFileError as exc:
            dead_hosts.add(host)
            errors.append(str(exc))
        except eph.requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            errors.append(f"HTTP {code} :: {url}")
        except eph.requests.RequestException as exc:
            dead_hosts.add(host)
            errors.append(f"Sem conexão :: {url}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Erro :: {url} :: {type(exc).__name__}: {exc}")
        finally:
            tmp.unlink(missing_ok=True)

    if cached.is_file():
        return {**meta, "path": str(cached), "errors": errors, "cached": True}
    return {"path": None, "errors": errors}


# --------------------------------------------------------------------------
# RBMC de um dia
# --------------------------------------------------------------------------

def collect_rbmc_day(code: str, day: date, convert_hatanaka: bool = True) -> tuple[list[Path], list[str]]:
    """Arquivos do ZIP diário do IBGE da estação (com .d convertido em .o, se pedido)."""
    from app.services.rbmc_observation_sync_service import ensure_rbmc_station_files

    state = ensure_rbmc_station_files(code, day.isoformat())
    extracted = Path(state["extracted_root"])
    converted = Path(state["converted_root"])
    notes: list[str] = []
    files: list[Path] = []

    originals = sorted(p for p in extracted.rglob("*") if p.is_file()) if extracted.exists() else []
    if not originals:
        raise FileNotFoundError(f"ZIP da estação {code} sem arquivos.")

    if not convert_hatanaka:
        return originals, notes

    obs_o = sorted(p for p in converted.rglob("*") if p.is_file()
                   and re.search(r"\.\d{2}o$", p.name.lower())) if converted.exists() else []
    for path in originals:
        is_hatanaka = bool(re.search(r"\.\d{2}d$", path.name.lower())) or path.name.lower().endswith(".crx")
        if is_hatanaka and obs_o:
            continue  # substituído pelo .o convertido
        if is_hatanaka:
            notes.append(f"{code}: não foi possível converter {path.name} (Hatanaka); enviado como veio do IBGE.")
        files.append(path)
    files.extend(p for p in obs_o if p.name not in {f.name for f in files})
    return files, notes


# --------------------------------------------------------------------------
# Tarefas em segundo plano
# --------------------------------------------------------------------------

def tasks_root(storage_root: Path, user_id: int) -> Path:
    return storage_root / "downloads" / f"u{int(user_id)}"


def _write_status(task_dir: Path, **fields) -> None:
    path = task_dir / "status.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_status(task_dir: Path) -> dict | None:
    path = task_dir / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cleanup_old_tasks(root: Path) -> None:
    if not root.exists():
        return
    limit = time.time() - TASK_TTL_SECONDS
    for task in root.iterdir():
        try:
            if task.is_dir() and task.stat().st_mtime < limit:
                shutil.rmtree(task, ignore_errors=True)
        except OSError:
            pass


def _short(error, limit: int = 140) -> str:
    text = " ".join(str(error).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_package(
    task_dir: Path,
    storage_root: Path,
    days: list[date],
    ephemeris: bool,
    include_rinex3: bool,
    stations: list[str],
    convert_hatanaka: bool,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    precise: bool = False,
) -> Path:
    total = len(days) * ((1 if ephemeris else 0) + (1 if precise else 0) + len(stations)) or 1
    done = 0
    log: list[str] = []
    entries: list[tuple[Path, str]] = []

    def progress(message: str) -> None:
        _write_status(task_dir, status="running", percent=int(100 * done / total),
                      message=message, log=log[-200:])

    for day in days:
        folder = day_label(day)
        if ephemeris:
            progress(f"{day.strftime('%d/%m/%Y')}: efemérides GPS e GLONASS")
            try:
                eph = ensure_daily_ephemeris(
                    day, storage_root, earthdata_username, earthdata_password,
                    fallback_stations=stations, include_rinex3=include_rinex3,
                )
                for kind, raw in sorted(eph["files"].items()):
                    entries.append((Path(raw), f"{folder}/Efemerides/{Path(raw).name}"))
                    label = "GPS" if kind == "n" else "GLONASS"
                    log.append(f"{day.strftime('%d/%m/%Y')} {label}: {Path(raw).name} "
                               f"({eph['sources'].get(kind, 'cache')})")
                if eph.get("rinex3"):
                    raw = Path(eph["rinex3"])
                    entries.append((raw, f"{folder}/Efemerides/RINEX3/{raw.name}"))
                for kind in eph.get("missing", []):
                    label = "GPS (.n)" if kind == "n" else "GLONASS (.g)"
                    reason = _short(eph["errors"][0]) if eph.get("errors") else ""
                    log.append(f"{day.strftime('%d/%m/%Y')}: efeméride {label} não encontrada"
                               + (f" ({reason})." if reason else "."))
            except Exception as exc:  # noqa: BLE001
                log.append(f"{day.strftime('%d/%m/%Y')}: erro nas efemérides: {exc}")
            done += 1

        if precise:
            progress(f"{day.strftime('%d/%m/%Y')}: órbita precisa (SP3)")
            try:
                sp3 = ensure_daily_precise_orbit(day, storage_root, earthdata_username, earthdata_password)
                if sp3.get("path"):
                    raw = Path(sp3["path"])
                    entries.append((raw, f"{folder}/Orbita_precisa/{raw.name}"))
                    systems = sp3.get("systems") or []
                    which = "GPS + GLONASS" if "R" in systems else "somente GPS"
                    log.append(f"{day.strftime('%d/%m/%Y')} órbita precisa: {raw.name} "
                               f"({sp3.get('product')}, {which}; original {sp3.get('source_name')})")
                else:
                    reason = _short(sp3["errors"][0]) if sp3.get("errors") else ""
                    log.append(f"{day.strftime('%d/%m/%Y')}: órbita precisa ainda não publicada"
                               + (f" ({reason})." if reason else "."))
            except Exception as exc:  # noqa: BLE001
                log.append(f"{day.strftime('%d/%m/%Y')}: erro na órbita precisa: {_short(exc)}")
            done += 1

        for code in stations:
            progress(f"{day.strftime('%d/%m/%Y')}: RBMC {code}")
            try:
                files, notes = collect_rbmc_day(code, day, convert_hatanaka)
                for path in files:
                    entries.append((path, f"{folder}/RBMC/{code}/{path.name}"))
                log.extend(notes)
                log.append(f"{day.strftime('%d/%m/%Y')} RBMC {code}: {len(files)} arquivo(s).")
            except Exception as exc:  # noqa: BLE001
                log.append(f"{day.strftime('%d/%m/%Y')} RBMC {code}: sem dados no IBGE ({_short(exc)}).")
            done += 1

    if not entries:
        raise FileNotFoundError("Nenhum arquivo encontrado para as datas escolhidas.")

    _write_status(task_dir, status="running", percent=99, message="Compactando", log=log[-200:])
    first, last = days[0], days[-1]
    name = (f"orbitbase_{first.strftime('%Y%m%d')}"
            + (f"_{last.strftime('%Y%m%d')}" if last != first else "") + ".zip")
    out = task_dir / name
    tmp = out.with_suffix(".part")
    seen = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, arcname in entries:
            if arcname in seen or not path.is_file():
                continue
            seen.add(arcname)
            zf.write(path, arcname=arcname)
        readme = ["OrbitBase - downloads de efemérides e RBMC", ""] + log
        if precise:
            readme += ["", "Topcon Tools: importe o .sp3 da pasta Orbita_precisa na obra",
                       "(Obra > Importar, tipo Efemérides precisas / SP3) e processe de novo.",
                       "Órbita final sai ~2 semanas depois do dia; antes disso vem a rápida.",
                       "O IGS combinado tem só GPS; o CODE tem GPS + GLONASS."]
        zf.writestr("LEIAME.txt", "\r\n".join(readme) + "\r\n")
    tmp.replace(out)
    return out


def start_download_task(
    storage_root: Path,
    user_id: int,
    days: list[date],
    ephemeris: bool,
    include_rinex3: bool,
    stations: list[str],
    convert_hatanaka: bool,
    earthdata_username: str | None = None,
    earthdata_password: str | None = None,
    precise: bool = False,
) -> str:
    root = tasks_root(storage_root, user_id)
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_old_tasks(root)
    task_id = uuid.uuid4().hex[:16]
    task_dir = root / task_id
    task_dir.mkdir()
    _write_status(task_dir, status="queued", percent=0, message="Na fila", log=[],
                  created_at=datetime.now(timezone.utc).isoformat())

    def run() -> None:
        try:
            out = build_package(
                task_dir, storage_root, days, ephemeris, include_rinex3, stations,
                convert_hatanaka, earthdata_username, earthdata_password,
                precise=precise,
            )
            status = read_status(task_dir) or {}
            _write_status(task_dir, status="done", percent=100, message="Pronto",
                          file_name=out.name, file_size=out.stat().st_size,
                          log=status.get("log", []))
        except Exception as exc:  # noqa: BLE001
            _write_status(task_dir, status="failed", percent=100, message=str(exc))

    threading.Thread(target=run, name=f"downloads-{task_id}", daemon=True).start()
    return task_id
