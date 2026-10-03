"""Regression tests for room-o-matic/docs#18: disjoint PATCHes never undo each other."""

import pytest

from roomsd import db
from roomsd.routes import rooms as rooms_routes


@pytest.fixture
def room_id(client, boostie):
    r = client.post(
        "/v1/rooms", json={"name": "original", "purpose": "p0", "tags": ["a"]}, headers=boostie
    )
    return r.json()["room_id"]


@pytest.fixture
def interleave(monkeypatch, settings):
    """Run `write(conn)` as another writer committing right after the request's auth check:
    the point where the old code took its stale snapshot."""

    def install(write):
        real = rooms_routes.require_right

        def hooked(conn, room_id, caller, right):
            row = real(conn, room_id, caller, right)
            other = db.connect(settings.db_path)
            with other:
                write(other, room_id)
            other.close()
            return row

        monkeypatch.setattr(rooms_routes, "require_right", hooked)

    return install


def get(client, rid, h):
    return client.get(f"/v1/rooms/{rid}", headers=h).json()


def test_disjoint_patches_both_stick(client, room_id, boostie, interleave):
    interleave(lambda c, rid: c.execute("update rooms set name = 'new-name' where id = ?", (rid,)))
    r = client.patch(f"/v1/rooms/{room_id}", json={"purpose": "new-purpose"}, headers=boostie)
    assert r.status_code == 200
    room = get(client, room_id, boostie)
    assert (room["name"], room["purpose"]) == ("new-name", "new-purpose")


def test_concurrent_unlist_is_not_undone_by_a_tag_edit(client, room_id, boostie, interleave):
    client.patch(f"/v1/rooms/{room_id}", json={"listed": True}, headers=boostie)
    interleave(lambda c, rid: c.execute("update rooms set listed = 0 where id = ?", (rid,)))
    client.patch(f"/v1/rooms/{room_id}", json={"tags": ["b"]}, headers=boostie)
    room = get(client, room_id, boostie)
    assert (room["listed"], room["tags"]) == (False, ["b"])


def test_expected_revision_makes_conflicts_explicit(client, room_id, boostie):
    rev = get(client, room_id, boostie)["revision"]
    ok = client.patch(
        f"/v1/rooms/{room_id}", json={"name": "a", "expected_revision": rev}, headers=boostie
    )
    assert ok.status_code == 200 and ok.json()["revision"] == rev + 1
    stale = client.patch(
        f"/v1/rooms/{room_id}", json={"name": "b", "expected_revision": rev}, headers=boostie
    )
    assert stale.status_code == 409 and "revision" in stale.json()["detail"]
    assert get(client, room_id, boostie)["name"] == "a"


def test_same_field_without_revision_is_last_writer_wins(client, room_id, boostie):
    client.patch(f"/v1/rooms/{room_id}", json={"name": "first"}, headers=boostie)
    client.patch(f"/v1/rooms/{room_id}", json={"name": "second"}, headers=boostie)
    assert get(client, room_id, boostie)["name"] == "second"


def test_only_supplied_fields_and_null_semantics(client, room_id, boostie):
    r = client.patch(
        f"/v1/rooms/{room_id}", json={"purpose": None, "name": None}, headers=boostie
    ).json()
    assert (r["purpose"], r["name"]) == (None, "original")  # purpose cleared, name kept


def test_listing_resync_only_for_listing_fields(client, settings, room_id, boostie):
    def version():
        conn = db.connect(settings.db_path)
        v = conn.execute("select listing_version from rooms where id = ?", (room_id,)).fetchone()[0]
        conn.close()
        return v

    client.patch(f"/v1/rooms/{room_id}", json={"listed": True}, headers=boostie)
    v = version()
    client.patch(f"/v1/rooms/{room_id}", json={"max_hops": 5}, headers=boostie)
    assert version() == v  # not a listing field
    client.patch(f"/v1/rooms/{room_id}", json={"purpose": "shown in lobby"}, headers=boostie)
    assert version() == v + 1
