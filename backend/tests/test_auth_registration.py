import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers import auth
from app.schemas import UserCreate


def test_self_registration_is_blocked_by_default(monkeypatch):
    monkeypatch.setattr(settings, "allow_self_registration", False)
    payload = UserCreate(name="Fulano", email="fulano@exemplo.com", password="123456")
    with pytest.raises(HTTPException) as exc:
        auth.register(payload, db=None)  # bloqueia antes de tocar no banco
    assert exc.value.status_code == 403
