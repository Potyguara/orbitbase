from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User, UserAdvancedCredentials
from app.schemas import (
    UserAdvancedCredentialsUpsert,
    UserAdvancedCredentialsRead,
    UserAdvancedCredentialsDeleteResponse,
)

router = APIRouter(
    prefix="/users/me/advanced-credentials",
    tags=["Advanced Credentials"],
)


def _mask_username(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()
    if len(value) <= 2:
        return "*" * len(value)
    if len(value) <= 5:
        return value[:1] + "*" * (len(value) - 2) + value[-1:]

    return value[:3] + "*" * max(3, len(value) - 5) + value[-2:]


@router.get("", response_model=UserAdvancedCredentialsRead)
def get_my_advanced_credentials(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = (
        db.query(UserAdvancedCredentials)
        .filter(UserAdvancedCredentials.user_id == current_user.id)
        .first()
    )

    if not record:
        return UserAdvancedCredentialsRead(
            earthdata_configured=False,
            earthdata_username_masked=None,
            updated_at=None,
        )

    configured = bool(record.earthdata_username and record.earthdata_password)

    return UserAdvancedCredentialsRead(
        earthdata_configured=configured,
        earthdata_username_masked=_mask_username(record.earthdata_username),
        updated_at=record.updated_at,
    )


@router.put("", response_model=UserAdvancedCredentialsRead)
def upsert_my_advanced_credentials(
    payload: UserAdvancedCredentialsUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    username = payload.earthdata_username.strip()
    password = payload.earthdata_password.strip()

    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail="Usuário e senha Earthdata/CDDIS são obrigatórios.",
        )

    record = (
        db.query(UserAdvancedCredentials)
        .filter(UserAdvancedCredentials.user_id == current_user.id)
        .first()
    )

    if not record:
        record = UserAdvancedCredentials(
            user_id=current_user.id,
            earthdata_username=username,
            earthdata_password=password,
        )
    else:
        record.earthdata_username = username
        record.earthdata_password = password

    db.add(record)
    db.commit()
    db.refresh(record)

    return UserAdvancedCredentialsRead(
        earthdata_configured=True,
        earthdata_username_masked=_mask_username(record.earthdata_username),
        updated_at=record.updated_at,
    )


@router.delete("", response_model=UserAdvancedCredentialsDeleteResponse)
def delete_my_advanced_credentials(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = (
        db.query(UserAdvancedCredentials)
        .filter(UserAdvancedCredentials.user_id == current_user.id)
        .first()
    )

    if record:
        db.delete(record)
        db.commit()

    return UserAdvancedCredentialsDeleteResponse(
        ok=True,
        message="Credenciais Earthdata/CDDIS removidas com sucesso.",
    )
