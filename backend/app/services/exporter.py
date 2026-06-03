from __future__ import annotations

from pathlib import Path
import json


def export_diagnostics(output_dir: str, diagnostics: dict) -> str:
    path = Path(output_dir) / "vrs_engine_diagnostics.json"
    path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return str(path)
