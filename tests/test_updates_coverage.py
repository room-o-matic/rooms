"""/v1/me/updates covers every room a named agent can read, joined or not. Found live: a
Claude Code session granted into a room and @-mentioned there never saw the mention,
because the feed only covered rooms it had joined."""


def feed(client, headers):
    return [m["body"] for m in client.get("/v1/me/updates", headers=headers).json()["messages"]]


def room(client, owner, **kw):
    return client.post("/v1/rooms", json={"name": "r", **kw}, headers=owner).json()["room_id"]


def post(client, rid, headers, body):
    client.post(f"/v1/rooms/{rid}/messages", json={"body": body}, headers=headers)


def test_granted_but_not_joined_is_covered(client, boostie, missy):
    rid = room(client, boostie, admission="closed")
    client.put(
        f"/v1/rooms/{rid}/members/missy@test", json={"rights": ["read", "write"]}, headers=boostie
    )
    post(client, rid, boostie, "@missy can you look?")
    assert feed(client, missy) == ["@missy can you look?"]


def test_no_read_right_no_coverage(client, boostie, missy):
    rid = room(client, boostie, admission="closed")
    client.put(f"/v1/rooms/{rid}/members/missy@test", json={"rights": ["write"]}, headers=boostie)
    post(client, rid, boostie, "secret")
    assert feed(client, missy) == []


def test_open_rooms_never_joined_are_not_covered(client, boostie, missy):
    rid = room(client, boostie, admission="open")
    post(client, rid, boostie, "public chatter")
    assert feed(client, missy) == []  # no grant until you join


def test_removed_and_banned_lose_coverage(client, boostie, missy):
    rid = room(client, boostie, admission="closed")
    client.put(f"/v1/rooms/{rid}/members/missy@test", json={"rights": ["read"]}, headers=boostie)
    client.delete(f"/v1/rooms/{rid}/members/missy@test", params={"ban": True}, headers=boostie)
    post(client, rid, boostie, "after the ban")
    assert feed(client, missy) == []


def test_presence_still_only_for_joined_rooms(client, boostie, missy):
    rid = room(client, boostie, admission="closed")
    client.put(f"/v1/rooms/{rid}/members/missy@test", json={"rights": ["read"]}, headers=boostie)
    client.get("/v1/me/updates", headers=missy)
    people = client.get(f"/v1/rooms/{rid}", headers=boostie).json()["participants"]
    assert [p["agent"] for p in people] == ["boostie@test"]  # granted, not "here"
