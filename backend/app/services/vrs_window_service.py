from __future__ import annotations

from datetime import datetime
import re

from app.services.rinex_service import parse_rinex_observation_metadata


def _normalize_year(year: int) -> int:
    if year < 100:
        return 2000 + year
    return year


def _safe_int(value: str) -> int:
    return int(str(value).strip())


def _safe_float(value: str) -> float:
    return float(str(value).strip())


def _parse_rinex_epoch_text(value: str | None) -> datetime | None:
    """
    Aceita formatos como:
    - 2026 4 12 0 0 0.0000000 GPS
    - 26 4 12 23 59 45.0000000
    - 2026 04 12 00 00 00.0000000
    - 26 04 12 23 59 45.0000000 GPS
    """
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    # remove sufixos GNSS comuns ao final da linha
    text = re.sub(r"\s+(GPS|GLO|GAL|BDS|QZS|IRNSS|SBAS)\s*$", "", text, flags=re.I)

    # normaliza múltiplos espaços
    text = re.sub(r"\s+", " ", text).strip()

    match = re.match(
        r"^(\d{2,4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d+(?:\.\d+)?)$",
        text,
    )
    if not match:
        return None

    year = _normalize_year(_safe_int(match.group(1)))
    month = _safe_int(match.group(2))
    day = _safe_int(match.group(3))
    hour = _safe_int(match.group(4))
    minute = _safe_int(match.group(5))
    second_float = _safe_float(match.group(6))

    second = int(second_float)
    microsecond = int(round((second_float - second) * 1_000_000))

    # proteção caso arredondamento leve a 1000000
    if microsecond >= 1_000_000:
        second += 1
        microsecond -= 1_000_000

    try:
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            microsecond,
        )
    except Exception:
        return None


def get_file_time_window(file_path: str) -> dict:
    metadata = parse_rinex_observation_metadata(file_path)

    first_epoch_text = metadata.get("first_epoch_text")
    last_epoch_text = metadata.get("last_epoch_text")

    start_dt = _parse_rinex_epoch_text(first_epoch_text)
    end_dt = _parse_rinex_epoch_text(last_epoch_text)

    if start_dt is None or end_dt is None:
        raise ValueError(
            "Não foi possível obter janela temporal do arquivo: "
            f"{file_path} | first_epoch_text={first_epoch_text!r} | "
            f"last_epoch_text={last_epoch_text!r}"
        )

    if end_dt <= start_dt:
        raise ValueError(
            "Janela temporal inválida no arquivo: "
            f"{file_path} | início={start_dt.isoformat()} | fim={end_dt.isoformat()}"
        )

    return {
        "file_path": file_path,
        "start": start_dt,
        "end": end_dt,
        "duration_seconds": int((end_dt - start_dt).total_seconds()),
    }


def get_common_time_window(file_paths: list[str]) -> dict:
    if not file_paths:
        raise ValueError("Nenhum arquivo foi informado para cálculo da janela comum.")

    windows = [get_file_time_window(path) for path in file_paths]

    common_start = max(item["start"] for item in windows)
    common_end = min(item["end"] for item in windows)

    if common_end <= common_start:
        raise ValueError("Não existe interseção temporal entre os arquivos informados.")

    return {
        "common_start": common_start,
        "common_end": common_end,
        "duration_seconds": int((common_end - common_start).total_seconds()),
        "files": windows,
    }


def validate_common_window(
    common_window: dict,
    requested_minutes: int | None = None,
) -> None:
    if not common_window:
        raise ValueError("Janela temporal comum não informada.")

    duration_seconds = int(common_window.get("duration_seconds", 0))
    if duration_seconds <= 0:
        raise ValueError("Janela temporal comum inválida.")

    if requested_minutes and duration_seconds < int(requested_minutes) * 60:
        raise ValueError("A janela temporal comum é menor que o tempo solicitado.")


def format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
