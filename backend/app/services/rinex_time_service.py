from __future__ import annotations

from datetime import datetime
import re


def parse_rinex_epoch_text(line: str) -> datetime | None:
    if not line:
        return None

    text = line.strip()
    if not text:
        return None

    # RINEX 3
    # Ex.: > 2026 04 12 12 00  0.0000000  0 18
    if text.startswith(">"):
        m = re.match(
            r"^>\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([\d\.]+)",
            text,
        )
        if m:
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            hour = int(m.group(4))
            minute = int(m.group(5))
            sec_float = float(m.group(6))
            second = int(sec_float)
            microsecond = int(round((sec_float - second) * 1_000_000))
            return datetime(year, month, day, hour, minute, second, microsecond)

    # RINEX 2 ou compatível
    # Exemplos:
    # 26  4 12 23 59 45.0000000
    # 2026  4 12 23 59 45.0000000
    m = re.match(
        r"^\s*(\d{2,4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([\d\.]+)",
        text,
    )
    if m:
        raw_year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        hour = int(m.group(4))
        minute = int(m.group(5))
        sec_float = float(m.group(6))
        second = int(sec_float)
        microsecond = int(round((sec_float - second) * 1_000_000))

        if raw_year < 100:
            year = 2000 + raw_year if raw_year < 80 else 1900 + raw_year
        else:
            year = raw_year

        return datetime(year, month, day, hour, minute, second, microsecond)

    return None
