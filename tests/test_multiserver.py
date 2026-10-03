import asyncio
import json

import httpx

from roomsd import lobby_client


def create(client, headers, **body):
    r = client.post("/v1/rooms", json={"name": "r", **body}, headers=headers)
    assert r.status_code == 201
    return r.json()


def test_well_known(client):
    wk = client.get("/.well-known/roomsd").json()
    assert wk["server_id"] == "rooms-a"
    assert wk["base_url"] == "http://rooms-a.test"
    assert (wk["issuer"], wk["domain"]) == ("http://lobby.test", "test")
    assert "me.updates" in wk["features"]


def test_rooms_carry_urls_tags_and_listed(client, boostie):
    created = create(client, boostie, listed=True, tags=["project=alpha"])
    assert created["room_url"] == f"http://rooms-a.test/v1/rooms/{created['room_id']}"
    room = client.get(f"/v1/rooms/{created['room_id']}", headers=boostie).json()
    assert room["room_url"] == created["room_url"]
    assert (room["listed"], room["tags"]) == (True, ["project=alpha"])


def test_only_creator_can_patch(client, boostie, missy):
    rid = create(client, boostie)["room_id"]
    client.post(f"/v1/rooms/{rid}/participants", json={}, headers=missy)
    assert client.patch(f"/v1/rooms/{rid}", json={"listed": True}, headers=missy).status_code == 403
    r = client.patch(
        f"/v1/rooms/{rid}", json={"listed": True, "purpose": "now public"}, headers=boostie
    )
    assert r.status_code == 200
    assert (r.json()["listed"], r.json()["purpose"]) == (True, "now public")


def test_me_updates_spans_joined_rooms(client, boostie, missy):
    a = create(client, boostie, name="a")["room_id"]
    b = create(client, boostie, name="b")["room_id"]
    c = create(client, missy, name="c")["room_id"]  # boostie never joins this one
    for rid, who in [(a, boostie), (c, missy), (b, boostie), (a, boostie)]:
        client.post(f"/v1/rooms/{rid}/messages", json={"body": rid[-4:]}, headers=who)

    page = client.get("/v1/me/updates", headers=boostie).json()
    assert [m["room_id"] for m in page["messages"]] == [a, b, a]
    assert set(page["room_urls"]) == {a, b}
    assert page["room_urls"][a].endswith(f"/v1/rooms/{a}")

    first = client.get("/v1/me/updates", params={"limit": 1}, headers=boostie).json()
    rest = client.get(
        "/v1/me/updates", params={"cursor": first["next_cursor"]}, headers=boostie
    ).json()
    assert len(first["messages"]) + len(rest["messages"]) == 3
    empty = client.get(
        "/v1/me/updates", params={"cursor": rest["next_cursor"]}, headers=boostie
    ).json()
    assert (empty["messages"], empty["next_cursor"]) == ([], rest["next_cursor"])


def test_me_updates_for_invitee_sees_only_its_room(client, boostie):
    a = create(client, boostie, name="a")["room_id"]
    b = create(client, boostie, name="b")["room_id"]
    inv = client.post(f"/v1/rooms/{a}/invites", json={"name": "w"}, headers=boostie).json()
    w = {"Authorization": f"Bearer {inv['token']}"}
    client.post(f"/v1/rooms/{a}/participants", json={}, headers=w)
    client.post(f"/v1/rooms/{a}/messages", json={"body": "in a"}, headers=boostie)
    client.post(f"/v1/rooms/{b}/messages", json={"body": "in b"}, headers=boostie)
    page = client.get("/v1/me/updates", headers=w).json()
    assert [m["body"] for m in page["messages"]] == ["in a"]


class FakeLobbyd:
    """Records directory calls made by lobby_client."""

    def __init__(self, fail=False):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body or dict(request.url.params)))
        if self.fail:
            return httpx.Response(503)
        return httpx.Response(204 if request.method == "DELETE" else 200, json={})

    def run(self, settings, fn):
        async def go():
            transport = httpx.MockTransport(self.handler)
            async with httpx.AsyncClient(transport=transport, base_url="http://lobby.test") as c:
                return await fn(c, settings)

        return asyncio.run(go())


def test_listing_sync_pushes_changes_once(client, settings, boostie):
    listed = create(client, boostie, name="pub", listed=True, tags=["t"])
    create(client, boostie, name="private")  # unlisted rooms are never pushed

    lobby = FakeLobbyd()
    assert lobby.run(settings, lobby_client.sync_listings) == 1
    method, path, body = lobby.calls[0]
    assert (method, path) == ("PUT", "/v1/rooms")
    assert body == {"room_url": listed["room_url"], "name": "pub", "purpose": None, "tags": ["t"]}

    assert lobby.run(settings, lobby_client.sync_listings) == 0  # nothing new

    client.patch(f"/v1/rooms/{listed['room_id']}", json={"listed": False}, headers=boostie)
    lobby.run(settings, lobby_client.sync_listings)
    assert lobby.calls[-1][:2] == ("DELETE", "/v1/rooms")
    assert lobby.calls[-1][2] == {"room_url": listed["room_url"]}


def test_failed_sync_is_retried(client, settings, boostie):
    create(client, boostie, listed=True)
    failing = FakeLobbyd(fail=True)
    try:
        failing.run(settings, lobby_client.sync_listings)
    except httpx.HTTPStatusError:
        pass
    ok = FakeLobbyd()
    assert ok.run(settings, lobby_client.sync_listings) == 1


def test_heartbeat_payload(settings):
    lobby = FakeLobbyd()
    lobby.run(settings, lobby_client.heartbeat)
    assert lobby.calls == [
        (
            "PUT",
            "/v1/servers/roomsd/rooms-a",
            {"base_url": "http://rooms-a.test", "tags": [], "ttl_seconds": 60},
        )
    ]
