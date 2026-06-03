from __future__ import annotations
from pathlib import Path
from uuid import uuid4
from datetime import datetime
import json
import re
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User, VRSJob
from app.schemas import (
    ExtractedFileResponse,
    MessageResponse,
    NetworkValidationResponse,
    PreprocessResponse,
    ProcessResponse,
    UploadResponse,
    VRSJobCreate,
    VRSJobRead,
    VRSJobUpdate,
)
from app.services.billing import add_generated_minutes
from app.services.network_validation_service import validate_network
from app.services.rinex_service import (
    detect_observation_file_from_job,
    parse_rinex_observation_metadata,
)
from app.services.vrs_processing_service import process_real_vrs
from app.services.vrs_profile_service import (
    resolve_effective_profile_for_job,
    resolve_profile_for_processing,
)
from app.services.rbmc_observation_sync_service import (
    sync_rbmc_observations_for_job_date,
)


from zipfile import ZipFile, ZIP_DEFLATED
from fastapi import APIRouter, Depends, HTTPException

import shutil
import tempfile
from pydantic import BaseModel

router = APIRouter(prefix="/jobs", tags=["VRS Jobs"])
router = APIRouter(prefix="/jobs", tags=["jobs"])


BASE_DIR = Path(__file__).resolve().parents[2]
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
EXTRACT_DIR = STORAGE_DIR / "extracted"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXTRACT_DIR.mkdir(parents=True, exist_ok=True)


def _safe_export_name(value: str | None, default: str) -> str:
    if not value:
        return default
    cleaned = "".join(
        c if c.isalnum() or c in ("-", "", ".") else "" for c in value.strip()
    ).strip("._")
    return cleaned or default


def _job_processing_root(job: VRSJob) -> Path | None:
    candidates = []

    if job.solution_path:
        try:
            solution_path = Path(job.solution_path)
            if solution_path.exists():
                candidates.append(solution_path.parent)
        except Exception:
            pass

    if job.rinex_path:
        try:
            rinex_path = Path(job.rinex_path)
            if rinex_path.exists():
                candidates.append(rinex_path.parent)
        except Exception:
            pass

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def _collect_export_candidates(
    result_root: Path, job: VRSJob
) -> dict[str, Path | None]:
    output_base_name = _safe_export_name(
        getattr(job, "output_rinex_name", None),
        "BASE",
    )

    preferred_final = [
        result_root / f"{output_base_name}.obs",
        result_root / "BASE.obs",
        result_root / "virtual_base_final.obs",
        result_root / "virtual_base_clean.obs",
    ]

    preferred_mixed = [
        result_root / "virtual_base_mixed.obs",
        result_root / "virtual_base.obs",
    ]

    preferred_summary = [
        result_root / f"job_{job.id}_vrs_result.txt",
        result_root / "resumo_processamento.txt",
    ]

    preferred_readme = [
        result_root / "LEIA-ME.txt",
    ]

    preferred_manifest = [
        result_root / "manifest.json",
    ]

    preferred_meta = [
        result_root / "virtual_base_meta.json",
    ]

    preferred_validation = [
        result_root / "virtual_station_validation_report.json",
    ]

    preferred_sync = [
        result_root / "sync_summary.json",
    ]

    preferred_epoch_summary = [
        result_root / "epoch_observations_summary.json",
    ]

    preferred_blocks = [
        result_root / "synchronized_observation_blocks.json",
    ]

    preferred_numeric = [
        result_root / "numeric_observations_summary.json",
    ]

    preferred_virtual_seed = [
        result_root / "virtual_station_observations.json",
    ]

    preferred_virtual_sanitized = [
        result_root / "virtual_station_observations_sanitized.json",
    ]

    preferred_virtual_summary = [
        result_root / "virtual_station_numeric_summary.json",
    ]

    preferred_virtual_summary_sanitized = [
        result_root / "virtual_station_numeric_summary_sanitized.json",
    ]

    def first_existing(paths: list[Path]) -> Path | None:
        for path in paths:
            if path.exists() and path.is_file():
                return path
        return None

    return {
        "final_obs": first_existing(preferred_final),
        "mixed_obs": first_existing(preferred_mixed),
        "summary_txt": first_existing(preferred_summary),
        "readme_txt": first_existing(preferred_readme),
        "manifest_json": first_existing(preferred_manifest),
        "meta_json": first_existing(preferred_meta),
        "validation_json": first_existing(preferred_validation),
        "sync_json": first_existing(preferred_sync),
        "epoch_summary_json": first_existing(preferred_epoch_summary),
        "blocks_json": first_existing(preferred_blocks),
        "numeric_json": first_existing(preferred_numeric),
        "virtual_seed_json": first_existing(preferred_virtual_seed),
        "virtual_sanitized_json": first_existing(preferred_virtual_sanitized),
        "virtual_summary_json": first_existing(preferred_virtual_summary),
        "virtual_summary_sanitized_json": first_existing(
            preferred_virtual_summary_sanitized
        ),
    }


def _build_readme_text(job: VRSJob, files: dict[str, Path | None]) -> str:
    final_name = files["final_obs"].name if files["final_obs"] else "BASE.obs"
    mixed_name = (
        files["mixed_obs"].name if files["mixed_obs"] else "virtual_base_mixed.obs"
    )

    lines = [
        "ORBITBASE - PACOTE DE EXPORTAÇÃO VRS",
        "",
        f"Job ID: {job.id}",
        f"Nome do job: {job.name}",
        "",
        "ARQUIVO PRINCIPAL PARA O CLIENTE / PPP-IBGE:",
        f"- {final_name}",
        "",
        "ARQUIVO AUXILIAR TÉCNICO:",
        f"- {mixed_name}",
        "",
        "ARQUIVOS DE APOIO:",
        "- resumo_processamento.txt",
        "- manifest.json",
        "- virtual_base_meta.json",
        "- virtual_station_validation_report.json",
        "",
        "OBSERVAÇÃO:",
        "O arquivo principal do pacote é o RINEX final GPS-only preparado para uso no fluxo PPP/IBGE.",
    ]
    return "\n".join(lines) + "\n"


