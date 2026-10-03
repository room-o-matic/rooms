def put(client, room_id, headers, key, value):
    return client.put(f"/v1/rooms/{room_id}/notes/{key}", json={"value": value}, headers=headers)


def test_put_and_get_note(client, room_id, boostie):
    value = {"summary": "SSE preferred; polling fallback."}
    r = put(client, room_id, boostie, "summary", value)
    assert r.status_code == 200
    assert r.json()["updated_by"] == "boostie@test"

    r = client.get(f"/v1/rooms/{room_id}/notes/summary", headers=boostie)
    assert r.json()["value"] == value


def test_put_overwrites_and_records_writer(client, room_id, boostie, missy):
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    put(client, room_id, boostie, "summary", "v1")
    r = put(client, room_id, missy, "summary", "v2")
    assert r.json()["value"] == "v2"
    assert r.json()["updated_by"] == "missy@test"


def test_read_notes_filters_by_keys(client, room_id, boostie):
    for key in ("summary", "open_questions", "architecture"):
        put(client, room_id, boostie, key, key)
    notes = client.get(
        f"/v1/rooms/{room_id}/notes", params={"keys": "summary,open_questions"}, headers=boostie
    ).json()["notes"]
    assert sorted(notes) == ["open_questions", "summary"]

    all_notes = client.get(f"/v1/rooms/{room_id}/notes", headers=boostie).json()["notes"]
    assert len(all_notes) == 3

    blank = client.get(f"/v1/rooms/{room_id}/notes", params={"keys": ","}, headers=boostie)
    assert len(blank.json()["notes"]) == 3


def test_missing_note_is_404(client, room_id, boostie):
    assert client.get(f"/v1/rooms/{room_id}/notes/nope", headers=boostie).status_code == 404


def test_invalid_key_rejected(client, room_id, boostie):
    assert put(client, room_id, boostie, "bad key!", 1).status_code == 422


def test_oversized_note_rejected(client, room_id, boostie):
    assert put(client, room_id, boostie, "big", "x" * 2000).status_code == 413
