from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_missing_token_is_401(client):
    r = client.get("/v1/rooms")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_garbage_tokens_are_401(client):
    for token in ["nope", "rmsd_nope", "a.b.c"]:
        r = client.get("/v1/rooms", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token


def test_lobbyd_token_identity(client, boostie):
    me = client.get("/v1/auth/whoami", headers=boostie).json()
    assert (me["agent"], me["scope"]) == ("boostie@test", "agent")


def test_token_for_another_server_rejected(client, lobby):
    token = lobby.token("boostie", aud="http://rooms-b.test")
    assert client.get("/v1/rooms", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_token_rejected(client, lobby):
    token = lobby.token("boostie", ttl=-120)
    assert client.get("/v1/rooms", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_forged_token_rejected(client, lobby):
    token = lobby.token("boostie", key=Ed25519PrivateKey.generate())
    assert client.get("/v1/rooms", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_non_agent_scopes_rejected(client, make_agent):
    for scope in ("agentd", "roomsd"):
        assert client.get("/v1/rooms", headers=make_agent("x", scope=scope)).status_code == 401


def test_access_tokens_cannot_self_revoke(client, boostie):
    assert client.post("/v1/auth/revoke", headers=boostie).status_code == 400


def test_cannot_impersonate_on_create(client, boostie):
    r = client.post("/v1/rooms", json={"name": "x", "created_by": "missy@test"}, headers=boostie)
    assert r.status_code == 403


def test_cannot_impersonate_on_join(client, room_id, missy):
    r = client.post(
        f"/v1/rooms/{room_id}/participants", json={"agent": "boostie@test"}, headers=missy
    )
    assert r.status_code == 403


def test_cannot_impersonate_on_message(client, room_id, boostie):
    r = client.post(
        f"/v1/rooms/{room_id}/messages",
        json={"from": "claude@test", "body": "hi"},
        headers=boostie,
    )
    assert r.status_code == 403


def test_matching_from_is_accepted(client, room_id, boostie):
    r = client.post(
        f"/v1/rooms/{room_id}/messages",
        json={"from": "boostie@test", "body": "hi"},
        headers=boostie,
    )
    assert r.status_code == 201
    assert r.json()["from"] == "boostie@test"
