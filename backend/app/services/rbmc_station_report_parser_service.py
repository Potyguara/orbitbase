from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def _extract(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    if not match:
        return None
    value = match.group(1)
    return value.strip() if value else None


def _normalize_spaces(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()


def _parse_br_float(value: str | None) -> float | None:
    if not value:
        return None

    cleaned = value.strip().replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")

    try:
        return float(cleaned)
    except Exception:
        return None


def _parse_int_safe(value: str | None) -> int | None:
    if not value:
        return None

    cleaned = value.strip()
    cleaned = re.sub(r"[^\d\-]", "", cleaned)

    if not cleaned:
        return None

    try:
        return int(cleaned)
    except Exception:
        return None


def _parse_coord_dms_to_decimal(value: str | None) -> float | None:
    """
    Exemplos comuns:
    + 02º 50' 42,65645"
    - 60º 42' 04,01375"
    +02 50 42,65645
    """
    if not value:
        return None

    text = value.strip().replace("º", " ").replace("°", " ")
    text = text.replace("'", " ").replace('"', " ")
    text = re.sub(r"\s+", " ", text)

    match = re.search(r"([+\-])\s*(\d+)\s+(\d+)\s+([\d,\.]+)", text)
    if not match:
        return None

    sign = -1 if match.group(1) == "-" else 1
    deg = float(match.group(2))
    minute = float(match.group(3))
    sec = float(match.group(4).replace(",", "."))

    decimal = deg + (minute / 60.0) + (sec / 3600.0)
    return sign * decimal


def _parse_datetime_pt(value: str | None) -> datetime | None:
    if not value:
        return None

    match = re.search(r"(\d{2}/\d{2}/\d{4})\s+às\s+(\d{2}:\d{2})", value)
    if not match:
        return None

    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)}",
            "%d/%m/%Y %H:%M",
        )
    except Exception:
        return None


_UF_BY_NAME = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS",
    "minas gerais": "MG", "para": "PA", "paraiba": "PB", "parana": "PR",
    "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO",
    "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
    "tocantins": "TO",
}


