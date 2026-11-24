from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.models import User, UserEquipment

router = APIRouter(prefix="/users/me/equipment", tags=["Meus equipamentos"])

MAX_EQUIPMENT_PER_USER = 50


class EquipmentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    receiver_type: str | None = Field(default=None, max_length=40)
    board_id: str | None = Field(default=None, max_length=40)
    firmware: str | None = Field(default=None, max_length=40)
    antenna_type: str | None = Field(default=None, max_length=40)
    antenna_serial: str | None = Field(default=None, max_length=40)
    label_serial: str | None = Field(default=None, max_length=40)


class EquipmentRead(EquipmentPayload):
    id: int


FIELDS = tuple(EquipmentPayload.model_fields)


def _clean(payload: EquipmentPayload) -> dict:
    data = {}
    for key in FIELDS:
        value = getattr(payload, key)
        value = " ".join(str(value).split()) if value is not None else ""
        data[key] = value or None
    if not data["name"]:
        raise HTTPException(status_code=422, detail="Informe um apelido para o equipamento.")
    return data


def _read(item: UserEquipment) -> EquipmentRead:
    return EquipmentRead(id=item.id, **{k: getattr(item, k) for k in FIELDS})


def _own(db: Session, user: User, equipment_id: int) -> UserEquipment:
    item = (
        db.query(UserEquipment)
        .filter(UserEquipment.id == equipment_id, UserEquipment.user_id == user.id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Equipamento não encontrado.")
    return item


@router.get("", response_model=list[EquipmentRead])
def list_equipment(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    items = (
        db.query(UserEquipment)
        .filter(UserEquipment.user_id == current_user.id)
        .order_by(UserEquipment.name)
        .all()
    )
    return [_read(i) for i in items]


@router.post("", response_model=EquipmentRead, status_code=201)
def create_equipment(
    payload: EquipmentPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    count = db.query(UserEquipment).filter(UserEquipment.user_id == current_user.id).count()
    if count >= MAX_EQUIPMENT_PER_USER:
        raise HTTPException(status_code=400, detail="Limite de equipamentos atingido.")
    item = UserEquipment(user_id=current_user.id, **_clean(payload))
    db.add(item)
    db.commit()
    db.refresh(item)
    return _read(item)


@router.put("/{equipment_id}", response_model=EquipmentRead)
def update_equipment(
    equipment_id: int,
    payload: EquipmentPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = _own(db, current_user, equipment_id)
    for key, value in _clean(payload).items():
        setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return _read(item)


@router.delete("/{equipment_id}", status_code=204)
def delete_equipment(
    equipment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = _own(db, current_user, equipment_id)
    db.delete(item)
    db.commit()
