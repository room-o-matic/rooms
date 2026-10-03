from roomsd import db


def test_create_room_autojoins_creator(client, room_id, boostie):
    r = client.get(f"/v1/rooms/{room_id}", headers=boostie)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "roomsd-design"
    assert body["created_by"] == "boostie"
    assert [p["agent"] for p in body["participants"]] == ["boostie"]
    assert room_id.startswith("room_") and len(room_id) == len("room_") + 26


def test_list_rooms_only_shows_joined(client, room_id, boostie, missy):
    assert [r["id"] for r in client.get("/v1/rooms", headers=boostie).json()] == [room_id]
    assert client.get("/v1/rooms", headers=missy).json() == []


def test_join_sets_and_keeps_role(client, room_id, missy):
    url = f"/v1/rooms/{room_id}/participants"
    r = client.post(url, json={"agent": "missy", "role": "implementer"}, headers=missy)
    assert r.status_code == 200
    assert r.json()["role"] == "implementer"
    # Re-joining without a role keeps the existing one.
    assert client.post(url, json={}, headers=missy).json()["role"] == "implementer"


def test_unknown_room_is_404(client, boostie):
    assert client.get("/v1/rooms/room_nope", headers=boostie).status_code == 404
    r = client.post("/v1/rooms/room_nope/participants", json={}, headers=boostie)
    assert r.status_code == 404


def test_non_participant_is_403(client, room_id, missy):
    assert client.get(f"/v1/rooms/{room_id}", headers=missy).status_code == 403
    assert client.get(f"/v1/rooms/{room_id}/messages", headers=missy).status_code == 403
    assert client.get(f"/v1/rooms/{room_id}/notes", headers=missy).status_code == 403


def test_archived_room_rejects_writes(client, settings, room_id, boostie):
    conn = db.connect(settings.db_path)
    with conn:
        conn.execute("update rooms set archived_at = 'now' where id = ?", (room_id,))
    conn.close()
    r = client.post(f"/v1/rooms/{room_id}/messages", json={"body": "hi"}, headers=boostie)
    assert r.status_code == 409
    # Reads still work.
    assert client.get(f"/v1/rooms/{room_id}/messages", headers=boostie).status_code == 200


def test_actions_are_audited(client, settings, room_id, boostie, missy):
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    client.post(f"/v1/rooms/{room_id}/messages", json={"body": "hi"}, headers=missy)
    client.put(f"/v1/rooms/{room_id}/notes/summary", json={"value": "s"}, headers=missy)
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute("select agent, action from audit order by id").fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [
        ("boostie", "room.create"),
        ("missy", "room.join"),
        ("missy", "message.post"),
        ("missy", "note.put"),
    ]
