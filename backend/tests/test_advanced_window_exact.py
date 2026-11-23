from datetime import datetime, timedelta, timezone

from app.services.vrs_advanced.advanced_pipeline_service import (
    _base_extraction_window,
    _build_effective_time_grid,
)
from app.services.vrs_advanced.advanced_window_service import (
    build_advanced_requested_window,
    build_target_time_grid,
)


class _Job:
    session_date = "2026-08-20"
    start_time = "07:05:22"
    end_time = "13:26:38"


def _base(first, last):
    return {"epoch_count": 10, "first_epoch": first, "last_epoch": last}


def test_advanced_window_keeps_exact_seconds():
    window = build_advanced_requested_window(_Job())
    assert window["start_utc"] == datetime(2026, 8, 20, 10, 5, 22, tzinfo=timezone.utc)
    assert window["end_utc"] == datetime(2026, 8, 20, 16, 26, 38, tzinfo=timezone.utc)

    grid = build_target_time_grid(window["start_utc"], window["end_utc"], 1)

    # Bases RBMC a cada 30 s, lidas com a margem: a primeira época fica antes de 10:05:22.
    base_start, base_end = _base_extraction_window(window)
    first = base_start.replace(second=(base_start.second // 30) * 30) + timedelta(seconds=30)
    last = base_end.replace(second=(base_end.second // 30) * 30)
    effective, summary = _build_effective_time_grid(grid, [_base(first, last)] * 3)

    assert effective[0] == window["start_utc"]
    assert effective[-1] == window["end_utc"]
    assert len(effective) == len(grid) == 22877


def test_without_padding_grid_would_be_clipped():
    window = build_advanced_requested_window(_Job())
    grid = build_target_time_grid(window["start_utc"], window["end_utc"], 1)
    clipped, _ = _build_effective_time_grid(
        grid,
        [_base(datetime(2026, 8, 20, 10, 5, 30, tzinfo=timezone.utc),
               datetime(2026, 8, 20, 16, 26, 30, tzinfo=timezone.utc))],
    )
    assert len(clipped) == 22861  # o comportamento antigo observado no job 1
