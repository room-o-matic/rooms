from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from roomsd import auth, db
from roomsd.app import create_app
from roomsd.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path, max_message_bytes=1024, max_note_bytes=1024)
    db.init_db(s.db_path)
    return s


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def make_agent(settings) -> Callable[..., dict[str, str]]:
    """Issue a token for an agent and return its Authorization headers."""

    def _make(agent: str, scope: auth.Scope = "agent") -> dict[str, str]:
        conn = db.connect(settings.db_path)
        try:
            token = auth.create_token(conn, agent, scope)
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


@pytest.fixture
def agentd1(make_agent):
    return make_agent("agentd-host1", "agentd")
