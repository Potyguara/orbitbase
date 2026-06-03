from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass
class RinexHeaderInfo:
    version: str | None
    is_v2: bool
    obs_types: list[str]
    approx_position_line: str | None
    time_first_obs_line: str | None
    time_last_obs_line: str | None
    end_header_index: int


def _safe_float(value: str) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _parse_rinex_v2_epoch_line(line: str) -> tuple[datetime | None, int | None]:
    """
    Parse da linha de época RINEX 2.x.

    Exemplo típico:
     25  8 17 12 14 11.0000000  0 20G 5G 6G 7G...

    Retorna:
    - datetime sem timezone
    - quantidade de satélites indicada na linha
    """
    if not line or len(line) < 32:
        return None, None

    if line.startswith(">"):
        return None, None

    yy = _safe_float(line[1:3].strip())
    mm = _safe_float(line[4:6].strip())
    dd = _safe_float(line[7:9].strip())
    hh = _safe_float(line[10:12].strip())
    mi = _safe_float(line[13:15].strip())
    ss = _safe_float(line[15:26].strip())

    if None in {yy, mm, dd, hh, mi, ss}:
        return None, None

    yy_i = int(yy)
    year = 2000 + yy_i if yy_i < 80 else 1900 + yy_i

    sec_int = int(ss)
    micro = int(round((ss - sec_int) * 1_000_000))

    try:
        epoch_dt = datetime(
            year,
            int(mm),
            int(dd),
            int(hh),
            int(mi),
            sec_int,
            micro,
        )
    except Exception:
        return None, None

    sat_count = None
    try:
        sat_count = int(line[29:32].strip())
    except Exception:
        sat_count = None

    return epoch_dt, sat_count


def _is_rinex_v2_epoch_line(line: str) -> bool:
    dt, sat_count = _parse_rinex_v2_epoch_line(line)
    return dt is not None and sat_count is not None


def _count_header_satellite_list_lines(sat_count: int) -> int:
    """
    Em RINEX 2.x, a linha de época pode carregar até 12 PRNs na própria linha.
    Se houver mais, as demais continuam em linhas seguintes, 12 por linha.
    """
    if sat_count <= 12:
        return 0

    remaining = sat_count - 12
    extra = remaining // 12
    if remaining % 12:
        extra += 1
    return extra


def _count_observation_lines_per_satellite(obs_type_count: int) -> int:
    """
    RINEX 2.x: até 5 observações por linha por satélite.
    """
    if obs_type_count <= 0:
        return 1

    lines = obs_type_count // 5
    if obs_type_count % 5:
        lines += 1
    return max(lines, 1)


def _extract_header_info(lines: list[str]) -> RinexHeaderInfo:
    version = None
    obs_types: list[str] = []
    approx_position_line = None
    time_first_obs_line = None
    time_last_obs_line = None
    end_header_index = -1

    i = 0
    while i < len(lines):
        line = lines[i]
        label = line[60:80].strip() if len(line) >= 60 else ""

        if "RINEX VERSION / TYPE" == label:
            version = line[:20].strip() or None

        elif "# / TYPES OF OBSERV" == label:
            try:
                total_count = int(line[:6].strip())
            except Exception:
                total_count = 0

            current_types = []
            current_types.extend([line[j : j + 6].strip() for j in range(6, 60, 6)])
            current_types = [x for x in current_types if x]

            while len(current_types) < total_count and i + 1 < len(lines):
                nxt = lines[i + 1]
                nxt_label = nxt[60:80].strip() if len(nxt) >= 60 else ""
                if nxt_label != "# / TYPES OF OBSERV":
                    break
                i += 1
                current_types.extend([nxt[j : j + 6].strip() for j in range(6, 60, 6)])
                current_types = [x for x in current_types if x]

            obs_types = current_types[:total_count]

        elif "APPROX POSITION XYZ" == label:
            approx_position_line = line

        elif "TIME OF FIRST OBS" == label:
            time_first_obs_line = line

        elif "TIME OF LAST OBS" == label:
            time_last_obs_line = line

        elif "END OF HEADER" == label:
            end_header_index = i
            break

        i += 1

    if end_header_index < 0:
        raise ValueError("Cabeçalho RINEX inválido: END OF HEADER não encontrado.")

    is_v2 = False
    if version:
        is_v2 = version.startswith("2")

    return RinexHeaderInfo(
        version=version,
        is_v2=is_v2,
        obs_types=obs_types,
        approx_position_line=approx_position_line,
        time_first_obs_line=time_first_obs_line,
        time_last_obs_line=time_last_obs_line,
        end_header_index=end_header_index,
    )


