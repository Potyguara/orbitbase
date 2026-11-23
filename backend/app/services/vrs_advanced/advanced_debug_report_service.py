from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_advanced_debug_report(output_path: str, payload: dict) -> str:
    from .advanced_json_io import dump_json_file

    return dump_json_file(output_path, payload)
