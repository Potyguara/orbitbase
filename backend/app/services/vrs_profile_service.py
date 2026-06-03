from __future__ import annotations

import json
from copy import deepcopy

from sqlalchemy.orm import Session

from app.models import VRSJob, VrsProcessingProfile

DEFAULT_VRS_PROFILE = {
    "name": "PPP padrao",
    "description": (
        "Equilibrio entre continuidade temporal, preservacao da janela completa "
        "e geracao de bloco auxiliar PPP."
    ),
    "profile_type": "system_default",
    "is_system": True,
    "config": {
        "min_bases_per_observation": 2,
        "min_satellites_per_epoch": 5,
        "max_satellites_per_epoch": 10,
        "min_satellite_presence_ratio": 0.45,
        "min_satellite_longest_arc": 80,
        "local_continuity_window": 2,
        "min_valid_in_local_window": 4,
        "prefer_nearest_base": True,
        "strong_nearest_base_bias": True,
        "require_dual_frequency": True,
        "preserve_full_user_window": True,
        "generate_ppp_aux_file": True,
        "priority_obs": ["P1", "C1", "L1", "P2", "C2", "L2", "C5", "S1", "S2", "S5"],
        "ppp_trials": [
            {
                "strategy": "strict",
                "min_consecutive_epochs": 120,
                "min_satellites_per_epoch": 8,
                "min_priority_obs_per_satellite_avg": 4.0,
                "max_gap_factor": 2.0,
            },
            {
                "strategy": "balanced",
                "min_consecutive_epochs": 60,
                "min_satellites_per_epoch": 6,
                "min_priority_obs_per_satellite_avg": 3.0,
                "max_gap_factor": 2.5,
            },
            {
                "strategy": "short_rover",
                "min_consecutive_epochs": 20,
                "min_satellites_per_epoch": 4,
                "min_priority_obs_per_satellite_avg": 2.0,
                "max_gap_factor": 3.0,
            },
        ],
    },
}


def resolve_effective_profile_for_job(
    db: Session,
    user_id: int,
    job: VRSJob,
) -> dict:
    """
    Resolve o perfil efetivo do job.
    Prioridade:
    1) perfil gravado no próprio job
    2) perfil ativo do usuário
    3) perfil padrão mínimo
    """
    # 1) perfil já persistido no job
    if getattr(job, "vrs_profile_config_json", None):
        try:
            config = json.loads(job.vrs_profile_config_json)
            return {
                "source": "job",
                "config": config,
                "name": getattr(job, "vrs_profile_name", None) or "Perfil do job",
            }
        except Exception:
            pass

    # 2) perfil ativo do usuário
    active_profile = (
        db.query(VrsProcessingProfile)
        .filter(
            VrsProcessingProfile.user_id == user_id,
            VrsProcessingProfile.is_active == True,
        )
        .order_by(VrsProcessingProfile.updated_at.desc())
        .first()
    )

    if active_profile and active_profile.config_json:
        try:
            config = json.loads(active_profile.config_json)
            return {
                "source": "user_active",
                "config": config,
                "name": active_profile.name,
            }
        except Exception:
            pass

    # 3) fallback mínimo
    return {
        "source": "default",
        "name": "Perfil padrão",
        "config": {},
    }


def get_default_vrs_config() -> dict:
    return deepcopy(DEFAULT_VRS_PROFILE["config"])