@router.get("/{job_id}/export")
def export_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    if job.processing_status != "completed":
        raise HTTPException(
            status_code=400,
            detail="O job ainda não foi processado com sucesso.",
        )

    result_root = _job_processing_root(job)
    if result_root is None or not result_root.exists():
        raise HTTPException(
            status_code=404,
            detail="Diretório de resultados do job não encontrado.",
        )

    files = _collect_export_candidates(result_root, job)

    if files["final_obs"] is None:
        raise HTTPException(
            status_code=404,
            detail="Arquivo final RINEX do job não foi encontrado para exportação.",
        )

    output_base_name = _safe_export_name(
        getattr(job, "output_rinex_name", None),
        "BASE",
    )
    zip_name = _safe_export_name(f"job{job.id}export.zip", f"job{job.id}_export.zip")

    temp_dir = Path(tempfile.mkdtemp(prefix=f"orbitbase_job_{job.id}_"))
    export_dir = temp_dir / f"job_{job.id}_export"
    export_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1) produto principal
        final_target_name = f"{output_base_name}.obs"
        shutil.copy2(files["final_obs"], export_dir / final_target_name)

        # 2) mixed auxiliar, se existir
        if files["mixed_obs"] is not None:
            shutil.copy2(files["mixed_obs"], export_dir / "virtual_base_mixed.obs")

        # 3) resumo
        if files["summary_txt"] is not None:
            shutil.copy2(files["summary_txt"], export_dir / "resumo_processamento.txt")

        # 4) readme
        readme_path = export_dir / "LEIA-ME.txt"
        if files["readme_txt"] is not None:
            shutil.copy2(files["readme_txt"], readme_path)
        else:
            readme_path.write_text(
                _build_readme_text(job, files),
                encoding="utf-8",
            )

        # 5) arquivos de apoio
        support_map = {
            "manifest.json": files["manifest_json"],
            "virtual_base_meta.json": files["meta_json"],
            "virtual_station_validation_report.json": files["validation_json"],
            "sync_summary.json": files["sync_json"],
            "epoch_observations_summary.json": files["epoch_summary_json"],
            "synchronized_observation_blocks.json": files["blocks_json"],
            "numeric_observations_summary.json": files["numeric_json"],
            "virtual_station_observations.json": files["virtual_seed_json"],
            "virtual_station_observations_sanitized.json": files[
                "virtual_sanitized_json"
            ],
            "virtual_station_numeric_summary.json": files["virtual_summary_json"],
            "virtual_station_numeric_summary_sanitized.json": files[
                "virtual_summary_sanitized_json"
            ],
        }

        for export_name, source_path in support_map.items():
            if source_path is not None and source_path.exists():
                shutil.copy2(source_path, export_dir / export_name)

        zip_path = temp_dir / zip_name
        with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
            # ordem proposital
            preferred_order = [
                final_target_name,
                "LEIA-ME.txt",
                "resumo_processamento.txt",
                "virtual_base_mixed.obs",
                "manifest.json",
                "virtual_base_meta.json",
                "virtual_station_validation_report.json",
                "sync_summary.json",
                "epoch_observations_summary.json",
                "synchronized_observation_blocks.json",
                "numeric_observations_summary.json",
                "virtual_station_observations.json",
                "virtual_station_observations_sanitized.json",
                "virtual_station_numeric_summary.json",
                "virtual_station_numeric_summary_sanitized.json",
            ]

            existing_names = {p.name for p in export_dir.iterdir() if p.is_file()}

            for name in preferred_order:
                if name in existing_names:
                    zf.write(export_dir / name, arcname=name)

            remaining = sorted(
                [
                    p.name
                    for p in export_dir.iterdir()
                    if p.is_file() and p.name not in preferred_order
                ]
            )
            for name in remaining:
                zf.write(export_dir / name, arcname=name)

        job.export_count = int(job.export_count or 0) + 1
        db.add(job)
        db.commit()

        return FileResponse(
            path=str(zip_path),
            filename=zip_name,
            media_type="application/zip",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Falha ao exportar job: {exc}",
        )


def _safe_job_root(job) -> Path | None:
    if getattr(job, "manifest_path", None):
        try:
            manifest_path = Path(job.manifest_path)
            if manifest_path.exists():
                return manifest_path.parent
        except Exception:
            pass

    if getattr(job, "solution_path", None):
        try:
            solution_path = Path(job.solution_path)
            if solution_path.exists():
                return solution_path.parent.parent
        except Exception:
            pass

    return None


def _first_existing_path(candidates: list[str | None]) -> Path | None:
    for item in candidates:
        if not item:
            continue
        try:
            path = Path(item)
            if path.exists() and path.is_file():
                return path
        except Exception:
            continue
    return None


def _resolve_primary_export_file(job) -> Path | None:
    return _first_existing_path(
        [
            getattr(job, "custom_final_rinex_path", None),
            getattr(job, "rinex_path", None),
            getattr(job, "solution_path", None),
        ]
    )


def _resolve_processing_outputs(job) -> dict[str, Path | None]:
    job_root = _safe_job_root(job)
    result_root = job_root / "results" if job_root else None

    def pick(attr_name: str, fallback_name: str | None = None) -> Path | None:
        direct = getattr(job, attr_name, None)
        direct_path = _first_existing_path([direct])
        if direct_path:
            return direct_path

        if result_root and fallback_name:
            candidate = result_root / fallback_name
            if candidate.exists() and candidate.is_file():
                return candidate

        return None

    return {
        "job_root": job_root,
        "result_root": result_root,
        "primary_rinex": pick("custom_final_rinex_path"),
        "clean_rinex": pick("virtual_rinex_clean_path", "virtual_base_final.obs"),
        "mixed_rinex": pick("virtual_rinex_mixed_path", "virtual_base_mixed.obs"),
        "raw_rinex": pick("virtual_rinex_path", "virtual_base_master_based.obs"),
        "ppp_aux_rinex": pick("ppp_aux_rinex_path", "base_ppp.obs"),
        "solution_txt": pick("solution_path"),
        "manifest": pick(
            "manifest_path", None if not result_root else "../manifest.json"
        ),
        "virtual_meta": pick("virtual_meta_path", "virtual_base_meta.json"),
        "virtual_validation": pick(
            "virtual_validation_path", "virtual_station_validation_report.json"
        ),
        "virtual_json": pick(
            "virtual_observations_json_path", "virtual_station_observations.json"
        ),
        "virtual_summary": pick(
            "virtual_numeric_summary_path", "virtual_station_numeric_summary.json"
        ),
        "sanitized_json": pick(
            "sanitized_virtual_json_path", "virtual_station_observations_sanitized.json"
        ),
        "sanitized_summary": pick(
            "sanitized_virtual_summary_path",
            "virtual_station_numeric_summary_sanitized.json",
        ),
        "sync_summary": pick("sync_summary_path", "sync_summary.json"),
        "numeric_summary": pick(
            "numeric_summary_path", "numeric_observations_summary.json"
        ),
        "observation_summary": pick(
            "observation_summary_path", "epoch_observations_summary.json"
        ),
        "observation_blocks": pick(
            "observation_blocks_path", "synchronized_observation_blocks.json"
        ),
    }


