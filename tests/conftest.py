from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from roomsd import auth, db
from roomsd.app import create_app
from roomsd.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, max_message_bytes=1024, max_note_bytes=1024)


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def make_agent(settings) -> Callable[[str], dict[str, str]]:
    """Issue a token for an agent and return its Authorization headers."""

    def _make(agent: str) -> dict[str, str]:
        conn = db.connect(settings.db_path)
        try:
            token = auth.create_token(conn, agent)
        finally:
            conn.close()
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def boostie(make_agent):
    return make_agent("boostie")


@pytest.fixture
def missy(make_agent):
    return make_agent("missy")


@pytest.fixture
def room_id(client, boostie) -> str:
    r = client.post("/v1/rooms", json={"name": "roomsd-design"}, headers=boostie)
    assert r.status_code == 201
    return r.json()["room_id"]