def _format_rinex_v2_time_line(epoch: datetime, label: str) -> str:
    content = (
        f"{epoch.year:6d}"
        f"{epoch.month:6d}"
        f"{epoch.day:6d}"
        f"{epoch.hour:6d}"
        f"{epoch.minute:6d}"
        f"{epoch.second + epoch.microsecond / 1_000_000:13.7f}"
        f"{'GPS':>8}"
    )
    return f"{content[:60]:<60}{label:<20}"


def _replace_header_times(
    header_lines: list[str],
    first_epoch: datetime,
    last_epoch: datetime,
) -> list[str]:
    updated: list[str] = []

    has_first = False
    has_last = False

    for line in header_lines:
        label = line[60:80].strip() if len(line) >= 60 else ""

        if label == "TIME OF FIRST OBS":
            updated.append(_format_rinex_v2_time_line(first_epoch, "TIME OF FIRST OBS"))
            has_first = True
        elif label == "TIME OF LAST OBS":
            updated.append(_format_rinex_v2_time_line(last_epoch, "TIME OF LAST OBS"))
            has_last = True
        else:
            updated.append(line)

    if not has_first or not has_last:
        final_lines: list[str] = []
        inserted = False
        for line in updated:
            label = line[60:80].strip() if len(line) >= 60 else ""
            if label == "END OF HEADER" and not inserted:
                if not has_first:
                    final_lines.append(
                        _format_rinex_v2_time_line(first_epoch, "TIME OF FIRST OBS")
                    )
                if not has_last:
                    final_lines.append(
                        _format_rinex_v2_time_line(last_epoch, "TIME OF LAST OBS")
                    )
                inserted = True
            final_lines.append(line)
        updated = final_lines

    return updated


def _filter_gps_only_epoch_block(
    epoch_lines: list[str],
    obs_lines: list[str],
    obs_type_count: int,
) -> tuple[list[str], list[str]] | None:
    """
    Recebe um bloco de uma época RINEX 2.x e devolve o mesmo bloco filtrado para GPS-only.
    Mantém ordem original dos satélites GPS.

    epoch_lines:
      linha de época + possíveis linhas extras de lista de satélites
    obs_lines:
      linhas de observação de todos os satélites da época
    """
    if not epoch_lines:
        return None

    first_epoch_line = epoch_lines[0]
    _, sat_count = _parse_rinex_v2_epoch_line(first_epoch_line)
    if sat_count is None or sat_count <= 0:
        return None

    obs_lines_per_sat = _count_observation_lines_per_satellite(obs_type_count)

    full_sat_text = first_epoch_line[32:].rstrip("\n")
    for extra in epoch_lines[1:]:
        full_sat_text += extra.rstrip("\n")

    prns = [full_sat_text[i : i + 3] for i in range(0, len(full_sat_text), 3)]
    prns = [p for p in prns if p.strip()]
    prns = prns[:sat_count]

    if not prns:
        return None

    gps_indexes: list[int] = []
    gps_prns: list[str] = []

    for idx, prn in enumerate(prns):
        code = prn.strip()
        if code.startswith("G"):
            gps_indexes.append(idx)
            gps_prns.append(code[1:].rjust(2))
        elif code[:1].isdigit():
            # Em alguns RINEX 2.x GPS-only os PRNs podem vir só como número
            gps_indexes.append(idx)
            gps_prns.append(code.rjust(2))

    if not gps_prns:
        return None

    sat_obs_blocks: list[list[str]] = []
    cursor = 0
    for _ in range(sat_count):
        sat_block = obs_lines[cursor : cursor + obs_lines_per_sat]
        if len(sat_block) < obs_lines_per_sat:
            return None
        sat_obs_blocks.append(sat_block)
        cursor += obs_lines_per_sat

    filtered_obs_lines: list[str] = []
    for idx in gps_indexes:
        filtered_obs_lines.extend(sat_obs_blocks[idx])

    epoch_dt, _ = _parse_rinex_v2_epoch_line(first_epoch_line)
    if epoch_dt is None:
        return None

    rebuilt_epoch_lines = _build_rinex_v2_epoch_header_lines(epoch_dt, gps_prns)
    return rebuilt_epoch_lines, filtered_obs_lines


def _build_rinex_v2_epoch_header_lines(
    epoch_dt: datetime, gps_prns_2char: list[str]
) -> list[str]:
    sat_count = len(gps_prns_2char)
    yy = epoch_dt.year % 100
    sec = epoch_dt.second + epoch_dt.microsecond / 1_000_000

    first = (
        f" {yy:02d} {epoch_dt.month:02d} {epoch_dt.day:02d} "
        f"{epoch_dt.hour:02d} {epoch_dt.minute:02d}"
        f"{sec:11.7f}  0{sat_count:3d}"
    )

    lines = []
    first_chunk = gps_prns_2char[:12]
    lines.append(first + "".join(f"{prn:>3}" for prn in first_chunk))

    remaining = gps_prns_2char[12:]
    while remaining:
        chunk = remaining[:12]
        remaining = remaining[12:]
        lines.append(" " * 32 + "".join(f"{prn:>3}" for prn in chunk))

    return lines


