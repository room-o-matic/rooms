from roomsd import db


def test_create_room_autojoins_creator(client, room_id, boostie):
    r = client.get(f"/v1/rooms/{room_id}", headers=boostie)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "roomsd-design"
    assert body["created_by"] == "boostie@test"
    assert [p["agent"] for p in body["participants"]] == ["boostie@test"]
    assert room_id.startswith("room_") and len(room_id) == len("room_") + 26


def test_list_rooms_only_shows_joined(client, room_id, boostie, missy):
    assert [r["id"] for r in client.get("/v1/rooms", headers=boostie).json()] == [room_id]
    assert client.get("/v1/rooms", headers=missy).json() == []


def test_join_sets_and_keeps_role(client, room_id, missy):
    url = f"/v1/rooms/{room_id}/participants"
    r = client.post(url, json={"agent": "missy@test", "role": "implementer"}, headers=missy)
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
        ("boostie@test", "room.create"),
        ("missy@test", "room.join"),
        ("missy@test", "message.post"),
        ("missy@test", "note.put"),
    ]


def test_request_connection_can_change_threads(settings):
    """Regression: FastAPI enters get_conn in one threadpool thread and runs the route in
    another; under concurrent load sqlite3 used to raise ProgrammingError."""
    import threading

    conn = db.connect(settings.db_path)
    result = []
    t = threading.Thread(target=lambda: result.append(conn.execute("select 1").fetchone()[0]))
    t.start()
    t.join()
    conn.close()
    assert result == [1]


def test_concurrent_requests(client, room_id, boostie):
    from concurrent.futures import ThreadPoolExecutor

    def post(i):
        return client.post(
            f"/v1/rooms/{room_id}/messages", json={"body": f"m{i}"}, headers=boostie
        ).status_code

    with ThreadPoolExecutor(8) as pool:
        assert set(pool.map(post, range(40))) == {201}
