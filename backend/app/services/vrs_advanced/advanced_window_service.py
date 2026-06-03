from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone

LOCAL_TIMEZONE = "America/Belem"
LOCAL_UTC_OFFSET_HOURS = -3
LOCAL_TZ = timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))


def _parse_datetime_local(session_date: str, time_text: str) -> datetime:
    raw = str(time_text or "").strip()
    parts = raw.split(":")
    if len(parts) != 3:
        raise ValueError("No modo avançado, use HH:MM:SS.")
    hh, mm, ss = parts
    return datetime.fromisoformat(
        f"{session_date}T{hh.zfill(2)}:{mm.zfill(2)}:{ss.zfill(2)}"
    )


def build_advanced_requested_window(job) -> dict:
    """
    Interpreta session_date/start_time/end_time como horário local de Belém/Macapá (UTC-3)
    e converte para UTC para uso interno do pipeline.
    """
    session_date = (getattr(job, "session_date", None) or "").strip()
    start_time = (getattr(job, "start_time", None) or "").strip()
    end_time = (getattr(job, "end_time", None) or "").strip()

    if not session_date:
        raise ValueError("session_date não informado no job.")
    if not start_time:
        raise ValueError("start_time não informado no job.")
    if not end_time:
        raise ValueError("end_time não informado no job.")

    try:
        start_local = datetime.strptime(
            f"{session_date} {start_time}",
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=LOCAL_TZ)

        end_local = datetime.strptime(
            f"{session_date} {end_time}",
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=LOCAL_TZ)
    except Exception as exc:
        raise ValueError(
            f"Janela temporal inválida. session_date={session_date}, "
            f"start_time={start_time}, end_time={end_time}"
        ) from exc

    if end_local <= start_local:
        raise ValueError("A hora final deve ser maior que a hora inicial.")

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    duration_seconds = int((end_utc - start_utc).total_seconds())

    return {
        "timezone_name": "America/Belem",
        "timezone_offset_hours": LOCAL_UTC_OFFSET_HOURS,
        "start_local": start_local,
        "end_local": end_local,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "duration_seconds": duration_seconds,
    }


def build_target_time_grid(
    start_utc: datetime,
    end_utc: datetime,
    step_seconds: int,
) -> list[datetime]:
    if step_seconds <= 0:
        raise ValueError("step_seconds deve ser maior que zero.")

    out: list[datetime] = []
    current = start_utc
    delta = timedelta(seconds=step_seconds)

    while current <= end_utc:
        out.append(current)
        current += delta

    return out
