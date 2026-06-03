from pathlib import Path
import re

from app.services.hatanaka_service import convert_hatanaka_to_rinex


def detect_observation_file(file_names: list[str]) -> str | None:
    """
    Detecta arquivo de observação GNSS dentro de uma lista de nomes.
    Suporta:
    - .obs / .OBS
    - .o / .O
    - .YYo
    - .YYd (Hatanaka)
    - .rnx
    """
    if not file_names:
        return None

    candidates = []

    for raw_name in file_names:
        name = Path(raw_name).name
        lower = name.lower()

        if lower.endswith(".obs") or lower.endswith(".rnx"):
            candidates.append(name)
            continue

        if lower.endswith(".o"):
            candidates.append(name)
            continue

        if re.search(r"\.\d{2}o$", lower):
            candidates.append(name)
            continue

        if re.search(r"\.\d{2}d$", lower):
            candidates.append(name)
            continue

    if not candidates:
        return None

    def score(filename: str) -> tuple[int, str]:
        lower = filename.lower()

        if lower.endswith(".obs"):
            return (1, lower)
        if re.search(r"\.\d{2}o$", lower):
            return (2, lower)
        if lower.endswith(".o"):
            return (3, lower)
        if lower.endswith(".rnx"):
            return (4, lower)
        if re.search(r"\.\d{2}d$", lower):
            return (5, lower)

        return (99, lower)

    candidates.sort(key=score)
    return candidates[0]


def _extract_epoch_from_line(line: str) -> str | None:
    stripped = line.rstrip("\n")

    if not stripped.strip():
        return None

    # RINEX 3+
    if stripped.lstrip().startswith(">"):
        parts = stripped.lstrip()[1:].split()
        if len(parts) >= 6:
            return " ".join(parts[:6])
        return None

    # RINEX 2.xx
    match = re.match(
        r"^\s*(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d+(?:\.\d+)?)",
        stripped,
    )
    if match:
        return " ".join(match.groups())

    return None


def parse_rinex_observation_metadata(file_path: str) -> dict:
    original_path = Path(file_path)
    if not original_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    if not original_path.is_file():
        raise ValueError(f"Caminho inválido para arquivo: {file_path}")

    parsed_path = Path(convert_hatanaka_to_rinex(str(original_path)))

    print(f"[RINEX] Arquivo original: {original_path}")
    print(f"[RINEX] Arquivo usado na leitura: {parsed_path}")

    marker_name = None
    rinex_version = None
    approx_position = None
    interval_seconds = None
    first_epoch = None
    last_epoch = None

    with parsed_path.open("r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    print(f"[RINEX] Total de linhas lidas: {len(lines)}")

    if not lines:
        raise ValueError("Arquivo vazio.")

    header_ended = False
    data_lines = []

    for line in lines:
        if "RINEX VERSION / TYPE" in line:
            rinex_version = line[:20].strip()

        if "MARKER NAME" in line:
            marker_name = line[:60].strip()

        if "APPROX POSITION XYZ" in line:
            approx_position = line[:60].strip()

        if "INTERVAL" in line:
            raw = line[:20].strip()
            try:
                interval_seconds = float(raw.split()[0])
            except Exception:
                interval_seconds = None

        if "TIME OF FIRST OBS" in line:
            first_epoch = line[:60].strip()

        if "TIME OF LAST OBS" in line:
            last_epoch = line[:60].strip()

        if "END OF HEADER" in line:
            header_ended = True
            continue

        if header_ended and line.strip():
            data_lines.append(line)

    if not header_ended:
        raise ValueError("Cabeçalho RINEX não encontrado corretamente.")

    if not first_epoch:
        for line in data_lines:
            epoch = _extract_epoch_from_line(line)
            if epoch:
                first_epoch = epoch
                break

    if not last_epoch:
        for line in reversed(data_lines):
            epoch = _extract_epoch_from_line(line)
            if epoch:
                last_epoch = epoch
                break

    print(f"[RINEX] first_epoch_text: {first_epoch}")
    print(f"[RINEX] last_epoch_text: {last_epoch}")

    return {
        "marker_name": marker_name,
        "rinex_version": rinex_version,
        "approx_position_text": approx_position,
        "interval_seconds": interval_seconds,
        "first_epoch_text": first_epoch,
        "last_epoch_text": last_epoch,
        "line_count": len(lines),
        "data_line_count": len(data_lines),
        "parsed_file_path": str(parsed_path),
    }


from pathlib import Path


def detect_observation_file_from_job(job) -> str | None:
    """
    Compatibilidade com fluxos antigos.

    No modo atual network_only, o job pode não ter rover.
    Então esta função tenta localizar um arquivo de observação do rover
    apenas se esses campos existirem; caso contrário, retorna None.
    """

    candidates = [
        getattr(job, "rover_uploaded_file_path", None),
        getattr(job, "uploaded_file_path", None),
        getattr(job, "observation_file_path", None),
    ]

    for candidate in candidates:
        if candidate and Path(candidate).exists() and Path(candidate).is_file():
            return str(candidate)

    return None
