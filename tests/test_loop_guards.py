"""Regression tests for room-o-matic/docs#16 (room side): structured recipients, reply
chains with a depth cap, a per-room message budget, and an owner stop switch."""

import pytest


@pytest.fixture
def room_id(client, boostie, missy):
    rid = client.post("/v1/rooms", json={"name": "r", "max_hops": 3}, headers=boostie).json()[
        "room_id"
    ]
    client.post(f"/v1/rooms/{rid}/participants", json={}, headers=missy)
    return rid


def post(client, rid, headers, body="x", **kw):
    return client.post(f"/v1/rooms/{rid}/messages", json={"body": body, **kw}, headers=headers)


def test_recipients_and_reply_chains(client, room_id, boostie, missy):
    q = post(client, room_id, boostie, "review?", type="question", to=["missy@test"]).json()
    assert q["to"] == ["missy@test"] and q.get("hop") is None
    a = post(client, room_id, missy, "looks good", type="answer", in_reply_to=q["id"]).json()
    assert (a["in_reply_to"], a["hop"]) == (q["id"], 1)


def test_two_agent_reply_loop_is_cut_off(client, room_id, boostie, missy):
    last = post(client, room_id, boostie, "ping", to=["missy@test"]).json()
    who = [missy, boostie]
    hops = []
    for i in range(10):  # two agents dutifully replying to each other
        r = post(client, room_id, who[i % 2], f"ack {i}", in_reply_to=last["id"])
        if r.status_code != 201:
            assert r.status_code == 409 and "max_hops" in r.json()["detail"]
            break
        last = r.json()
        hops.append(last["hop"])
    assert hops == [1, 2, 3]  # max_hops=3: the 4th reply is refused


def test_in_reply_to_must_be_in_this_room(client, room_id, boostie):
    other = client.post("/v1/rooms", json={"name": "o"}, headers=boostie).json()["room_id"]
    foreign = post(client, other, boostie).json()["id"]
    assert post(client, room_id, boostie, in_reply_to=foreign).status_code == 422


def test_owner_can_pause_and_drain(client, room_id, boostie, missy):
    client.patch(f"/v1/rooms/{room_id}", json={"paused": True}, headers=boostie)
    r = post(client, room_id, missy)
    assert r.status_code == 423 and "paused" in r.json()["detail"]
    assert post(client, room_id, boostie, "draining, back soon").status_code == 201  # admin
    client.patch(f"/v1/rooms/{room_id}", json={"paused": False}, headers=boostie)
    assert post(client, room_id, missy).status_code == 201


def test_room_message_budget_covers_all_non_admins(client, boostie, missy, make_agent):
    rid = client.post(
        "/v1/rooms", json={"name": "r", "message_rate_per_minute": 3}, headers=boostie
    ).json()["room_id"]
    odin = make_agent("odin")
    for h in (missy, odin):
        client.post(f"/v1/rooms/{rid}/participants", json={}, headers=h)
    codes = [post(client, rid, h).status_code for h in (missy, odin, missy, odin)]
    assert codes == [201, 201, 201, 429]  # shared across participants
    r = post(client, rid, odin)
    assert r.headers["retry-after"] == "60"
    assert post(client, rid, boostie, "owner still talks").status_code == 201


def test_room_settings_visible_and_patchable(client, room_id, boostie):
    room = client.get(f"/v1/rooms/{room_id}", headers=boostie).json()
    assert (room["max_hops"], room["paused"], room["message_rate_per_minute"]) == (3, False, None)
    r = client.patch(
        f"/v1/rooms/{room_id}", json={"max_hops": 5, "message_rate_per_minute": 10}, headers=boostie
    ).json()
    assert (r["max_hops"], r["message_rate_per_minute"]) == (5, 10)