def _write_job_export_zip(job, outputs: dict[str, Path | None]) -> Path:
    job_root = outputs.get("job_root")
    if not job_root:
        raise ValueError("Não foi possível localizar a pasta do job para exportação.")

    export_dir = job_root / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    zip_path = export_dir / f"job_{job.id}_export.zip"

    primary_rinex = (
        outputs.get("primary_rinex")
        or outputs.get("clean_rinex")
        or outputs.get("mixed_rinex")
        or outputs.get("ppp_aux_rinex")
        or outputs.get("raw_rinex")
    )

    if primary_rinex is None:
        raise ValueError(
            "Nenhum arquivo principal de saída foi encontrado para exportação."
        )

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zipf:
        # Produto principal
        zipf.write(primary_rinex, arcname=primary_rinex.name)

        # Produtos auxiliares
        aux_map = {
            "virtual_base_final.obs": outputs.get("clean_rinex"),
            "virtual_base_mixed.obs": outputs.get("mixed_rinex"),
            "virtual_base_master_based.obs": outputs.get("raw_rinex"),
            "base_ppp.obs": outputs.get("ppp_aux_rinex"),
            "resumo_processamento.txt": outputs.get("solution_txt"),
            "virtual_base_meta.json": outputs.get("virtual_meta"),
            "virtual_station_validation_report.json": outputs.get("virtual_validation"),
            "virtual_station_observations.json": outputs.get("virtual_json"),
            "virtual_station_numeric_summary.json": outputs.get("virtual_summary"),
            "virtual_station_observations_sanitized.json": outputs.get(
                "sanitized_json"
            ),
            "virtual_station_numeric_summary_sanitized.json": outputs.get(
                "sanitized_summary"
            ),
            "sync_summary.json": outputs.get("sync_summary"),
            "numeric_observations_summary.json": outputs.get("numeric_summary"),
            "epoch_observations_summary.json": outputs.get("observation_summary"),
            "synchronized_observation_blocks.json": outputs.get("observation_blocks"),
        }

        manifest_path = outputs.get("manifest")
        if manifest_path and manifest_path.exists():
            zipf.write(manifest_path, arcname="manifest.json")

        added_real_paths = {primary_rinex.resolve()}

        for arcname, path in aux_map.items():
            if not path or not path.exists() or not path.is_file():
                continue

            try:
                real = path.resolve()
            except Exception:
                real = path

            if real in added_real_paths:
                continue

            zipf.write(path, arcname=arcname)
            added_real_paths.add(real)

        # README simples
        readme = [
            f"Job ID: {job.id}",
            f"Nome: {job.name}",
            "",
            "ARQUIVO PRINCIPAL PARA PROCESSAMENTO:",
            f"- {primary_rinex.name}",
            "",
            "ORDEM DE PRIORIDADE ADOTADA:",
            "1) custom_final_rinex_path",
            "2) virtual_rinex_clean_path",
            "3) virtual_rinex_mixed_path",
            "4) ppp_aux_rinex_path",
            "5) virtual_rinex_path",
            "6) solution_path",
            "",
            "Observação:",
            "O produto principal foi definido para priorizar o RINEX final compatível com o fluxo do IBGE-PPP.",
        ]
        zipf.writestr("LEIA-ME.txt", "\n".join(readme))

    return zip_path


# ____________________#


def _get_job_selected_station_codes(job: VRSJob) -> list[str]:
    raw = getattr(job, "selected_station_codes_json", None)
    if not raw:
        return []

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip().upper() for item in data if str(item).strip()]
    except Exception:
        pass

    return []


