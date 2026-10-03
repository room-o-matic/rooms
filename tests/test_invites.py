import pytest

from roomsd import db


def invite(client, room_id, headers, **body):
    return client.post(f"/v1/rooms/{room_id}/invites", json=body, headers=headers)


@pytest.fixture
def worker(client, room_id, boostie):
    """A minted invite for 'boostie/codex-1' plus its auth headers."""
    r = invite(client, room_id, boostie, name="codex-1", role="implementer")
    assert r.status_code == 201
    body = r.json()
    return body, {"Authorization": f"Bearer {body['token']}"}


def test_invite_identity_is_namespaced_under_inviter(worker, room_id):
    body, _ = worker
    assert body["agent"] == "boostie/codex-1"
    assert body["room_id"] == room_id
    assert body["role"] == "implementer"
    assert body["invite_id"].startswith("inv_")


def test_invitee_joins_and_posts_with_granted_role(client, room_id, worker):
    _, headers = worker
    r = client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=headers)
    assert r.status_code == 200
    assert (r.json()["agent"], r.json()["role"]) == ("boostie/codex-1", "implementer")

    r = client.post(f"/v1/rooms/{room_id}/messages", json={"body": "on it"}, headers=headers)
    assert r.status_code == 201
    assert r.json()["from"] == "boostie/codex-1"


def test_invitee_cannot_change_role(client, room_id, worker):
    _, headers = worker
    r = client.post(f"/v1/rooms/{room_id}/participants", json={"role": "chair"}, headers=headers)
    assert r.status_code == 403


def test_invitee_cannot_impersonate(client, room_id, worker):
    _, headers = worker
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=headers)
    r = client.post(
        f"/v1/rooms/{room_id}/messages", json={"from": "boostie", "body": "x"}, headers=headers
    )
    assert r.status_code == 403


def test_invitee_confined_to_its_room(client, room_id, boostie, worker):
    _, headers = worker
    other = client.post("/v1/rooms", json={"name": "other"}, headers=boostie).json()["room_id"]
    assert (
        client.post(f"/v1/rooms/{other}/participants", json={}, headers=headers).status_code == 403
    )
    assert client.get(f"/v1/rooms/{other}/messages", headers=headers).status_code == 403


def test_invitee_cannot_create_rooms_invites_or_use_registry(client, room_id, worker):
    _, headers = worker
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=headers)
    assert client.post("/v1/rooms", json={"name": "x"}, headers=headers).status_code == 403
    assert invite(client, room_id, headers, name="sub").status_code == 403
    assert client.get("/v1/registry/agentd", headers=headers).status_code == 403


def test_only_participants_can_invite(client, room_id, missy):
    assert invite(client, room_id, missy, name="codex-1").status_code == 403


def test_invalid_name_and_ttl_rejected(client, room_id, boostie):
    assert invite(client, room_id, boostie, name="Bad/Name").status_code == 422
    assert invite(client, room_id, boostie, name="w", ttl_seconds=10).status_code == 422
    assert invite(client, room_id, boostie, name="w", ttl_seconds=10**7).status_code == 422


def test_expired_invite_is_401(client, settings, room_id, worker):
    body, headers = worker
    conn = db.connect(settings.db_path)
    with conn:
        conn.execute(
            "update tokens set expires_at = '2000-01-01T00:00:00.000Z' where invite_id = ?",
            (body["invite_id"],),
        )
    conn.close()
    assert client.get("/v1/auth/whoami", headers=headers).status_code == 401


def test_inviter_can_list_and_revoke(client, room_id, boostie, worker):
    body, headers = worker
    listed = client.get(f"/v1/rooms/{room_id}/invites", headers=boostie).json()
    assert [i["invite_id"] for i in listed] == [body["invite_id"]]
    assert "token" not in listed[0]

    r = client.delete(f"/v1/rooms/{room_id}/invites/{body['invite_id']}", headers=boostie)
    assert r.status_code == 200
    assert r.json()["revoked_at"] is not None
    assert client.get("/v1/auth/whoami", headers=headers).status_code == 401


def test_other_participant_cannot_revoke(client, room_id, missy, worker):
    body, _ = worker
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    r = client.delete(f"/v1/rooms/{room_id}/invites/{body['invite_id']}", headers=missy)
    assert r.status_code == 403


def test_room_creator_can_revoke_others_invites(client, room_id, boostie, missy):
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    inv = invite(client, room_id, missy, name="helper").json()
    r = client.delete(f"/v1/rooms/{room_id}/invites/{inv['invite_id']}", headers=boostie)
    assert r.status_code == 200


def test_whoami_and_self_revoke(client, room_id, worker):
    body, headers = worker
    me = client.get("/v1/auth/whoami", headers=headers).json()
    assert me["scope"] == "invite"
    assert me["agent"] == "boostie/codex-1"
    assert me["room_id"] == room_id
    assert me["expires_at"] == body["expires_at"]

    assert client.post("/v1/auth/revoke", headers=headers).status_code == 204
    assert client.get("/v1/auth/whoami", headers=headers).status_code == 401
