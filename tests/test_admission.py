"""Regression tests for room-o-matic/docs#10: room admission, rights, removal and bans."""

import pytest
from fastapi.testclient import TestClient

from roomsd import db
from roomsd.app import create_app
from roomsd.verify import TokenVerifier


def room(client, headers, **body):
    r = client.post("/v1/rooms", json={"name": "r", **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["room_id"]


def join(client, rid, headers):
    return client.post(f"/v1/rooms/{rid}/participants", json={}, headers=headers)


def say(client, rid, headers, body="hi"):
    return client.post(f"/v1/rooms/{rid}/messages", json={"body": body}, headers=headers)


def grant(client, rid, headers, agent, rights):
    return client.put(f"/v1/rooms/{rid}/members/{agent}", json={"rights": rights}, headers=headers)


def remove(client, rid, headers, agent, ban=False):
    return client.delete(f"/v1/rooms/{rid}/members/{agent}", params={"ban": ban}, headers=headers)


@pytest.fixture
def outsider(make_agent):
    return make_agent("outsider")


def test_unrelated_agent_cannot_self_admit_to_closed_room(client, boostie, outsider):
    rid = room(client, boostie, admission="closed")
    r = join(client, rid, outsider)
    assert r.status_code == 403 and "closed" in r.json()["detail"]
    assert client.get(f"/v1/rooms/{rid}/messages", headers=outsider).status_code == 403


def test_approved_named_peer_can_join_closed_room(client, boostie, missy):
    rid = room(client, boostie, admission="closed")
    assert grant(client, rid, boostie, "missy@test", ["read", "write"]).status_code == 200
    assert join(client, rid, missy).status_code == 200
    assert say(client, rid, missy).status_code == 201
    # but not invite: that wasn't granted
    r = client.post(f"/v1/rooms/{rid}/invites", json={"name": "w"}, headers=missy)
    assert r.status_code == 403


def test_open_room_behaviour_remains_available(client, boostie, outsider):
    rid = room(client, boostie)  # server default: open
    assert join(client, rid, outsider).status_code == 200
    assert say(client, rid, outsider).status_code == 201
    r = client.post(f"/v1/rooms/{rid}/invites", json={"name": "w"}, headers=outsider)
    assert r.status_code == 201  # default rights for an open room include invite


def test_open_room_default_rights_are_configurable(client, boostie, outsider):
    rid = room(client, boostie, default_rights=["read"])
    join(client, rid, outsider)
    assert client.get(f"/v1/rooms/{rid}/messages", headers=outsider).status_code == 200
    assert say(client, rid, outsider).status_code == 403


def test_removed_member_with_valid_jwt_cannot_read_or_rejoin_closed_room(client, boostie, missy):
    rid = room(client, boostie, admission="closed")
    grant(client, rid, boostie, "missy@test", ["read", "write"])
    join(client, rid, missy)
    assert remove(client, rid, boostie, "missy@test").status_code == 204
    # Same, still-valid lobbyd token:
    assert client.get(f"/v1/rooms/{rid}/messages", headers=missy).status_code == 403
    assert join(client, rid, missy).status_code == 403
    assert rid not in {r["id"] for r in client.get("/v1/rooms", headers=missy).json()}


def test_ban_blocks_rejoin_even_in_open_room_until_regranted(client, boostie, outsider):
    rid = room(client, boostie)
    join(client, rid, outsider)
    remove(client, rid, boostie, "outsider@test", ban=True)
    r = join(client, rid, outsider)
    assert r.status_code == 403 and "removed" in r.json()["detail"]
    grant(client, rid, boostie, "outsider@test", ["read"])  # an admin lifts the ban
    assert join(client, rid, outsider).status_code == 200


def test_removal_invalidates_delegated_capabilities(client, boostie, missy):
    rid = room(client, boostie)
    join(client, rid, missy)
    inv = client.post(f"/v1/rooms/{rid}/invites", json={"name": "helper"}, headers=missy).json()
    guest = {"Authorization": f"Bearer {inv['token']}"}
    join(client, rid, guest)
    assert say(client, rid, guest).status_code == 201
    remove(client, rid, boostie, "missy@test")
    assert client.get("/v1/auth/whoami", headers=guest).status_code == 401  # invite revoked
    agents = {
        p["agent"] for p in client.get(f"/v1/rooms/{rid}", headers=boostie).json()["participants"]
    }
    assert agents == {"boostie@test"}  # missy and her guest are gone


def test_guests_cannot_escalate_or_delegate(client, boostie):
    rid = room(client, boostie)
    inv = client.post(f"/v1/rooms/{rid}/invites", json={"name": "g"}, headers=boostie).json()
    guest = {"Authorization": f"Bearer {inv['token']}"}
    join(client, rid, guest)
    assert (
        client.post(f"/v1/rooms/{rid}/invites", json={"name": "x"}, headers=guest).status_code
        == 403
    )
    assert grant(client, rid, guest, "outsider@test", ["admin"]).status_code == 403
    assert (
        client.patch(f"/v1/rooms/{rid}", json={"admission": "open"}, headers=guest).status_code
        == 403
    )
    assert remove(client, rid, guest, "boostie@test").status_code == 403


def test_admin_rights_are_explicit_not_roles(client, boostie, missy, outsider):
    rid = room(client, boostie)
    client.post(f"/v1/rooms/{rid}/participants", json={"role": "chair"}, headers=missy)
    # an advisory "chair" role grants nothing
    assert grant(client, rid, missy, "outsider@test", ["read"]).status_code == 403
    grant(client, rid, boostie, "missy@test", ["read", "write", "admin"])
    assert grant(client, rid, missy, "outsider@test", ["read"]).status_code == 200
    assert remove(client, rid, missy, "boostie@test").status_code == 409  # creator stays


def test_evicting_a_guest(client, boostie):
    rid = room(client, boostie)
    inv = client.post(f"/v1/rooms/{rid}/invites", json={"name": "g"}, headers=boostie).json()
    guest = {"Authorization": f"Bearer {inv['token']}"}
    join(client, rid, guest)
    remove(client, rid, boostie, inv["agent"])
    assert client.get("/v1/auth/whoami", headers=guest).status_code == 401


def test_aggregate_feed_follows_grants(client, boostie, missy):
    rid = room(client, boostie)
    join(client, rid, missy)
    say(client, rid, boostie, "before")
    assert [m["body"] for m in client.get("/v1/me/updates", headers=missy).json()["messages"]] == [
        "before"
    ]
    grant(client, rid, boostie, "missy@test", ["write"])  # read withdrawn
    assert client.get("/v1/me/updates", headers=missy).json()["messages"] == []
    remove(client, rid, boostie, "missy@test")
    assert client.get("/v1/me/updates", headers=missy).json()["messages"] == []


def test_archive_and_unarchive(client, boostie):
    rid = room(client, boostie)
    client.patch(f"/v1/rooms/{rid}", json={"archived": True}, headers=boostie)
    assert say(client, rid, boostie).status_code == 409
    assert client.get(f"/v1/rooms/{rid}/messages", headers=boostie).status_code == 200
    r = client.patch(f"/v1/rooms/{rid}", json={"name": "renamed"}, headers=boostie)
    assert r.status_code == 409  # only unarchiving is allowed while archived
    client.patch(f"/v1/rooms/{rid}", json={"archived": False}, headers=boostie)
    assert say(client, rid, boostie).status_code == 201


def test_policy_changes_are_audited(client, settings, boostie, missy):
    rid = room(client, boostie, admission="closed")
    grant(client, rid, boostie, "missy@test", ["read"])
    remove(client, rid, boostie, "missy@test", ban=True)
    conn = db.connect(settings.db_path)
    actions = [r[0] for r in conn.execute("select action from audit where room_id = ?", (rid,))]
    conn.close()
    assert actions == ["room.create", "member.grant", "member.remove"]


def test_server_default_admission(settings, lobby, make_agent):
    closed = settings.__class__(**{**settings.__dict__, "default_admission": "closed"})
    verifier = TokenVerifier(
        issuer="http://lobby.test",
        domain="test",
        audience="http://rooms-a.test",
        fetch_jwks=lobby.jwks,
    )
    c = TestClient(create_app(closed, verifier=verifier))
    rid = room(c, make_agent("boostie"))
    assert c.get(f"/v1/rooms/{rid}", headers=make_agent("boostie")).json()["admission"] == "closed"
    assert join(c, rid, make_agent("outsider")).status_code == 403