def _parse_state(value: str | None) -> str | None:
    """Converte o campo Estado (sigla ou nome por extenso) em UF; None se não for brasileiro."""
    if not value:
        return None
    import unicodedata

    raw = value.strip()
    if re.fullmatch(r"[A-Z]{2}", raw[:2]) and (len(raw) == 2 or not raw[2].isalpha()):
        return raw[:2] if raw[:2] in _UF_BY_NAME.values() else None
    norm = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower()
    norm = re.sub(r"[^a-z ]", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    # tenta o nome mais longo primeiro ("mato grosso do sul" antes de "mato grosso", "para" por último)
    for name in sorted(_UF_BY_NAME, key=len, reverse=True):
        if norm == name or norm.startswith(name + " "):
            return _UF_BY_NAME[name]
    return None


def infer_station_code_from_filename(file_name: str) -> str | None:
    if not file_name:
        return None

    base = Path(file_name).name
    match = re.search(r"Descritivo_([A-Za-z0-9]{4})\.pdf$", base, re.IGNORECASE)
    if not match:
        return None

    return match.group(1).upper()


def parse_rbmc_station_report_text(text: str, file_name: str | None = None) -> dict:
    if not text:
        return {
            "station_code": infer_station_code_from_filename(file_name or ""),
            "station_name": None,
            "sat_code": None,
            "international_code": None,
            "city": None,
            "state": None,
            "latitude": None,
            "longitude": None,
            "ellipsoidal_height": None,
            "x": None,
            "y": None,
            "z": None,
            "utm_n": None,
            "utm_e": None,
            "mc": None,
            "receiver_model": None,
            "receiver_serial": None,
            "receiver_firmware": None,
            "last_firmware_update": None,
            "antenna_model": None,
            "antenna_serial": None,
            "antenna_height": None,
            "report_reference_date": None,
        }

    station_name = _normalize_spaces(_extract(r"Nome da Estação:\s*(.+)", text))

    station_code = _normalize_spaces(
        _extract(r"Ident\.?\s*da\s*Estação\s*[:\-–]\s*([A-Za-z0-9]{4,10})", text)
    )
    if not station_code:
        station_code = infer_station_code_from_filename(file_name or "")

    sat_code = _normalize_spaces(_extract(r"Código SAT:\s*(.+)", text))
    international_code = _normalize_spaces(
        _extract(r"Código Internacional:\s*(.+)", text)
    )

    city = _normalize_spaces(_extract(r"Cidade:\s*(.+)", text))
    state = _parse_state(_extract(r"Estado:\s*(.+)", text))

    lat_text = _extract(r"Latitude:\s*(.+?)\s+Sigma:", text, re.S)
    lon_text = _extract(r"Longitude:\s*(.+?)\s+Sigma:", text, re.S)
    h_text = _extract(r"Alt\.?\s*Elip\.?:\s*(.+?)\s+Sigma:", text, re.S)

    x_text = _extract(r"\bX:\s*([-\d\.,]+)\s*m", text)
    y_text = _extract(r"\bY:\s*([-\d\.,]+)\s*m", text)
    z_text = _extract(r"\bZ:\s*([-\d\.,]+)\s*m", text)

    utm_n_text = _extract(r"UTM\s*\(N\):\s*([-\d\.,]+)\s*m", text)
    utm_e_text = _extract(r"UTM\s*\(E\):\s*([-\d\.,]+)\s*m", text)
    mc_text = _extract(r"\bMC:\s*([-\d]+)", text)

    receiver_model = _normalize_spaces(
        _extract(r"4\.1\.1\s*Tipo do Receptor\s*-\s*(.+)", text)
    )
    receiver_serial = _normalize_spaces(
        _extract(r"4\.1\.1.*?Número de Série\s*-\s*(.+)", text, re.S)
    )
    receiver_firmware = _normalize_spaces(
        _extract(r"4\.1\.1.*?Versão do Firmware\s*-\s*(.+)", text, re.S)
    )
    receiver_fw_update_text = _normalize_spaces(
        _extract(r"4\.1\.1.*?Atualização do Firmware\s*-\s*(.+)", text, re.S)
    )

    antenna_model = _normalize_spaces(
        _extract(r"4\.2\.1\s*Tipo de Antena\s*-\s*(.+)", text)
    )
    antenna_serial = _normalize_spaces(
        _extract(r"4\.2\.1.*?Número de Série\s*-\s*(.+)", text, re.S)
    )
    antenna_height_text = _extract(
        r"4\.2\.1.*?Altura da Antena \(m\)\s*-\s*([-\d\.,]+)",
        text,
        re.S,
    )

    return {
        "station_code": station_code.upper() if station_code else None,
        "station_name": station_name,
        "sat_code": sat_code,
        "international_code": international_code,
        "city": city,
        "state": state.upper()[:2] if state else None,
        "latitude": _parse_coord_dms_to_decimal(lat_text),
        "longitude": _parse_coord_dms_to_decimal(lon_text),
        "ellipsoidal_height": _parse_br_float(h_text),
        "x": _parse_br_float(x_text),
        "y": _parse_br_float(y_text),
        "z": _parse_br_float(z_text),
        "utm_n": _parse_br_float(utm_n_text),
        "utm_e": _parse_br_float(utm_e_text),
        "mc": _parse_int_safe(mc_text),
        "receiver_model": (
            receiver_model.splitlines()[0].strip() if receiver_model else None
        ),
        "receiver_serial": (
            receiver_serial.splitlines()[0].strip() if receiver_serial else None
        ),
        "receiver_firmware": (
            receiver_firmware.splitlines()[0].strip() if receiver_firmware else None
        ),
        "last_firmware_update": _parse_datetime_pt(receiver_fw_update_text),
        "antenna_model": (
            antenna_model.splitlines()[0].strip() if antenna_model else None
        ),
        "antenna_serial": (
            antenna_serial.splitlines()[0].strip() if antenna_serial else None
        ),
        "antenna_height": _parse_br_float(antenna_height_text),
        "report_reference_date": None,
    }
