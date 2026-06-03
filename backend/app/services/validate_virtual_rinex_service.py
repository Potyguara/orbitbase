from __future__ import annotations

from pathlib import Path
import json
import re


def _read_lines(file_path: str) -> list[str]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _split_header(lines: list[str]) -> tuple[list[str], list[str]]:
    header = []
    data = []
    found_end = False

    for line in lines:
        if not found_end:
            header.append(line)
            if "END OF HEADER" in line:
                found_end = True
        else:
            data.append(line)

    return header, data


def _find_header_line(header: list[str], label: str) -> str | None:
    for line in header:
        if label in line:
            return line
    return None


def _parse_rinex_version(header: list[str]) -> float | None:
    line = _find_header_line(header, "RINEX VERSION / TYPE")
    if not line:
        return None

    raw = line[:20].strip()
    try:
        return float(raw.split()[0])
    except Exception:
        return None


def _parse_obs_types_v2(header: list[str]) -> list[str]:
    obs_types: list[str] = []

    for line in header:
        if "# / TYPES OF OBSERV" not in line:
            continue

        left = line[:60]
        tokens = left.split()

        if not tokens:
            continue

        if tokens[0].isdigit():
            tokens = tokens[1:]

        for token in tokens:
            token = token.strip()
            if len(token) == 2:
                obs_types.append(token)

    # manter ordem e remover duplicadas
    cleaned = []
    seen = set()
    for obs in obs_types:
        if obs not in seen:
            seen.add(obs)
            cleaned.append(obs)

    return cleaned


def _parse_interval(header: list[str]) -> float | None:
    line = _find_header_line(header, "INTERVAL")
    if not line:
        return None

    raw = line[:20].strip()
    try:
        return float(raw)
    except Exception:
        return None


def _count_epoch_lines_v2(data_lines: list[str]) -> int:
    count = 0

    for line in data_lines:
        if len(line) < 32:
            continue

        prefix = line[:32]

        # época RINEX 2 costuma começar com ano 2 dígitos na coluna inicial
        if re.match(r"^\s*\d{2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}", prefix):
            count += 1

    return count


def _extract_epoch_satellite_counts_v2(data_lines: list[str]) -> list[int]:
    counts = []

    for line in data_lines:
        if len(line) < 32:
            continue

        prefix = line[:32]
        if not re.match(r"^\s*\d{2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}", prefix):
            continue

        try:
            # colunas típicas RINEX 2: flag e número de satélites no fim do cabeçalho da época
            sat_count_str = line[29:32].strip()
            if sat_count_str:
                counts.append(int(sat_count_str))
        except Exception:
            continue

    return counts


def _detect_non_empty_observation_records(data_lines: list[str]) -> int:
    count = 0

    for line in data_lines:
        if not line.strip():
            continue

        # ignora linhas de época
        if re.match(r"^\s*\d{2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}", line[:32]):
            continue

        # se existe algum número com decimal na linha, conta como observação não vazia
        if re.search(r"[- ]\d+\.\d+", line):
            count += 1

    return count


def _validate_ppp_v2_structure(
    header: list[str], data: list[str]
) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict = {}

    version = _parse_rinex_version(header)
    obs_types = _parse_obs_types_v2(header)
    interval = _parse_interval(header)

    stats["rinex_version"] = version
    stats["observation_types"] = obs_types
    stats["interval_seconds"] = interval

    if version is None:
        errors.append("Cabeçalho sem 'RINEX VERSION / TYPE'.")
    elif version < 2.0 or version >= 3.0:
        errors.append(
            f"Arquivo PPP inválido: versão RINEX {version} detectada. O PPP final deve ser 2.x."
        )

    if _find_header_line(header, "# / TYPES OF OBSERV") is None:
        errors.append("Cabeçalho sem '# / TYPES OF OBSERV'.")

    if not obs_types:
        errors.append("Não foi possível extrair os tipos de observação do cabeçalho.")

    if "C1" not in obs_types:
        errors.append("Arquivo PPP sem observação C1.")
    if "L1" not in obs_types:
        errors.append("Arquivo PPP sem observação L1.")
    if "L2" not in obs_types:
        errors.append("Arquivo PPP sem observação L2.")
    if "P2" not in obs_types and "C2" not in obs_types:
        errors.append("Arquivo PPP sem observação de segunda frequência (P2/C2).")

    if _find_header_line(header, "APPROX POSITION XYZ") is None:
        errors.append("Cabeçalho sem 'APPROX POSITION XYZ'.")

    if _find_header_line(header, "TIME OF FIRST OBS") is None:
        errors.append("Cabeçalho sem 'TIME OF FIRST OBS'.")

    if _find_header_line(header, "TIME OF LAST OBS") is None:
        errors.append("Cabeçalho sem 'TIME OF LAST OBS'.")

    if interval is None or interval <= 0:
        warnings.append("INTERVAL ausente ou inválido.")

    epoch_count = _count_epoch_lines_v2(data)
    satellite_counts = _extract_epoch_satellite_counts_v2(data)
    non_empty_obs_lines = _detect_non_empty_observation_records(data)

    stats["epoch_count"] = epoch_count
    stats["satellite_counts"] = satellite_counts[:50]
    stats["avg_satellites_per_epoch"] = (
        sum(satellite_counts) / len(satellite_counts) if satellite_counts else 0.0
    )
    stats["non_empty_observation_lines"] = non_empty_obs_lines

    if epoch_count <= 0:
        errors.append("Nenhuma época válida foi detectada no corpo do arquivo.")

    if non_empty_obs_lines <= 0:
        errors.append(
            "Nenhuma linha de observação válida foi detectada no corpo do arquivo."
        )

    if satellite_counts and max(satellite_counts) < 4:
        warnings.append("Poucos satélites por época para um arquivo PPP robusto.")

    if epoch_count and epoch_count < 20:
        warnings.append("Poucas épocas disponíveis no arquivo PPP.")

    return errors, warnings, stats


def validate_virtual_observations(file_or_seed) -> dict:
    """
    Aceita:
    - caminho de arquivo RINEX final PPP (.obs)
    - ou seed dict (compatibilidade)
    """
    if isinstance(file_or_seed, dict):
        return {
            "status": "warning",
            "errors": [],
            "warnings": [
                "Validação recebeu seed em memória. Para PPP final, valide o arquivo .obs gerado."
            ],
            "recommendations": [
                "Executar a validação sobre o arquivo RINEX final PPP (BASE.obs)."
            ],
            "stats": {"mode": "seed_only"},
        }

    file_path = str(file_or_seed)
    lines = _read_lines(file_path)
    header, data = _split_header(lines)

    errors, warnings, stats = _validate_ppp_v2_structure(header, data)

    recommendations = []

    if errors:
        recommendations.append(
            "Gerar novamente o arquivo PPP final em RINEX 2.11 GPS-only."
        )
        recommendations.append("Não usar 'virtual_base_mixed.obs' no PPP do IBGE.")
        recommendations.append(
            "Garantir presença de C1, L1, L2 e P2/C2 no cabeçalho '# / TYPES OF OBSERV'."
        )

    if not errors and warnings:
        recommendations.append(
            "Arquivo estruturalmente aceito, mas convém revisar densidade de épocas e número de satélites."
        )

    status = "failed" if errors else ("warning" if warnings else "passed")

    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "recommendations": recommendations,
        "stats": stats,
        "validated_file": file_path,
    }


def write_virtual_validation_report(output_path: str | Path, report: dict) -> str:
    path = Path(output_path)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)