def _read_job_progress_file(job_id: int) -> dict | None:
    try:
        job_root = (
            Path(__file__).resolve().parents[2]
            / "storage"
            / "processing"
            / f"job_{job_id}"
        )
        progress_path = job_root / "results" / "progress.json"

        if progress_path.exists():
            return json.loads(progress_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    return None


def _get_network_status_label(job: VRSJob) -> str:
    status = (job.network_validation_status or "").strip().lower()

    if status == "completed":
        return "Validada"
    if status == "failed":
        return "Falhou"
    if status == "running":
        return "Validando"

    return "Pendente"


def _get_processing_status_label(job: VRSJob) -> str:
    status = (job.processing_status or "").strip().lower()

    if status == "completed":
        return "Concluído"
    if status == "failed":
        return "Falhou"
    if status == "running":
        return "Processando"

    return "Pendente"


def _get_general_status_label(job: VRSJob) -> str:
    processing_status = (job.processing_status or "").strip().lower()
    general_status = (job.status or "").strip().lower()

    if processing_status == "completed":
        return "Concluído"
    if processing_status == "failed":
        return "Falhou"
    if processing_status == "running":
        return "Processando"

    if general_status == "completed":
        return "Concluído"
    if general_status == "failed":
        return "Falhou"
    if general_status == "running":
        return "Processando"

    return "Rascunho"


def _get_next_step_hint(job: VRSJob) -> str:
    processing_status = (job.processing_status or "").strip().lower()

    if processing_status == "completed":
        return "Pronto para exportação."
    if processing_status == "failed":
        return "Revise os parâmetros e processe novamente."
    if processing_status == "running":
        return "Processamento em andamento..."

    return "Pronto para processar."


def _build_job_progress(job: VRSJob) -> tuple[int, str]:
    progress_data = _read_job_progress_file(job.id)

    if progress_data:
        try:
            percent = int(progress_data.get("percent", 0) or 0)
        except Exception:
            percent = 0

        label = str(progress_data.get("message") or "Processando")
        return percent, label

    processing_status = (job.processing_status or "").strip().lower()
    network_status = (job.network_validation_status or "").strip().lower()

    if processing_status == "completed":
        return 100, "Processamento concluído"

    if processing_status == "failed":
        return 100, "Falha no processamento"

    if processing_status == "running":
        return 5, "Processando"

    if network_status == "completed":
        return 5, "Rede validada"

    selected_codes = _get_job_selected_station_codes(job)
    if selected_codes:
        return 2, "Estações selecionadas"

    return 0, "Aguardando processamento"


def _safe_json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _normalize_station_codes(value) -> list[str]:
    if not isinstance(value, list):
        return []

    cleaned = []
    seen = set()

    for item in value:
        if item is None:
            continue

        code = str(item).strip().upper()
        if not code or code in seen:
            continue

        seen.add(code)
        cleaned.append(code)

    return cleaned


def _set_job_selected_station_codes(
    job: VRSJob, station_codes: list[str] | None
) -> None:
    normalized = _normalize_station_codes(station_codes or [])
    if hasattr(job, "selected_station_codes_json"):
        job.selected_station_codes_json = json.dumps(normalized, ensure_ascii=False)
    if hasattr(job, "selected_station_count"):
        job.selected_station_count = len(normalized)


def _snapshot_vrs_profile_into_job(
    db: Session,
    current_user,
    job,
) -> dict:
    resolved_profile = resolve_profile_for_processing(
        db=db,
        user_id=current_user.id,
        explicit_profile_id=getattr(job, "vrs_profile_id", None),
    )

    if hasattr(job, "vrs_profile_name"):
        job.vrs_profile_name = resolved_profile["profile_name"]
    if hasattr(job, "vrs_profile_type"):
        job.vrs_profile_type = resolved_profile["profile_type"]
    if hasattr(job, "vrs_profile_config_json"):
        job.vrs_profile_config_json = json.dumps(
            resolved_profile["config"],
            ensure_ascii=False,
        )

    db.add(job)
    db.commit()
    db.refresh(job)

    return resolved_profile


def detect_observation_file(file_names: list[str]) -> str | None:
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


def _status_label(status_value: str | None) -> str:
    if status_value == "completed":
        return "Concluído"
    if status_value == "failed":
        return "Falhou"
    if status_value == "running":
        return "Em execução"
    if status_value == "queued":
        return "Em fila"
    if status_value == "draft":
        return "Rascunho"
    return "Pendente"


def _network_status_label(status_value: str | None) -> str:
    if status_value == "completed":
        return "Validada"
    if status_value == "failed":
        return "Falhou"
    if status_value == "running":
        return "Validando"
    return "Pendente"


def _processing_status_label(status_value: str | None) -> str:
    if status_value == "completed":
        return "Concluído"
    if status_value == "failed":
        return "Falhou"
    if status_value == "running":
        return "Processando"
    return "Pendente"


def _progress_info(job: VRSJob) -> tuple[int, str]:
    if (job.export_count or 0) > 0:
        return 100, "Exportado"

    if job.processing_status == "completed" or job.status == "completed":
        return 75, "Processamento concluído"

    if job.network_validation_status == "completed":
        return 45, "Rede validada"

    if len(_get_job_selected_station_codes(job)) >= 3:
        return 20, "Estações selecionadas"

    return 0, "Aguardando criação"


def _next_step_hint(job: VRSJob) -> str:
    if (job.export_count or 0) > 0:
        return "Fluxo concluído."
    if job.processing_status == "completed":
        return "Pronto para exportação."
    if job.network_validation_status == "completed":
        return "Pronto para processamento."
    return "Valide a rede para continuar."


def _try_extract_lat_lon_from_job_processing(
    job_id: int, base_filename: str
) -> tuple[float | None, float | None]:
    try:
        if not base_filename:
            return None, None

        base_name = base_filename.lower()
        processing_root = STORAGE_DIR / "processing" / f"job_{job_id}" / "metadata"

        candidates = [
            processing_root / "base1_meta.json",
            processing_root / "base2_meta.json",
            processing_root / "base3_meta.json",
        ]

        for candidate in candidates:
            if not candidate.exists():
                continue

            data = json.loads(candidate.read_text(encoding="utf-8"))
            file_name = str(data.get("file_name", "")).lower()
            marker_name = str(data.get("marker_name", "")).lower()

            if base_name in file_name or (marker_name and marker_name in base_name):
                geodetic = data.get("geodetic", {}) or {}
                return geodetic.get("lat"), geodetic.get("lon")

        return None, None
    except Exception:
        return None, None


def _serialize_job_for_dashboard(job: VRSJob) -> dict:
    progress_percent, progress_label = _progress_info(job)

    base_1_lat, base_1_lon = _try_extract_lat_lon_from_job_processing(
        job.id,
        getattr(job, "base_1_detected_observation_file", None)
        or getattr(job, "base_1_original_name", None)
        or "",
    )
    base_2_lat, base_2_lon = _try_extract_lat_lon_from_job_processing(
        job.id,
        getattr(job, "base_2_detected_observation_file", None)
        or getattr(job, "base_2_original_name", None)
        or "",
    )
    base_3_lat, base_3_lon = _try_extract_lat_lon_from_job_processing(
        job.id,
        getattr(job, "base_3_detected_observation_file", None)
        or getattr(job, "base_3_original_name", None)
        or "",
    )

    reference_base_name = None
    try:
        virtual_meta_path = (
            STORAGE_DIR
            / "processing"
            / f"job_{job.id}"
            / "results"
            / "virtual_base_meta.json"
        )
        if virtual_meta_path.exists():
            meta = json.loads(virtual_meta_path.read_text(encoding="utf-8"))
            ref = meta.get("reference_base") or {}
            reference_base_name = ref.get("file_name")
    except Exception:
        reference_base_name = None

    selected_station_codes = _get_job_selected_station_codes(job)

    return {
        "id": job.id,
        "name": job.name,
        "output_rinex_name": getattr(job, "output_rinex_name", None),
        "output_marker_name": getattr(job, "output_marker_name", None),
        "rover_filename": getattr(job, "rover_filename", None),
        "upload_original_name": getattr(job, "upload_original_name", None),
        "detected_observation_file": getattr(job, "detected_observation_file", None),
        "uploaded_file_path": getattr(job, "uploaded_file_path", None),
        "approx_lat": getattr(job, "approx_lat", None),
        "approx_lon": getattr(job, "approx_lon", None),
        "requested_minutes": job.requested_minutes,
        "generated_minutes": job.generated_minutes,
        "export_count": job.export_count,
        "status": job.status,
        "rinex_path": getattr(job, "rinex_path", None),
        "notes": job.notes,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "preprocess_status": getattr(job, "preprocess_status", None),
        "preprocess_summary": getattr(job, "preprocess_summary", None),
        "observation_file_path": getattr(job, "observation_file_path", None),
        "marker_name": getattr(job, "marker_name", None),
        "rinex_version": getattr(job, "rinex_version", None),
        "approx_position_text": getattr(job, "approx_position_text", None),
        "interval_seconds": getattr(job, "interval_seconds", None),
        "first_epoch_text": getattr(job, "first_epoch_text", None),
        "last_epoch_text": getattr(job, "last_epoch_text", None),
        "processing_status": getattr(job, "processing_status", None),
        "processing_summary": getattr(job, "processing_summary", None),
        "solution_path": getattr(job, "solution_path", None),
        "target_lat": getattr(job, "target_lat", None),
        "target_lon": getattr(job, "target_lon", None),
        "target_height": getattr(job, "target_height", None),
        "session_date": getattr(job, "session_date", None),
        "start_time": getattr(job, "start_time", None),
        "end_time": getattr(job, "end_time", None),
        "occupation_minutes": getattr(job, "occupation_minutes", None),
        "base_1_original_name": getattr(job, "base_1_original_name", None),
        "base_1_detected_observation_file": getattr(
            job, "base_1_detected_observation_file", None
        ),
        "base_1_uploaded_file_path": getattr(job, "base_1_uploaded_file_path", None),
        "base_2_original_name": getattr(job, "base_2_original_name", None),
        "base_2_detected_observation_file": getattr(
            job, "base_2_detected_observation_file", None
        ),
        "base_2_uploaded_file_path": getattr(job, "base_2_uploaded_file_path", None),
        "base_3_original_name": getattr(job, "base_3_original_name", None),
        "base_3_detected_observation_file": getattr(
            job, "base_3_detected_observation_file", None
        ),
        "base_3_uploaded_file_path": getattr(job, "base_3_uploaded_file_path", None),
        "selected_station_codes": selected_station_codes,
        "network_validation_status": getattr(job, "network_validation_status", None),
        "network_validation_summary": getattr(job, "network_validation_summary", None),
        "vrs_profile_name": getattr(job, "vrs_profile_name", None),
        "vrs_profile_type": getattr(job, "vrs_profile_type", None),
        "status_label": _status_label(job.status),
        "network_status_label": _network_status_label(
            getattr(job, "network_validation_status", None)
        ),
        "processing_status_label": _processing_status_label(
            getattr(job, "processing_status", None)
        ),
        "progress_percent": progress_percent,
        "progress_label": progress_label,
        "next_step_hint": _next_step_hint(job),
        "can_validate_network": job.processing_status != "running",
        "can_process": getattr(job, "network_validation_status", None) == "completed"
        and getattr(job, "processing_status", None) != "running",
        "can_export": (
            getattr(job, "processing_status", None) == "completed"
            or job.status == "completed"
        ),
        "processing_mode": "network_only",
        "base_1_lat": base_1_lat,
        "base_1_lon": base_1_lon,
        "base_2_lat": base_2_lat,
        "base_2_lon": base_2_lon,
        "base_3_lat": base_3_lat,
        "base_3_lon": base_3_lon,
        "reference_base_name": reference_base_name,
    }


def _remove_processing_artifacts(job_id: int) -> None:
    job_root = STORAGE_DIR / "processing" / f"job_{job_id}"
    if not job_root.exists():
        return

    for path in sorted(job_root.rglob("*"), reverse=True):
        try:
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        except Exception:
            pass


def _normalize_navigation_files(navigation_files_raw) -> list[dict]:
    normalized: list[dict] = []

    if isinstance(navigation_files_raw, dict):
        for source_key, files in navigation_files_raw.items():
            if not isinstance(files, list):
                continue

            for item in files:
                if not item:
                    continue

                if isinstance(item, dict):
                    file_path = item.get("file_path") or item.get("path")
                    file_name = item.get("file_name") or (
                        Path(file_path).name if file_path else None
                    )
                    if file_path:
                        normalized.append(
                            {
                                "source": source_key,
                                "file_name": file_name,
                                "file_path": file_path,
                            }
                        )
                elif isinstance(item, str):
                    normalized.append(
                        {
                            "source": source_key,
                            "file_name": Path(item).name,
                            "file_path": item,
                        }
                    )

    elif isinstance(navigation_files_raw, list):
        for item in navigation_files_raw:
            if isinstance(item, dict):
                file_path = item.get("file_path") or item.get("path")
                file_name = item.get("file_name") or (
                    Path(file_path).name if file_path else None
                )
                if file_path:
                    normalized.append(
                        {
                            "source": item.get("source", "unknown"),
                            "file_name": file_name,
                            "file_path": file_path,
                        }
                    )
            elif isinstance(item, str):
                normalized.append(
                    {
                        "source": "unknown",
                        "file_name": Path(item).name,
                        "file_path": item,
                    }
                )

    unique = []
    seen = set()
    for item in normalized:
        key = item.get("file_path")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


def _build_readme_text(
    job_id: int, job_name: str, custom_rinex_filename: str, marker_name: str
) -> str:
    return "\n".join(
        [
            "ORBITBASE - PACOTE DE BASE VIRTUAL (VRS)",
            "",
            "1. OBJETIVO",
            "Este pacote contém a base virtual gerada a partir das bases reais utilizadas no processamento.",
            "",
            "2. ARQUIVO PRINCIPAL",
            f"- {custom_rinex_filename}",
            "",
            "3. MARKER NAME UTILIZADO",
            f"- {marker_name}",
            "",
            "4. NAVEGAÇÃO",
            "Os arquivos de navegação detectados das bases utilizadas acompanham este pacote.",
            "",
            "5. OBSERVAÇÕES",
            "- Recomenda-se utilizar o arquivo principal .obs juntamente com os arquivos de navegação incluídos.",
            "- Recomenda-se validar a solução no software GNSS utilizado.",
            "",
            f"Job: {job_id} - {job_name}",
        ]
    )


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_rover_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Arquivo inválido.")

    safe_name = file.filename.replace("/", "_").replace("\\", "_").replace(" ", "_")
    unique_name = f"{current_user.id}_{uuid4().hex}_{safe_name}"
    target_path = UPLOAD_DIR / unique_name

    content = await file.read()
    target_path.write_bytes(content)

    return UploadResponse(
        filename=file.filename,
        stored_path=str(target_path),
        size_bytes=len(content),
    )


@router.post(
    "/upload-and-extract",
    response_model=ExtractedFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_and_extract_rover_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Arquivo inválido.")

    safe_name = file.filename.replace("/", "_").replace("\\", "_").replace(" ", "_")
    unique_prefix = f"{current_user.id}_{uuid4().hex}"
    saved_zip_path = UPLOAD_DIR / f"{unique_prefix}_{safe_name}"

    content = await file.read()
    saved_zip_path.write_bytes(content)

    lower_name = file.filename.lower()
    if not lower_name.endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Este endpoint aceita apenas arquivos .zip.",
        )

    extract_target_dir = EXTRACT_DIR / unique_prefix
    extract_target_dir.mkdir(parents=True, exist_ok=True)

    extracted_files = []

    try:
        with zipfile.ZipFile(saved_zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_target_dir)

            for member in zip_ref.namelist():
                if member.endswith("/") or member.startswith("__MACOSX/"):
                    continue
                extracted_files.append(Path(member).name)

    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=400,
            detail="Arquivo ZIP inválido ou corrompido.",
        )

    detected_observation = detect_observation_file(extracted_files)

    detected_observation_path = None
    if detected_observation:
        for path in extract_target_dir.rglob("*"):
            if path.is_file() and path.name == detected_observation:
                detected_observation_path = str(path)
                break

    return ExtractedFileResponse(
        original_filename=file.filename,
        extracted_files=extracted_files,
        detected_observation_file=detected_observation,
        detected_observation_path=detected_observation_path,
    )


