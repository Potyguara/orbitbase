from __future__ import annotations

from pathlib import Path

from app.services.rinex_nav_satpos_service import (
    detect_navigation_file_near_observation,
)


def _looks_like_nav_file(path: Path) -> bool:
    if not path or not path.exists() or not path.is_file():
        return False

    name = path.name.lower()

    return (
        name.startswith("brdc")
        or name.endswith(".nav")
        or name.endswith(".rnx")
        or name.endswith(".n")
        or name.endswith(".g")
        or name.endswith(".p")
        or name.endswith(".26n")
        or name.endswith(".25n")
        or name.endswith(".24n")
        or name.endswith(".23n")
        or name.endswith(".22n")
        or name.endswith(".21n")
        or name.endswith(".26g")
        or name.endswith(".25g")
        or name.endswith(".24g")
        or name.endswith(".23g")
        or name.endswith(".22g")
        or name.endswith(".21g")
        or name.endswith(".26p")
        or name.endswith(".25p")
        or name.endswith(".24p")
        or name.endswith(".23p")
        or name.endswith(".22p")
        or name.endswith(".21p")
    )


def _candidate_search_dirs(obs_file: Path) -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()

    raw_dirs = [
        obs_file.parent,
        obs_file.parent.parent if obs_file.parent != obs_file.parent.parent else None,
        (
            obs_file.parent.parent.parent
            if obs_file.parent.parent != obs_file.parent.parent.parent
            else None
        ),
    ]

    for d in raw_dirs:
        if d is None:
            continue

        try:
            resolved = d.resolve()
        except Exception:
            resolved = d

        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)

        if resolved.exists() and resolved.is_dir():
            dirs.append(resolved)

    return dirs


def _find_nav_by_glob(obs_path: str | None) -> str | None:
    if not obs_path:
        return None

    obs_file = Path(str(obs_path))
    if not obs_file.exists():
        return None

    patterns = [
        "brdc*",
        "*.nav",
        "*.NAV",
        "*.rnx",
        "*.RNX",
        "*.n",
        "*.N",
        "*.g",
        "*.G",
        "*.p",
        "*.P",
        "*.26n",
        "*.25n",
        "*.24n",
        "*.23n",
        "*.22n",
        "*.21n",
        "*.26N",
        "*.25N",
        "*.24N",
        "*.23N",
        "*.22N",
        "*.21N",
        "*.26g",
        "*.25g",
        "*.24g",
        "*.23g",
        "*.22g",
        "*.21g",
        "*.26p",
        "*.25p",
        "*.24p",
        "*.23p",
        "*.22p",
        "*.21p",
    ]

    for d in _candidate_search_dirs(obs_file):
        for pattern in patterns:
            for m in sorted(d.glob(pattern)):
                if _looks_like_nav_file(m):
                    return str(m)

    return None


def _resolve_nav_from_any_known_path(item: dict) -> str | None:
    possible_obs_keys = [
        "path",
        "obs_path",
        "rinex_path",
        "copied_path",
        "original_path",
    ]

    for key in possible_obs_keys:
        value = item.get(key)
        if not value:
            continue

        nav_path = detect_navigation_file_near_observation(str(value))
        if nav_path and _looks_like_nav_file(Path(nav_path)):
            return str(Path(nav_path))

    for key in possible_obs_keys:
        value = item.get(key)
        if not value:
            continue

        nav_path = _find_nav_by_glob(str(value))
        if nav_path and _looks_like_nav_file(Path(nav_path)):
            return str(Path(nav_path))

    return None


def resolve_advanced_navigation_files(candidate_paths: list[dict]) -> list[dict]:
    out = []

    for item in candidate_paths or []:
        station_code = item.get("station_code")

        nav_path = _resolve_nav_from_any_known_path(item)

        obs_path = (
            item.get("path")
            or item.get("obs_path")
            or item.get("rinex_path")
            or item.get("copied_path")
            or item.get("original_path")
        )

        out.append(
            {
                "station_code": station_code,
                "obs_path": str(obs_path) if obs_path else None,
                "nav_path": nav_path,
                "status": "resolved" if nav_path else "pending_resolution",
            }
        )

    print(f"[ADV-NAV] resolved={out}", flush=True)
    return out
