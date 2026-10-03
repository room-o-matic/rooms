"""Regression tests for room-o-matic/docs#23: what last_seen_at means, that the aggregate
feed refreshes it (scoped exactly like the feed), and that availability is not membership."""

import dataclasses
import sqlite3

import pytest
from conftest import BASE_URL, DOMAIN, ISSUER
from fastapi.testclient import TestClient

from roomsd.app import create_app
from roomsd.verify import TokenVerifier

OLD = "2000-01-01T00:00:00.000Z"


def db(settings):
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def age(settings, room_id=None, agent=None):
    """Make presence look stale (all participants, or one)."""
    with db(settings) as conn:
        conn.execute(
            "update participants set last_seen_at = ?"
            " where (? is null or room_id = ?) and (? is null or agent = ?)",
            (OLD, room_id, room_id, agent, agent),
        )


def seen(settings, room_id, agent):
    with db(settings) as conn:
        return conn.execute(
            "select last_seen_at from participants where room_id = ? and agent = ?",
            (room_id, agent),
        ).fetchone()[0]


def presence(client, room_id, h, agent):
    detail = client.get(f"/v1/rooms/{room_id}", headers=h).json()
    return next(p for p in detail["participants"] if p["agent"] == agent)


def invite(client, room_id, h, name, **kw):
    r = client.post(f"/v1/rooms/{room_id}/invites", json={"name": name, **kw}, headers=h)
    assert r.status_code == 201, r.text
    inv = r.json()
    g = {"Authorization": f"Bearer {inv['token']}"}
    assert client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=g).status_code == 200
    return inv, g


def test_empty_aggregate_poll_refreshes(client, settings, room_id, boostie):
    age(settings)
    page = client.get("/v1/me/updates", params={"cursor": 10**9}, headers=boostie).json()
    assert page["messages"] == []
    assert seen(settings, room_id, "boostie@test") > OLD


def test_nonempty_aggregate_poll_refreshes(client, settings, room_id, boostie, missy):
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    client.post(f"/v1/rooms/{room_id}/messages", json={"body": "hi"}, headers=boostie)
    age(settings)
    page = client.get("/v1/me/updates", headers=missy).json()
    assert [m["body"] for m in page["messages"]] == ["hi"]
    assert seen(settings, room_id, "missy@test") > OLD
    assert seen(settings, room_id, "boostie@test") == OLD  # only the poller


def test_direct_read_refreshes(client, settings, room_id, boostie):
    age(settings)
    client.get(f"/v1/rooms/{room_id}/messages", headers=boostie)
    assert seen(settings, room_id, "boostie@test") > OLD


def test_availability_follows_ttl(client, settings, room_id, boostie, missy):
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    assert presence(client, room_id, boostie, "missy@test")["available"] is True
    age(settings, agent="missy@test")
    p = presence(client, room_id, boostie, "missy@test")
    assert p["available"] is False and p["last_seen_at"] == OLD  # still a member
    client.get("/v1/me/updates", headers=missy)
    assert presence(client, room_id, boostie, "missy@test")["available"] is True


def test_presence_ttl_is_configurable(settings, lobby, boostie):
    s = dataclasses.replace(settings, presence_ttl_seconds=0)
    verifier = TokenVerifier(issuer=ISSUER, domain=DOMAIN, audience=BASE_URL, fetch_jwks=lobby.jwks)
    c = TestClient(create_app(s, verifier=verifier))
    rid = c.post("/v1/rooms", json={"name": "ttl"}, headers=boostie).json()["room_id"]
    assert presence(c, rid, boostie, "boostie@test")["available"] is False


def test_invite_poll_refreshes_only_its_room(client, settings, room_id, boostie):
    other = client.post("/v1/rooms", json={"name": "other"}, headers=boostie).json()["room_id"]
    inv_there, _ = invite(client, other, boostie, "w")
    client.delete(f"/v1/rooms/{other}/invites/{inv_there['invite_id']}", headers=boostie)
    # same guest identity reissued for this room; its stale membership there remains
    inv_here, g_here = invite(client, room_id, boostie, "w")
    agent = inv_here["agent"]
    assert agent == inv_there["agent"]
    age(settings)
    client.get("/v1/me/updates", headers=g_here)
    assert seen(settings, room_id, agent) > OLD
    assert seen(settings, other, agent) == OLD  # no refresh through a same-name collision
    assert presence(client, other, boostie, agent)["available"] is False


@pytest.mark.parametrize("ending", ["revoked", "expired"])
def test_ended_guest_cannot_renew_and_is_unavailable(client, settings, room_id, boostie, ending):
    inv, g = invite(client, room_id, boostie, "w")
    assert presence(client, room_id, boostie, inv["agent"])["available"] is True
    if ending == "revoked":
        client.delete(f"/v1/rooms/{room_id}/invites/{inv['invite_id']}", headers=boostie)
    else:
        with db(settings) as conn:
            conn.execute(
                "update invites set expires_at = ? where invite_id = ?", (OLD, inv["invite_id"])
            )
    # recently seen, but the invite is gone: not available, immediately
    assert presence(client, room_id, boostie, inv["agent"])["available"] is False
    age(settings, agent=inv["agent"])
    assert client.get("/v1/me/updates", headers=g).status_code == 401
    assert client.get(f"/v1/rooms/{room_id}/messages", headers=g).status_code == 401
    assert seen(settings, room_id, inv["agent"]) == OLD  # could not renew


def test_multiple_sessions_of_one_named_principal(client, settings, room_id, boostie, make_agent):
    other_session = make_agent("boostie")  # a second token for the same principal
    age(settings)
    client.get("/v1/me/updates", headers=other_session)
    assert presence(client, room_id, boostie, "boostie@test")["available"] is True
    # one presence per principal: whichever session polled most recently counts
    before = seen(settings, room_id, "boostie@test")
    client.get("/v1/me/updates", headers=boostie)
    assert seen(settings, room_id, "boostie@test") >= before