def _normalize_priority_obs(value, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return deepcopy(fallback)

    cleaned = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip().upper()
        if text:
            cleaned.append(text)

    return cleaned or deepcopy(fallback)


def _normalize_ppp_trials(value, fallback: list[dict]) -> list[dict]:
    if not isinstance(value, list) or not value:
        return deepcopy(fallback)

    normalized: list[dict] = []

    for item in value:
        if not isinstance(item, dict):
            continue

        strategy = str(item.get("strategy") or "").strip() or "custom"

        try:
            min_consecutive_epochs = int(item.get("min_consecutive_epochs", 20))
        except Exception:
            min_consecutive_epochs = 20

        try:
            min_satellites_per_epoch = int(item.get("min_satellites_per_epoch", 4))
        except Exception:
            min_satellites_per_epoch = 4

        try:
            min_priority_obs_per_satellite_avg = float(
                item.get("min_priority_obs_per_satellite_avg", 2.0)
            )
        except Exception:
            min_priority_obs_per_satellite_avg = 2.0

        try:
            max_gap_factor = float(item.get("max_gap_factor", 3.0))
        except Exception:
            max_gap_factor = 3.0

        normalized.append(
            {
                "strategy": strategy,
                "min_consecutive_epochs": max(1, min_consecutive_epochs),
                "min_satellites_per_epoch": max(1, min_satellites_per_epoch),
                "min_priority_obs_per_satellite_avg": max(
                    0.0, min_priority_obs_per_satellite_avg
                ),
                "max_gap_factor": max(0.0, max_gap_factor),
            }
        )

    return normalized or deepcopy(fallback)


def normalize_vrs_config(config: dict | None) -> dict:
    base = get_default_vrs_config()

    if not isinstance(config, dict):
        return base

    merged = deepcopy(base)
    merged.update(config)

    int_fields = [
        "min_bases_per_observation",
        "min_satellites_per_epoch",
        "max_satellites_per_epoch",
        "min_satellite_longest_arc",
        "local_continuity_window",
        "min_valid_in_local_window",
    ]

    float_fields = [
        "min_satellite_presence_ratio",
    ]

    bool_fields = [
        "prefer_nearest_base",
        "strong_nearest_base_bias",
        "require_dual_frequency",
        "preserve_full_user_window",
        "generate_ppp_aux_file",
    ]

    for field in int_fields:
        try:
            merged[field] = int(merged.get(field, base[field]))
        except Exception:
            merged[field] = base[field]

    for field in float_fields:
        try:
            merged[field] = float(merged.get(field, base[field]))
        except Exception:
            merged[field] = base[field]

    for field in bool_fields:
        merged[field] = bool(merged.get(field, base[field]))

    merged["min_bases_per_observation"] = max(1, merged["min_bases_per_observation"])
    merged["min_satellites_per_epoch"] = max(1, merged["min_satellites_per_epoch"])
    merged["max_satellites_per_epoch"] = max(
        merged["min_satellites_per_epoch"],
        merged["max_satellites_per_epoch"],
    )
    merged["min_satellite_presence_ratio"] = min(
        1.0, max(0.0, merged["min_satellite_presence_ratio"])
    )
    merged["min_satellite_longest_arc"] = max(1, merged["min_satellite_longest_arc"])
    merged["local_continuity_window"] = max(0, merged["local_continuity_window"])
    merged["min_valid_in_local_window"] = max(1, merged["min_valid_in_local_window"])

    merged["priority_obs"] = _normalize_priority_obs(
        merged.get("priority_obs"),
        base["priority_obs"],
    )
    merged["ppp_trials"] = _normalize_ppp_trials(
        merged.get("ppp_trials"),
        base["ppp_trials"],
    )

    return merged


def _safe_load_config(config_json: str | None) -> dict:
    if not config_json:
        return get_default_vrs_config()

    try:
        parsed = json.loads(config_json)
        if isinstance(parsed, dict):
            return normalize_vrs_config(parsed)
    except Exception:
        pass

    return get_default_vrs_config()


def ensure_system_default_profile(db: Session) -> VrsProcessingProfile:
    profile = (
        db.query(VrsProcessingProfile)
        .filter(VrsProcessingProfile.profile_type == "system_default")
        .first()
    )

    if profile:
        changed = False

        if not profile.name:
            profile.name = DEFAULT_VRS_PROFILE["name"]
            changed = True

        if not profile.description:
            profile.description = DEFAULT_VRS_PROFILE["description"]
            changed = True

        if profile.is_system is not True:
            profile.is_system = True
            changed = True

        if profile.profile_type != "system_default":
            profile.profile_type = "system_default"
            changed = True

        current_config = _safe_load_config(profile.config_json)
        normalized_default = normalize_vrs_config(DEFAULT_VRS_PROFILE["config"])

        if current_config != normalize_vrs_config(current_config):
            profile.config_json = json.dumps(
                normalize_vrs_config(current_config),
                ensure_ascii=False,
            )
            changed = True

        if changed:
            db.add(profile)
            db.commit()
            db.refresh(profile)

        return profile

    profile = VrsProcessingProfile(
        user_id=None,
        name=DEFAULT_VRS_PROFILE["name"],
        description=DEFAULT_VRS_PROFILE["description"],
        profile_type="system_default",
        is_system=True,
        is_active=False,
        config_json=json.dumps(
            normalize_vrs_config(DEFAULT_VRS_PROFILE["config"]),
            ensure_ascii=False,
        ),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def get_system_default_profile(db: Session) -> VrsProcessingProfile:
    return ensure_system_default_profile(db)


def get_user_active_profile(db: Session, user_id: int) -> VrsProcessingProfile | None:
    return (
        db.query(VrsProcessingProfile)
        .filter(
            VrsProcessingProfile.user_id == user_id,
            VrsProcessingProfile.is_active.is_(True),
        )
        .order_by(VrsProcessingProfile.id.desc())
        .first()
    )


def list_profiles_for_user(db: Session, user_id: int) -> list[VrsProcessingProfile]:
    ensure_system_default_profile(db)

    return (
        db.query(VrsProcessingProfile)
        .filter(
            (VrsProcessingProfile.user_id == user_id)
            | (VrsProcessingProfile.profile_type == "system_default")
        )
        .order_by(
            VrsProcessingProfile.is_system.desc(),
            VrsProcessingProfile.name.asc(),
            VrsProcessingProfile.id.asc(),
        )
        .all()
    )


def serialize_profile(profile: VrsProcessingProfile) -> dict:
    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "name": profile.name,
        "description": profile.description,
        "config": _safe_load_config(profile.config_json),
        "is_system": bool(profile.is_system),
        "is_active": bool(profile.is_active),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def resolve_profile_for_processing(
    db: Session,
    user_id: int,
    explicit_profile_id: int | None = None,
) -> dict:
    ensure_system_default_profile(db)

    profile: VrsProcessingProfile | None = None

    if explicit_profile_id is not None:
        profile = (
            db.query(VrsProcessingProfile)
            .filter(VrsProcessingProfile.id == explicit_profile_id)
            .first()
        )

        if not profile:
            raise ValueError("Perfil VRS informado não foi encontrado.")

        if not profile.is_system and profile.user_id != user_id:
            raise ValueError("O perfil VRS informado não pertence ao usuário.")

    if profile is None:
        profile = get_user_active_profile(db, user_id)

    if profile is None:
        profile = get_system_default_profile(db)

    config = normalize_vrs_config(_safe_load_config(profile.config_json))

    return {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "profile_type": profile.profile_type,
        "is_system": profile.is_system,
        "config": config,
    }


def create_custom_profile(
    db: Session,
    user_id: int,
    name: str,
    description: str | None,
    config: dict,
    activate: bool = False,
) -> VrsProcessingProfile:
    normalized = normalize_vrs_config(config)

    if activate:
        (
            db.query(VrsProcessingProfile)
            .filter(VrsProcessingProfile.user_id == user_id)
            .update({"is_active": False}, synchronize_session=False)
        )

    profile = VrsProcessingProfile(
        user_id=user_id,
        name=name.strip(),
        description=(description or "").strip() or None,
        profile_type="user_custom",
        is_system=False,
        is_active=activate,
        config_json=json.dumps(normalized, ensure_ascii=False),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def update_custom_profile(
    db: Session,
    user_id: int,
    profile_id: int,
    name: str | None = None,
    description: str | None = None,
    config: dict | None = None,
    activate: bool | None = None,
) -> VrsProcessingProfile:
    profile = (
        db.query(VrsProcessingProfile)
        .filter(
            VrsProcessingProfile.id == profile_id,
            VrsProcessingProfile.user_id == user_id,
            VrsProcessingProfile.is_system.is_(False),
        )
        .first()
    )

    if not profile:
        raise ValueError("Perfil customizado não encontrado.")

    if name is not None:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("O nome do perfil não pode ser vazio.")
        profile.name = cleaned_name

    if description is not None:
        profile.description = description.strip() or None

    if config is not None:
        profile.config_json = json.dumps(
            normalize_vrs_config(config),
            ensure_ascii=False,
        )

    if activate is True:
        (
            db.query(VrsProcessingProfile)
            .filter(VrsProcessingProfile.user_id == user_id)
            .update({"is_active": False}, synchronize_session=False)
        )
        profile.is_active = True
    elif activate is False:
        profile.is_active = False

    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def activate_profile(
    db: Session,
    user_id: int,
    profile_id: int,
) -> VrsProcessingProfile:
    profile = (
        db.query(VrsProcessingProfile)
        .filter(VrsProcessingProfile.id == profile_id)
        .first()
    )

    if not profile:
        raise ValueError("Perfil não encontrado.")

    if not profile.is_system and profile.user_id != user_id:
        raise ValueError("O perfil não pertence ao usuário.")

    (
        db.query(VrsProcessingProfile)
        .filter(VrsProcessingProfile.user_id == user_id)
        .update({"is_active": False}, synchronize_session=False)
    )

    if profile.is_system:
        clone = create_custom_profile(
            db=db,
            user_id=user_id,
            name=profile.name,
            description=profile.description,
            config=_safe_load_config(profile.config_json),
            activate=True,
        )
        return clone

    profile.is_active = True
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def get_profile_by_id_for_user(
    db: Session,
    user_id: int,
    profile_id: int,
) -> VrsProcessingProfile | None:
    profile = (
        db.query(VrsProcessingProfile)
        .filter(VrsProcessingProfile.id == profile_id)
        .first()
    )

    if not profile:
        return None

    if profile.is_system:
        return profile

    if profile.user_id != user_id:
        return None

    return profile
