from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

from app.services.rinex_time_service import parse_rinex_epoch_text


def _is_epoch_line(line: str) -> bool:
    if not line:
        return False

    stripped = line.lstrip()
    if not stripped:
        return False

    if stripped.startswith(">"):
        return True

    first_token = stripped.split(maxsplit=1)[0] if stripped.split() else ""
    if first_token.isdigit() and len(first_token) in (2, 4):
        return True

    return False


def try_parse_epoch_from_line(line: str) -> datetime | None:
    if not _is_epoch_line(line):
        return None
    return parse_rinex_epoch_text(line)


def summarize_epochs(
    file_path: str,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    started = time.perf_counter()
    print(f"[EPOCHS] Iniciando resumo: {file_path}", flush=True)

    total_lines = 0
    epoch_lines = 0
    epoch_count_in_window = 0
    first_epoch_any = None
    last_epoch_any = None
    first_epoch_in_window = None
    last_epoch_in_window = None

    in_header = True

    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            total_lines += 1

            if total_lines % 100000 == 0:
                print(
                    f"[EPOCHS] {path.name}: {total_lines} linhas lidas | "
                    f"épocas detectadas={epoch_lines} | "
                    f"épocas na janela={epoch_count_in_window}",
                    flush=True,
                )

            if in_header:
                if "END OF HEADER" in line:
                    in_header = False
                continue

            epoch_dt = try_parse_epoch_from_line(line)
            if epoch_dt is None:
                continue

            epoch_lines += 1

            if first_epoch_any is None:
                first_epoch_any = epoch_dt
            last_epoch_any = epoch_dt

            if start_dt is not None and epoch_dt < start_dt:
                continue

            if end_dt is not None and epoch_dt > end_dt:
                break

            epoch_count_in_window += 1

            if first_epoch_in_window is None:
                first_epoch_in_window = epoch_dt
            last_epoch_in_window = epoch_dt

    elapsed = time.perf_counter() - started

    print(
        f"[EPOCHS] Concluído: {path.name} em {elapsed:.2f}s | "
        f"linhas={total_lines} | épocas={epoch_lines} | "
        f"na janela={epoch_count_in_window}",
        flush=True,
    )

    return {
        "file_path": file_path,
        "total_lines_read": total_lines,
        "epoch_lines_detected": epoch_lines,
        "epoch_count_in_window": epoch_count_in_window,
        "first_epoch_any": first_epoch_any.isoformat() if first_epoch_any else None,
        "last_epoch_any": last_epoch_any.isoformat() if last_epoch_any else None,
        "first_epoch_in_window": (
            first_epoch_in_window.isoformat() if first_epoch_in_window else None
        ),
        "last_epoch_in_window": (
            last_epoch_in_window.isoformat() if last_epoch_in_window else None
        ),
        "window_start": start_dt.isoformat() if start_dt else None,
        "window_end": end_dt.isoformat() if end_dt else None,
        "elapsed_seconds": round(elapsed, 2),
    }