@router.post("", response_model=VRSJobRead, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: VRSJobCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    selected_station_codes = _normalize_station_codes(
        getattr(payload, "selected_station_codes", None)
    )

    if len(selected_station_codes) < 3:
        raise HTTPException(
            status_code=400,
            detail="Selecione ao menos 3 estações RBMC no mapa antes de criar o job.",
        )

    if (
        payload.target_lat is None
        or payload.target_lon is None
        or payload.target_height is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Informe latitude, longitude e altitude da VRS antes de criar o job.",
        )

    job = VRSJob(
        user_id=current_user.id,
        name=payload.name.strip(),
        output_rinex_name=getattr(payload, "output_rinex_name", None),
        output_marker_name=getattr(payload, "output_marker_name", None),
        rover_filename=getattr(payload, "rover_filename", None),
        upload_original_name=getattr(payload, "upload_original_name", None),
        detected_observation_file=getattr(payload, "detected_observation_file", None),
        uploaded_file_path=getattr(payload, "uploaded_file_path", None),
        observation_file_path=getattr(payload, "observation_file_path", None),
        target_lat=getattr(payload, "target_lat", None),
        target_lon=getattr(payload, "target_lon", None),
        target_height=getattr(payload, "target_height", None),
        session_date=getattr(payload, "session_date", None),
        start_time=getattr(payload, "start_time", None),
        end_time=getattr(payload, "end_time", None),
        occupation_minutes=getattr(payload, "occupation_minutes", None),
        approx_lat=getattr(payload, "approx_lat", None),
        approx_lon=getattr(payload, "approx_lon", None),
        requested_minutes=payload.requested_minutes,
        notes=getattr(payload, "notes", None),
        status="draft",
        processing_mode="network_only",
    )

    _set_job_selected_station_codes(job, selected_station_codes)

    db.add(job)
    db.commit()
    db.refresh(job)

    return _serialize_job_for_dashboard(job)


@router.get("")
def list_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    jobs = (
        db.query(VRSJob)
        .filter(VRSJob.user_id == current_user.id)
        .order_by(VRSJob.created_at.desc())
        .all()
    )

    result = []

    for job in jobs:
        selected_codes = _get_job_selected_station_codes(job)

        progress_percent, progress_label = _build_job_progress(job)

        processing_running = job.processing_status == "running"
        processing_completed = job.processing_status == "completed"

        can_process = not processing_running and not processing_completed
        can_export = processing_completed and bool(job.solution_path)

        reference_base_name = getattr(job, "reference_base_name", None)

        if not reference_base_name:
            try:
                job_root = (
                    Path(__file__).resolve().parents[2]
                    / "storage"
                    / "processing"
                    / f"job_{job.id}"
                )
                virtual_meta_path = job_root / "results" / "virtual_base_meta.json"

                if virtual_meta_path.exists():
                    meta = json.loads(virtual_meta_path.read_text(encoding="utf-8"))
                    reference_base = meta.get("reference_base", {}) or {}
                    reference_base_name = reference_base.get("file_name")
            except Exception:
                reference_base_name = None

        result.append(
            {
                "id": job.id,
                "name": job.name,
                "selected_station_codes": selected_codes,
                "reference_base_name": reference_base_name or "-",
                "network_validation_status": job.network_validation_status,
                "network_validation_summary": job.network_validation_summary,
                "network_status_label": _get_network_status_label(job),
                "processing_status": job.processing_status,
                "processing_summary": job.processing_summary,
                "processing_status_label": _get_processing_status_label(job),
                "status": job.status,
                "status_label": _get_general_status_label(job),
                "progress_percent": progress_percent,
                "progress_label": progress_label,
                "next_step_hint": _get_next_step_hint(job),
                "requested_minutes": job.requested_minutes,
                "generated_minutes": job.generated_minutes,
                "export_count": job.export_count,
                "solution_path": job.solution_path,
                "can_validate_network": False,
                "can_process": can_process,
                "can_export": can_export,
                "target_lat": job.target_lat,
                "target_lon": job.target_lon,
                "target_height": job.target_height,
                "session_date": job.session_date,
                "start_time": job.start_time,
                "end_time": job.end_time,
                "occupation_minutes": job.occupation_minutes,
                "created_at": (
                    job.created_at.isoformat()
                    if getattr(job, "created_at", None)
                    else None
                ),
                "updated_at": (
                    job.updated_at.isoformat()
                    if getattr(job, "updated_at", None)
                    else None
                ),
            }
        )

    return result


@router.put("/{job_id}", response_model=VRSJobRead)
def update_job_put(
    job_id: int,
    payload: VRSJobUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    if job.status == "running" or job.processing_status == "running":
        raise HTTPException(
            status_code=400,
            detail="Não é possível editar um job em processamento.",
        )

    old_values = {
        "name": job.name,
        "output_rinex_name": getattr(job, "output_rinex_name", None),
        "output_marker_name": getattr(job, "output_marker_name", None),
        "requested_minutes": job.requested_minutes,
        "notes": job.notes,
        "target_lat": getattr(job, "target_lat", None),
        "target_lon": getattr(job, "target_lon", None),
        "target_height": getattr(job, "target_height", None),
        "session_date": getattr(job, "session_date", None),
        "start_time": getattr(job, "start_time", None),
        "end_time": getattr(job, "end_time", None),
        "occupation_minutes": getattr(job, "occupation_minutes", None),
        "selected_station_codes": _get_job_selected_station_codes(job),
    }

    update_data = payload.model_dump(exclude_unset=True)

    selected_station_codes = None
    if "selected_station_codes" in update_data:
        selected_station_codes = _normalize_station_codes(
            update_data.pop("selected_station_codes")
        )

    for field, value in update_data.items():
        if hasattr(job, field):
            setattr(job, field, value)

    if selected_station_codes is not None:
        if len(selected_station_codes) < 3:
            raise HTTPException(
                status_code=400,
                detail="Selecione ao menos 3 estações RBMC no mapa.",
            )
        _set_job_selected_station_codes(job, selected_station_codes)

    editable_fields_that_affect_processing = {
        "name",
        "output_rinex_name",
        "output_marker_name",
        "requested_minutes",
        "notes",
        "target_lat",
        "target_lon",
        "target_height",
        "session_date",
        "start_time",
        "end_time",
        "occupation_minutes",
        "selected_station_codes",
    }

    changed_processing_fields = any(
        field in update_data and update_data[field] != old_values.get(field)
        for field in editable_fields_that_affect_processing
        if field != "selected_station_codes"
    )

    if selected_station_codes is not None and selected_station_codes != old_values.get(
        "selected_station_codes"
    ):
        changed_processing_fields = True

    if changed_processing_fields:
        job.network_validation_status = None
        job.network_validation_summary = None

        job.processing_status = None
        job.processing_summary = None
        job.solution_path = None
        job.generated_minutes = 0
        job.export_count = 0

        job.preprocess_status = None
        job.preprocess_summary = None
        job.observation_file_path = None
        job.marker_name = None
        job.rinex_version = None
        job.approx_position_text = None
        job.interval_seconds = None
        job.first_epoch_text = None
        job.last_epoch_text = None
        job.rinex_path = None

        job.status = "draft"

        _remove_processing_artifacts(job.id)

    db.commit()
    db.refresh(job)

    return _serialize_job_for_dashboard(job)


@router.patch("/{job_id}", response_model=VRSJobRead)
def update_job_patch(
    job_id: int,
    payload: VRSJobUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return update_job_put(
        job_id=job_id, payload=payload, current_user=current_user, db=db
    )


@router.delete("/{job_id}", response_model=MessageResponse)
def delete_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    _remove_processing_artifacts(job.id)

    db.delete(job)
    db.commit()
    return MessageResponse(message="Job removido com sucesso.")


@router.post("/{job_id}/generate", response_model=VRSJobRead)
def simulate_generate_vrs(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    if job.status == "completed":
        raise HTTPException(status_code=400, detail="Este job já foi concluído.")

    job.status = "running"
    db.commit()

    generated_minutes = max(1, job.requested_minutes)
    job.generated_minutes = generated_minutes
    job.status = "completed"
    job.rinex_path = f"/storage/rinex/job_{job.id}.obs"

    add_generated_minutes(db, current_user, generated_minutes)

    db.commit()
    db.refresh(job)
    return _serialize_job_for_dashboard(job)


@router.post("/{job_id}/preprocess", response_model=PreprocessResponse)
def preprocess_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    try:
        observation_path = detect_observation_file_from_job(job)
        if not observation_path:
            raise ValueError(
                "Não foi possível localizar o arquivo de observação do job."
            )

        metadata = parse_rinex_observation_metadata(observation_path)

        job.observation_file_path = observation_path
        job.marker_name = metadata.get("marker_name")
        job.rinex_version = metadata.get("rinex_version")
        job.approx_position_text = metadata.get("approx_position_text")
        job.interval_seconds = metadata.get("interval_seconds")
        job.first_epoch_text = metadata.get("first_epoch_text")
        job.last_epoch_text = metadata.get("last_epoch_text")
        job.preprocess_status = "completed"
        job.preprocess_summary = (
            f"Pré-processamento concluído. "
            f"Versão RINEX: {job.rinex_version or '-'} | "
            f"Marker: {job.marker_name or '-'}"
        )

        db.commit()
        db.refresh(job)

        return PreprocessResponse(
            job_id=job.id,
            preprocess_status=job.preprocess_status,
            preprocess_summary=job.preprocess_summary,
            observation_file_path=job.observation_file_path,
            marker_name=job.marker_name,
            rinex_version=job.rinex_version,
            approx_position_text=job.approx_position_text,
            interval_seconds=job.interval_seconds,
            first_epoch_text=job.first_epoch_text,
            last_epoch_text=job.last_epoch_text,
        )

    except Exception as exc:
        job.preprocess_status = "failed"
        job.preprocess_summary = str(exc)
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{job_id}/process")
def process_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    try:
        resolved_profile = _snapshot_vrs_profile_into_job(
            db=db,
            current_user=current_user,
            job=job,
        )

        result = process_real_vrs(
            job,
            profile_config=resolved_profile["config"],
        )

        job.processing_status = "completed"
        job.processing_summary = result.get(
            "processing_summary",
            "Processamento VRS concluído com sucesso.",
        )
        job.status = "completed"
        job.solution_path = result.get("solution_path")
        job.rinex_path = (
            result.get("base_ppp_path")
            or result.get("custom_final_rinex_path")
            or result.get("ppp_aux_rinex_path")
            or result.get("virtual_rinex_mixed_path")
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        return {
            "message": "Processamento concluído com sucesso.",
            "job_id": job.id,
            "profile_name": getattr(job, "vrs_profile_name", None),
            "profile_type": getattr(job, "vrs_profile_type", None),
            "result": result,
        }

    except Exception as e:
        job.processing_status = "failed"
        job.processing_summary = str(e)
        job.status = "failed"

        db.add(job)
        db.commit()

        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/validate-network", response_model=NetworkValidationResponse)
def validate_job_network(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    selected_station_codes = _get_job_selected_station_codes(job)

    if len(selected_station_codes) < 3:
        raise HTTPException(
            status_code=400,
            detail="Selecione ao menos 3 estações RBMC no mapa antes de validar a rede.",
        )

    if not job.target_lat or not job.target_lon or job.target_height is None:
        raise HTTPException(
            status_code=400,
            detail="Informe as coordenadas alvo da VRS antes de validar a rede.",
        )

    if not job.session_date:
        raise HTTPException(
            status_code=400,
            detail="Informe a data da sessão antes de validar a rede.",
        )

    sync_note = None

    try:
        rbmc_sync_result = sync_rbmc_observations_for_job_date(
            db=db,
            storage_root=str(STORAGE_DIR),
            session_date=job.session_date,
            selected_station_codes=selected_station_codes,
        )

        sync_note = (
            f"RBMC: {rbmc_sync_result.get('stations_with_observation', 0)}/"
            f"{rbmc_sync_result.get('stations_requested', 0)} estações com observação sincronizada."
        )
    except Exception as exc:
        sync_note = f"Falha na sincronização RBMC: {exc}"

    status_result, summary_result = validate_network(job)

    if sync_note:
        summary_result = f"{summary_result} {sync_note}"

    job.network_validation_status = status_result
    job.network_validation_summary = summary_result

    db.add(job)
    db.commit()
    db.refresh(job)

    return NetworkValidationResponse(
        job_id=job.id,
        network_validation_status=job.network_validation_status,
        network_validation_summary=job.network_validation_summary,
    )


def load_selected_station_codes(job: VRSJob) -> list[str]:
    raw = job.selected_station_codes_json
    if not raw:
        return []

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item).strip().upper() for item in data if str(item).strip()]
    except Exception:
        pass

    return []


def run_embedded_network_validation(
    db: Session,
    job: VRSJob,
    current_user: User,
) -> dict:
    try:
        result = validate_job_network(
            job_id=job.id,
            current_user=current_user,
            db=db,
        )

        return {
            "is_valid": result.network_validation_status == "completed",
            "summary": result.network_validation_summary or "",
        }

    except HTTPException as exc:
        detail = exc.detail if hasattr(exc, "detail") else str(exc)
        return {
            "is_valid": False,
            "summary": str(detail),
        }

    except Exception as exc:
        return {
            "is_valid": False,
            "summary": f"Erro na validação da rede: {type(exc).__name__}: {exc}",
        }


@router.post("/{job_id}/process-real")
def process_job_real_vrs(
    job_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    vrs_job = (
        db.query(VRSJob)
        .filter(
            VRSJob.id == job_id,
            VRSJob.user_id == current_user.id,
        )
        .first()
    )

    if not vrs_job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    def _write_progress(
        percent: int,
        step: str,
        message: str,
        detail: str | None = None,
        status: str = "running",
    ) -> None:
        try:
            job_root = (
                Path(__file__).resolve().parents[2]
                / "storage"
                / "processing"
                / f"job_{job_id}"
            )
            results_dir = job_root / "results"
            results_dir.mkdir(parents=True, exist_ok=True)

            progress_payload = {
                "job_id": vrs_job.id,
                "percent": int(percent),
                "step": step,
                "message": message,
                "detail": detail or "",
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "status": status,
            }

            (results_dir / "progress.json").write_text(
                json.dumps(progress_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    if vrs_job.processing_status == "running":
        raise HTTPException(
            status_code=400,
            detail="Este job já está em processamento.",
        )

    try:
        vrs_job.status = "running"
        vrs_job.processing_status = "running"
        vrs_job.processing_summary = "Iniciando processamento..."
        db.commit()

        _write_progress(
            2,
            "starting",
            "Iniciando processamento",
            "Preparando validação e processamento do job.",
            status="running",
        )

        # resolve perfil
        resolved_profile = resolve_effective_profile_for_job(
            db=db,
            user_id=current_user.id,
            job=vrs_job,
        )

        _write_progress(
            8,
            "network_validation",
            "Validando rede GNSS",
            "Executando validação automática da rede antes do processamento.",
            status="running",
        )

        # validação da rede embutida no fluxo
        validation_result = run_embedded_network_validation(
            db=db,
            job=vrs_job,
            current_user=current_user,
        )

        vrs_job.network_validation_status = (
            "completed" if validation_result.get("is_valid") else "failed"
        )
        vrs_job.network_validation_summary = validation_result.get("summary") or ""
        db.commit()

        if not validation_result.get("is_valid"):
            detail = (
                validation_result.get("summary")
                or "A rede selecionada não passou na validação."
            )
            vrs_job.status = "failed"
            vrs_job.processing_status = "failed"
            vrs_job.processing_summary = detail
            db.commit()

            _write_progress(
                100,
                "error",
                "Falha na validação da rede",
                detail,
                status="failed",
            )

            raise HTTPException(status_code=400, detail=detail)

        _write_progress(
            15,
            "network_validated",
            "Rede validada",
            vrs_job.network_validation_summary
            or "Rede aprovada. Iniciando o motor VRS.",
            status="running",
        )

        result = process_real_vrs(
            vrs_job,
            profile_config=resolved_profile["config"],
        )

        vrs_job.status = "completed"
        vrs_job.processing_status = "completed"
        vrs_job.processing_summary = result.get(
            "processing_summary",
            "Processamento concluído com sucesso.",
        )
        vrs_job.solution_path = result.get("solution_path")
        vrs_job.generated_minutes = int(vrs_job.requested_minutes or 0)
        db.commit()

        _write_progress(
            100,
            "done",
            "Processamento concluído",
            vrs_job.processing_summary,
            status="completed",
        )

        return {
            "ok": True,
            "job_id": vrs_job.id,
            "message": "Processamento concluído com sucesso.",
            "result": result,
        }

    except HTTPException:
        raise

    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"

        try:
            vrs_job.status = "failed"
            vrs_job.processing_status = "failed"
            vrs_job.processing_summary = detail
            db.commit()
        except Exception:
            db.rollback()

        _write_progress(
            100,
            "error",
            "Falha no processamento",
            detail,
            status="failed",
        )

        raise HTTPException(status_code=400, detail=detail)


@router.post("/{job_id}/export")
def export_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(VRSJob)
        .filter(VRSJob.id == job_id, VRSJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    if getattr(job, "processing_status", None) != "completed":
        raise HTTPException(
            status_code=400,
            detail="O job ainda não foi processado com sucesso.",
        )

    try:
        outputs = _resolve_processing_outputs(job)

        primary_file = (
            outputs.get("primary_rinex")
            or outputs.get("clean_rinex")
            or outputs.get("mixed_rinex")
            or outputs.get("ppp_aux_rinex")
            or outputs.get("raw_rinex")
        )

        if primary_file is None:
            raise ValueError(
                "Nenhum arquivo RINEX final foi localizado para exportação."
            )

        zip_path = _write_job_export_zip(job, outputs)

        current_exports = int(getattr(job, "exports_count", 0) or 0)
        job.exports_count = current_exports + 1
        job.status = "completed"
        db.add(job)
        db.commit()
        db.refresh(job)

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=zip_path.name,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Falha ao exportar job: {exc}",
        )


class JobProgressRead(BaseModel):
    job_id: int
    percent: int = 0
    step: str = "pending"
    message: str = "Aguardando processamento"
    detail: str | None = None
    updated_at: str | None = None
    status: str = "pending"


@router.get("/{job_id}/progress", response_model=JobProgressRead)
@router.get("/{job_id}/progress")
def get_job_progress(
    job_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    vrs_job = (
        db.query(VRSJob)
        .filter(
            VRSJob.id == job_id,
            VRSJob.user_id == current_user.id,
        )
        .first()
    )

    if not vrs_job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    job_root = (
        Path(__file__).resolve().parents[2] / "storage" / "processing" / f"job_{job_id}"
    )
    progress_path = job_root / "results" / "progress.json"

    if progress_path.exists():
        try:
            return json.loads(progress_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    progress_percent = 0
    progress_label = "Aguardando processamento"
    step = "pending"
    detail = ""

    if vrs_job.processing_status == "completed":
        progress_percent = 100
        progress_label = "Processamento concluído"
        step = "done"
        detail = vrs_job.processing_summary or ""

    elif vrs_job.processing_status == "failed":
        progress_percent = 100
        progress_label = "Falha no processamento"
        step = "error"
        detail = vrs_job.processing_summary or ""

    elif vrs_job.processing_status == "running":
        progress_percent = 1
        progress_label = "Processando"
        step = "processing"
        detail = vrs_job.processing_summary or ""

    elif vrs_job.network_validation_status == "completed":
        progress_percent = 1
        progress_label = "Rede validada"
        step = "validated"
        detail = vrs_job.network_validation_summary or ""

    return {
        "job_id": vrs_job.id,
        "percent": progress_percent,
        "message": progress_label,
        "step": step,
        "detail": detail,
    }
