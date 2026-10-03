"""Regression tests for room-o-matic/docs#1: invite tokens must stay inside one room."""

from roomsd import db


def make_room(client, headers, name, purpose=None):
    r = client.post("/v1/rooms", json={"name": name, "purpose": purpose}, headers=headers)
    return r.json()["room_id"]


def invite(client, room_id, headers, name, **kw):
    return client.post(f"/v1/rooms/{room_id}/invites", json={"name": name, **kw}, headers=headers)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def join(client, room_id, headers):
    assert (
        client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=headers).status_code
        == 200
    )


def revoke(client, room_id, invite_id, headers):
    r = client.delete(f"/v1/rooms/{room_id}/invites/{invite_id}", headers=headers)
    assert r.status_code == 200


def test_same_name_cannot_be_live_in_two_rooms(client, boostie):
    a, b = make_room(client, boostie, "A"), make_room(client, boostie, "B")
    assert invite(client, a, boostie, "codex").status_code == 201
    r = invite(client, b, boostie, "codex")
    assert r.status_code == 409
    assert "already has a live invite" in r.json()["detail"]


def test_stale_membership_in_other_room_never_leaks(client, boostie):
    """The issue's repro: identity has persisted membership in B after B's invite is
    revoked; a new invite in A for the same identity must not see B."""
    a, b = make_room(client, boostie, "A"), make_room(client, boostie, "B", purpose="secret")
    inv_b = invite(client, b, boostie, "codex").json()
    join(client, b, auth(inv_b["token"]))
    revoke(client, b, inv_b["invite_id"], boostie)
    client.post(f"/v1/rooms/{b}/messages", json={"body": "SECRET-B"}, headers=boostie)

    inv_a = invite(client, a, boostie, "codex").json()  # allowed: B's invite is dead
    assert inv_a["agent"] == inv_b["agent"]  # same identity, persisted membership in B
    ha = auth(inv_a["token"])
    join(client, a, ha)
    client.post(f"/v1/rooms/{a}/messages", json={"body": "hello A"}, headers=boostie)

    feed = client.get("/v1/me/updates", headers=ha).json()
    assert [m["body"] for m in feed["messages"]] == ["hello A"]
    assert set(feed["room_urls"]) == {a}
    assert [r["id"] for r in client.get("/v1/rooms", headers=ha).json()] == [a]
    assert client.get(f"/v1/rooms/{b}/messages", headers=ha).status_code == 403
    assert client.get("/v1/auth/whoami", headers=auth(inv_b["token"])).status_code == 401


def test_expired_invite_frees_the_name(client, settings, boostie):
    a, b = make_room(client, boostie, "A"), make_room(client, boostie, "B")
    old = invite(client, b, boostie, "codex").json()
    conn = db.connect(settings.db_path)
    with conn:
        conn.execute(
            "update invites set expires_at = '2000-01-01T00:00:00.000Z' where invite_id = ?",
            (old["invite_id"],),
        )
    conn.close()
    assert invite(client, a, boostie, "codex").status_code == 201


def test_same_room_sessions_need_distinct_names(client, boostie):
    """Sibling repro: two simultaneous same-room invites used to share one participant."""
    a = make_room(client, boostie, "A")
    first = invite(client, a, boostie, "codex").json()
    assert invite(client, a, boostie, "codex").status_code == 409
    second = invite(client, a, boostie, "codex-2").json()
    assert first["agent"] != second["agent"]

    h2 = auth(second["token"])
    # The second guest hasn't joined, and can't ride on the first guest's membership.
    r = client.post(f"/v1/rooms/{a}/messages", json={"body": "x"}, headers=h2)
    assert r.status_code == 403
    join(client, a, auth(first["token"]))
    join(client, a, h2)
    participants = {
        p["agent"] for p in client.get(f"/v1/rooms/{a}", headers=boostie).json()["participants"]
    }
    assert {first["agent"], second["agent"]} <= participants


def test_credential_rotation_keeps_stable_guest_membership(client, boostie):
    """Renewal: revoke then re-invite the same name in the same room."""
    a = make_room(client, boostie, "A")
    old = invite(client, a, boostie, "codex", role="reviewer").json()
    join(client, a, auth(old["token"]))
    revoke(client, a, old["invite_id"], boostie)
    new = invite(client, a, boostie, "codex", role="reviewer").json()
    assert new["agent"] == old["agent"]
    hn = auth(new["token"])
    # Membership persisted, so the renewed credential can post without re-joining.
    assert (
        client.post(f"/v1/rooms/{a}/messages", json={"body": "back"}, headers=hn).status_code == 201
    )
    assert client.get("/v1/auth/whoami", headers=auth(old["token"])).status_code == 401


def test_message_audit_records_invite(client, settings, boostie):
    a = make_room(client, boostie, "A")
    inv = invite(client, a, boostie, "codex").json()
    h = auth(inv["token"])
    join(client, a, h)
    client.post(f"/v1/rooms/{a}/messages", json={"body": "x"}, headers=h)
    conn = db.connect(settings.db_path)
    detail = conn.execute("select detail_json from audit where action = 'message.post'").fetchone()[
        0
    ]
    conn.close()
    assert inv["invite_id"] in detail


def test_named_agent_aggregate_feed_unchanged(client, boostie):
    a, b = make_room(client, boostie, "A"), make_room(client, boostie, "B")
    for rid in (a, b):
        client.post(f"/v1/rooms/{rid}/messages", json={"body": rid}, headers=boostie)
    feed = client.get("/v1/me/updates", headers=boostie).json()
    assert {m["room_id"] for m in feed["messages"]} == {a, b}
    assert {r["id"] for r in client.get("/v1/rooms", headers=boostie).json()} == {a, b}
