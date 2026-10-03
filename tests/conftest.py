import time
import uuid
from collections.abc import Callable

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from roomsd import db
from roomsd.app import create_app
from roomsd.config import Settings
from roomsd.verify import TokenVerifier

ISSUER = "http://lobby.test"
DOMAIN = "test"
BASE_URL = "http://rooms-a.test"


class FakeLobby:
    """Stands in for lobbyd: signs access tokens with a test key and serves its JWKS."""

    kid = "test-key"

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()

    def jwks(self) -> dict:
        jwk = jwt.algorithms.OKPAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "alg": "EdDSA", "use": "sig"}]}

    def token(self, name: str, *, scope="agent", aud=BASE_URL, ttl=900, key=None) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": f"{name}@{DOMAIN}",
            "aud": aud,
            "scope": scope,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, key or self.key, algorithm="EdDSA", headers={"kid": self.kid})


@pytest.fixture
def lobby() -> FakeLobby:
    return FakeLobby()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        data_dir=tmp_path,
        server_id="rooms-a",
        base_url=BASE_URL,
        lobbyd_url=ISSUER,
        lobbyd_domain=DOMAIN,
        max_message_bytes=1024,
        max_note_bytes=1024,
    )
    db.init_db(s.db_path)
    return s


@pytest.fixture
def client(settings, lobby) -> TestClient:
    verifier = TokenVerifier(issuer=ISSUER, domain=DOMAIN, audience=BASE_URL, fetch_jwks=lobby.jwks)
    return TestClient(create_app(settings, verifier=verifier))


@pytest.fixture
def make_agent(lobby) -> Callable[..., dict[str, str]]:
    """Authorization headers carrying a lobbyd access token for `name@test`."""

    def _make(name: str, **kw) -> dict[str, str]:
        return {"Authorization": f"Bearer {lobby.token(name, **kw)}"}

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
