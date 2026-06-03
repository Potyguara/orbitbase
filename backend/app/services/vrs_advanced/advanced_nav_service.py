from __future__ import annotations


def resolve_advanced_navigation_files(candidate_bases: list[dict]) -> list[dict]:
    """
    Esqueleto para resolver arquivos de navegação e suporte orbital.
    """
    out = []
    for item in candidate_bases:
        out.append(
            {
                "station_code": item.get("station_code"),
                "obs_path": item.get("path"),
                "nav_path": None,
                "status": "pending_resolution",
            }
        )
    return out
