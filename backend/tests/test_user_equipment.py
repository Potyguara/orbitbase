"""Meus equipamentos: CRUD por usuário, isolado entre contas."""
import os

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.deps import get_current_user, get_db
    from app.models import User
    from app.routers import user_equipment

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    state = {"user_id": 1}

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    def _user():
        return User(id=state["user_id"])

    app = FastAPI()
    app.include_router(user_equipment.router)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    c = TestClient(app)
    c.state = state
    return c


def test_equipment_crud_and_isolation(client):
    body = {
        "name": "HiPer SR 1064-12234", "receiver_type": "TPS HIPER_SR",
        "board_id": "O0IZ4RS5R82", "firmware": "4.7 HSR",
        "antenna_type": "TPSHIPER_SR", "antenna_serial": "1064-12234",
    }
    r = client.post("/users/me/equipment", json=body)
    assert r.status_code == 201, r.text
    eq = r.json()
    assert eq["board_id"] == "O0IZ4RS5R82"

    r = client.put(f"/users/me/equipment/{eq['id']}", json={**body, "firmware": "4.8 HSR"})
    assert r.json()["firmware"] == "4.8 HSR"
    assert [e["name"] for e in client.get("/users/me/equipment").json()] == ["HiPer SR 1064-12234"]

    client.state["user_id"] = 2
    assert client.get("/users/me/equipment").json() == []
    assert client.delete(f"/users/me/equipment/{eq['id']}").status_code == 404

    client.state["user_id"] = 1
    assert client.delete(f"/users/me/equipment/{eq['id']}").status_code == 204
    assert client.get("/users/me/equipment").json() == []


def test_equipment_requires_name(client):
    assert client.post("/users/me/equipment", json={"name": "   "}).status_code == 422
