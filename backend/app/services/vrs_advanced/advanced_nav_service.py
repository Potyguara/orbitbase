"""
Descoberta dos arquivos de navegação broadcast (NAV) para o modo avançado.

Causa do erro do job 141: a observação usada pelo pipeline fica em
``storage/rbmc/<est>/<data>/converted`` (ou numa cópia em
``storage/processing/job_X_advanced/inputs``), enquanto o ``.26n`` fica em
``storage/rbmc/<est>/<data>/extracted``. A busca antiga só olhava a pasta da
observação e as pastas-pai (sem recursão) e descartava o ``best_navigation_file``
devolvido pelo sincronizador RBMC. Resultado: ``nav_path=None``.

Agora a resolução segue esta ordem:
1. caminhos NAV explícitos (``nav_path`` / ``nav_paths``) vindos do RBMC;
2. pasta diária RBMC da estação (``storage/rbmc/<est>/<data>``), recursiva;
3. vizinhança da observação (pasta, ``nav``, ``extracted``, pais);
4. diretórios extras informados (ex.: BRDC baixado em ``precise_products``).
"""

from __future__ import annotations

from pathlib import Path

from app.services.rinex_nav_satpos_service import (
    detect_navigation_file_near_observation,
    is_gps_navigation_filename,
    navigation_file_priority,
)

_OBS_KEYS = ("path", "obs_path", "rinex_path", "copied_path", "original_path")
_NAV_KEYS = (
    "nav_path",
    "navigation_file",
    "navigation_path",
    "best_navigation_file",
    "broadcast_nav_path",
    "brdc_path",
)
_NAV_LIST_KEYS = ("nav_paths", "navigation_files")


def _rbmc_storage_root() -> Path:
    return Path(__file__).resolve().parents[3] / "storage" / "rbmc"


def _is_usable_nav(path: Path) -> bool:
    try:
        return (
            path.exists()
            and path.is_file()
            and path.stat().st_size > 0
            and is_gps_navigation_filename(path.name)
        )
    except Exception:
        return False


def _rbmc_day_root_from_path(path: Path) -> Path | None:
    """
    Sobe na árvore até achar ``.../rbmc/<estacao>/<data>``.
    """
    try:
        parts = path.resolve().parts
    except Exception:
        parts = path.parts

    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx].lower() == "rbmc" and idx + 2 < len(parts):
            return Path(*parts[: idx + 3])
    return None


def _scan_dir_for_nav(directory: Path, recursive: bool) -> list[Path]:
    if not directory or not directory.exists() or not directory.is_dir():
        return []
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    found = []
    try:
        for item in iterator:
            if _is_usable_nav(item):
                found.append(item)
    except Exception:
        pass
    return found


def find_navigation_files_for_station(
    station_code: str | None,
    session_date: str | None,
    rbmc_root: Path | None = None,
) -> list[Path]:
    if not station_code or not session_date:
        return []
    root = (rbmc_root or _rbmc_storage_root()) / station_code.lower() / session_date
    return sorted(_scan_dir_for_nav(root, recursive=True), key=navigation_file_priority)


def collect_navigation_paths(
    item: dict,
    session_date: str | None = None,
    rbmc_root: Path | None = None,
    extra_search_dirs: list[Path] | None = None,
) -> list[Path]:
    """Todos os NAV GPS utilizáveis para um item de contexto, por prioridade."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(path_value) -> None:
        if not path_value:
            return
        path = Path(str(path_value))
        if not _is_usable_nav(path):
            return
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    for key in _NAV_KEYS:
        add(item.get(key))
    for key in _NAV_LIST_KEYS:
        for value in item.get(key) or []:
            add(value)

    for path in find_navigation_files_for_station(
        item.get("station_code"), session_date, rbmc_root
    ):
        add(path)

    for key in _OBS_KEYS:
        value = item.get(key)
        if not value:
            continue
        obs = Path(str(value))
        day_root = _rbmc_day_root_from_path(obs)
        if day_root is not None:
            for path in _scan_dir_for_nav(day_root, recursive=True):
                add(path)
        add(detect_navigation_file_near_observation(str(obs)))

    for directory in extra_search_dirs or []:
        for path in _scan_dir_for_nav(Path(directory), recursive=True):
            add(path)

    found.sort(key=navigation_file_priority)
    return found


def resolve_advanced_navigation_files(
    candidate_paths: list[dict],
    session_date: str | None = None,
    rbmc_root: Path | None = None,
    extra_search_dirs: list[Path] | None = None,
) -> list[dict]:
    """
    Um item por estação (itens repetidos da mesma estação são agregados).
    Cada item traz ``nav_path`` (melhor) e ``nav_paths`` (todos).
    """
    merged: dict[str, dict] = {}
    order: list[str] = []

    for item in candidate_paths or []:
        station = (item.get("station_code") or item.get("label") or "").upper() or (
            f"item{len(order)}"
        )
        if station not in merged:
            merged[station] = {"station_code": item.get("station_code")}
            order.append(station)
        target = merged[station]
        for key in _OBS_KEYS + _NAV_KEYS:
            if item.get(key) and not target.get(key):
                target[key] = item.get(key)
        for key in _NAV_LIST_KEYS:
            if item.get(key):
                target.setdefault("nav_paths", [])
                target["nav_paths"].extend(item.get(key))

    out = []
    for station in order:
        item = merged[station]
        nav_paths = collect_navigation_paths(
            item,
            session_date=session_date,
            rbmc_root=rbmc_root,
            extra_search_dirs=extra_search_dirs,
        )
        obs_path = next((item.get(k) for k in _OBS_KEYS if item.get(k)), None)
        out.append(
            {
                "station_code": item.get("station_code"),
                "obs_path": str(obs_path) if obs_path else None,
                "nav_path": str(nav_paths[0]) if nav_paths else None,
                "nav_paths": [str(p) for p in nav_paths],
                "status": "resolved" if nav_paths else "nav_not_found",
            }
        )

    print(
        "[ADV-NAV] resolved="
        + str([(o["station_code"], o["nav_path"], len(o["nav_paths"])) for o in out]),
        flush=True,
    )
    return out
