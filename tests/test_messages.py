def post(client, room_id, headers, **body):
    return client.post(f"/v1/rooms/{room_id}/messages", json=body, headers=headers)


def read(client, room_id, headers, **params):
    return client.get(f"/v1/rooms/{room_id}/messages", params=params, headers=headers)


def test_typed_message_round_trip(client, room_id, boostie):
    r = post(
        client,
        room_id,
        boostie,
        type="proposal",
        topic="storage",
        body="Use SQLite for v1.",
        confidence=0.85,
        reply_requested=True,
        based_on_messages=[1, 2],
    )
    assert r.status_code == 201
    msg = r.json()
    assert msg["from"] == "boostie@test"
    assert msg["type"] == "proposal"
    assert msg["confidence"] == 0.85
    assert msg["reply_requested"] is True
    assert msg["based_on_messages"] == [1, 2]
    assert read(client, room_id, boostie).json()["messages"] == [msg]


def test_unknown_type_rejected(client, room_id, boostie):
    assert post(client, room_id, boostie, type="shout", body="hi").status_code == 422


def test_confidence_bounds(client, room_id, boostie):
    assert post(client, room_id, boostie, body="hi", confidence=1.5).status_code == 422


def test_oversized_body_rejected(client, room_id, boostie):
    r = post(client, room_id, boostie, body="x" * 1025)
    assert r.status_code == 413


def test_must_join_before_posting(client, room_id, missy):
    assert post(client, room_id, missy, body="hi").status_code == 403
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    assert post(client, room_id, missy, body="hi").status_code == 201


def test_polling_resumes_from_after_id(client, room_id, boostie):
    ids = [post(client, room_id, boostie, body=f"m{i}").json()["id"] for i in range(5)]
    assert ids == sorted(ids)

    page = read(client, room_id, boostie, after_id=0, limit=2).json()
    assert [m["body"] for m in page["messages"]] == ["m0", "m1"]
    assert page["latest_message_id"] == ids[1]

    page = read(client, room_id, boostie, after_id=page["latest_message_id"]).json()
    assert [m["body"] for m in page["messages"]] == ["m2", "m3", "m4"]

    empty = read(client, room_id, boostie, after_id=ids[-1]).json()
    assert empty["messages"] == []
    assert empty["latest_message_id"] == ids[-1]


def test_messages_scoped_to_room(client, room_id, boostie):
    other = client.post("/v1/rooms", json={"name": "other"}, headers=boostie).json()["room_id"]
    post(client, other, boostie, body="elsewhere")
    post(client, room_id, boostie, body="here")
    assert [m["body"] for m in read(client, room_id, boostie).json()["messages"]] == ["here"]
