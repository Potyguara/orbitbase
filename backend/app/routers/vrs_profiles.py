from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User
from app.schemas import (
    VrsProfileCreate,
    VrsProfileRead,
    VrsProfileUpdate,
)
from app.services.vrs_profile_service import (
    activate_profile,
    ensure_system_default_profile,
    get_profile_by_id_for_user,
    get_system_default_profile,
    get_user_active_profile,
    list_profiles_for_user,
    serialize_profile,
    create_custom_profile,
    update_custom_profile,
)

router = APIRouter(prefix="/vrs-profiles", tags=["vrs_profiles"])


@router.get("", response_model=list[VrsProfileRead])
def list_vrs_profiles(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        ensure_system_default_profile(db)
        profiles = list_profiles_for_user(db, current_user.id)
        return [serialize_profile(profile) for profile in profiles]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao listar perfis VRS: {str(exc)}",
        )


@router.get("/active", response_model=VrsProfileRead)
def get_active_vrs_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        ensure_system_default_profile(db)

        profile = get_user_active_profile(db, current_user.id)
        if profile is None:
            profile = get_system_default_profile(db)

        return serialize_profile(profile)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao obter perfil ativo: {str(exc)}",
        )


@router.get("/{profile_id}", response_model=VrsProfileRead)
def get_vrs_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        ensure_system_default_profile(db)

        profile = get_profile_by_id_for_user(db, current_user.id, profile_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Perfil VRS não encontrado.",
            )

        return serialize_profile(profile)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao buscar perfil VRS: {str(exc)}",
        )


@router.post("", response_model=VrsProfileRead, status_code=status.HTTP_201_CREATED)
def create_vrs_profile(
    payload: VrsProfileCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        profile = create_custom_profile(
            db=db,
            user_id=current_user.id,
            name=payload.name,
            description=payload.description,
            config=(
                payload.config.model_dump()
                if hasattr(payload.config, "model_dump")
                else payload.config
            ),
            activate=payload.activate,
        )
        return serialize_profile(profile)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao criar perfil VRS: {str(exc)}",
        )


@router.put("/{profile_id}", response_model=VrsProfileRead)
def update_vrs_profile(
    profile_id: int,
    payload: VrsProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        config_payload = payload.config
        if hasattr(config_payload, "model_dump"):
            config_payload = config_payload.model_dump()

        profile = update_custom_profile(
            db=db,
            user_id=current_user.id,
            profile_id=profile_id,
            name=payload.name,
            description=payload.description,
            config=config_payload,
            activate=None,
        )
        return serialize_profile(profile)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao atualizar perfil VRS: {str(exc)}",
        )


@router.post("/{profile_id}/activate", response_model=VrsProfileRead)
def activate_vrs_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        profile = activate_profile(
            db=db,
            user_id=current_user.id,
            profile_id=profile_id,
        )
        return serialize_profile(profile)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao ativar perfil VRS: {str(exc)}",
        )
