from roomsd import auth, db


def test_missing_token_is_401(client):
    r = client.get("/v1/rooms")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_bad_token_is_401(client):
    r = client.get("/v1/rooms", headers={"Authorization": "Bearer rmsd_nope"})
    assert r.status_code == 401


def test_revoked_token_is_401(client, settings, boostie):
    assert client.get("/v1/rooms", headers=boostie).status_code == 200
    conn = db.connect(settings.db_path)
    try:
        assert auth.revoke_tokens(conn, "boostie") == 1
    finally:
        conn.close()
    assert client.get("/v1/rooms", headers=boostie).status_code == 401


def test_tokens_stored_hashed(client, settings, boostie):
    token = boostie["Authorization"].removeprefix("Bearer ")
    conn = db.connect(settings.db_path)
    try:
        hashes = [r[0] for r in conn.execute("select token_hash from tokens")]
    finally:
        conn.close()
    assert token not in hashes
    assert auth.hash_token(token) in hashes


def test_cannot_impersonate_on_create(client, boostie):
    r = client.post("/v1/rooms", json={"name": "x", "created_by": "missy"}, headers=boostie)
    assert r.status_code == 403


def test_cannot_impersonate_on_join(client, room_id, missy):
    r = client.post(f"/v1/rooms/{room_id}/participants", json={"agent": "boostie"}, headers=missy)
    assert r.status_code == 403


def test_cannot_impersonate_on_message(client, room_id, boostie):
    r = client.post(
        f"/v1/rooms/{room_id}/messages",
        json={"from": "claude", "body": "hi"},
        headers=boostie,
    )
    assert r.status_code == 403


def test_matching_from_is_accepted(client, room_id, boostie):
    r = client.post(
        f"/v1/rooms/{room_id}/messages",
        json={"from": "boostie", "body": "hi"},
        headers=boostie,
    )
    assert r.status_code == 201
    assert r.json()["from"] == "boostie"