def _iterate_rinex_v2_epoch_blocks(
    body_lines: list[str],
    obs_type_count: int,
) -> Iterable[tuple[datetime, list[str], list[str]]]:
    i = 0
    total = len(body_lines)

    while i < total:
        line = body_lines[i]

        if not _is_rinex_v2_epoch_line(line):
            i += 1
            continue

        epoch_dt, sat_count = _parse_rinex_v2_epoch_line(line)
        if epoch_dt is None or sat_count is None:
            i += 1
            continue

        extra_sat_lines = _count_header_satellite_list_lines(sat_count)
        epoch_lines = body_lines[i : i + 1 + extra_sat_lines]
        i += 1 + extra_sat_lines

        obs_lines_per_sat = _count_observation_lines_per_satellite(obs_type_count)
        obs_total_lines = sat_count * obs_lines_per_sat
        obs_lines = body_lines[i : i + obs_total_lines]
        i += obs_total_lines

        if len(obs_lines) < obs_total_lines:
            break

        yield epoch_dt, epoch_lines, obs_lines


def build_ppp_safe_rinex_from_reference_base(
    reference_rinex_path: str | Path,
    output_path: str | Path,
    start_utc: datetime,
    end_utc: datetime,
    marker_name: str | None = None,
) -> str:
    """
    Gera um RINEX final PPP-safe a partir de uma base real de referência.

    Estratégia:
    - lê o RINEX original;
    - exige RINEX 2.x;
    - recorta a janela temporal;
    - filtra para GPS-only;
    - preserva observações originais;
    - atualiza TIME OF FIRST OBS / TIME OF LAST OBS;
    - opcionalmente atualiza MARKER NAME.
    """
    src = Path(reference_rinex_path)
    dst = Path(output_path)

    if not src.exists() or not src.is_file():
        raise ValueError(f"Arquivo de referência não encontrado: {src}")

    raw_lines = src.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not raw_lines:
        raise ValueError("Arquivo RINEX vazio.")

    header = _extract_header_info(raw_lines)
    if not header.is_v2:
        raise ValueError("O arquivo de referência para PPP-safe precisa ser RINEX 2.x.")

    obs_type_count = len(header.obs_types)
    if obs_type_count <= 0:
        raise ValueError(
            "Cabeçalho inválido: '# / TYPES OF OBSERV' não encontrado ou vazio."
        )

    header_lines = raw_lines[: header.end_header_index + 1]
    body_lines = raw_lines[header.end_header_index + 1 :]

    kept_epoch_blocks: list[tuple[datetime, list[str], list[str]]] = []

    for epoch_dt, epoch_lines, obs_lines in _iterate_rinex_v2_epoch_blocks(
        body_lines,
        obs_type_count,
    ):
        if epoch_dt < start_utc or epoch_dt > end_utc:
            continue

        filtered = _filter_gps_only_epoch_block(
            epoch_lines=epoch_lines,
            obs_lines=obs_lines,
            obs_type_count=obs_type_count,
        )

        if filtered is None:
            continue

        filtered_epoch_lines, filtered_obs_lines = filtered
        kept_epoch_blocks.append((epoch_dt, filtered_epoch_lines, filtered_obs_lines))

    if not kept_epoch_blocks:
        raise ValueError(
            "Nenhuma época GPS válida foi encontrada na janela solicitada."
        )

    first_epoch = kept_epoch_blocks[0][0]
    last_epoch = kept_epoch_blocks[-1][0]

    updated_header = _replace_header_times(
        header_lines=header_lines,
        first_epoch=first_epoch,
        last_epoch=last_epoch,
    )

    if marker_name:
        patched_header: list[str] = []
        marker_done = False
        for line in updated_header:
            label = line[60:80].strip() if len(line) >= 60 else ""
            if label == "MARKER NAME":
                patched_header.append(f"{marker_name[:60]:<60}{'MARKER NAME':<20}")
                marker_done = True
            else:
                patched_header.append(line)

        if not marker_done:
            final_header: list[str] = []
            inserted = False
            for line in patched_header:
                label = line[60:80].strip() if len(line) >= 60 else ""
                if label == "END OF HEADER" and not inserted:
                    final_header.append(f"{marker_name[:60]:<60}{'MARKER NAME':<20}")
                    inserted = True
                final_header.append(line)
            patched_header = final_header

        updated_header = patched_header

    output_lines: list[str] = []
    output_lines.extend(updated_header)

    for _, epoch_lines, obs_lines in kept_epoch_blocks:
        output_lines.extend(epoch_lines)
        output_lines.extend(obs_lines)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    return str(dst)
