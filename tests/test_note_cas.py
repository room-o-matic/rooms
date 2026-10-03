"""Regression tests for room-o-matic/docs#20: notes have revisions, compare-and-set,
recoverable history and resumable change discovery."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from roomsd.app import create_app
from roomsd.routes import rooms as rooms_routes
from roomsd.verify import TokenVerifier


def url(rid, key="", *rest):
    return "/".join(filter(None, [f"/v1/rooms/{rid}/notes", key, *rest]))


def put(client, rid, h, key, value, **kw):
    return client.put(url(rid, key), json={"value": value, **kw}, headers=h)


def test_two_stale_writers(client, room_id, boostie, missy):
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=missy)
    put(client, room_id, boostie, "tasks", [])
    base = client.get(url(room_id, "tasks"), headers=boostie).json()["revision"]
    a = put(client, room_id, boostie, "tasks", ["A"], if_revision=base)
    b = put(client, room_id, missy, "tasks", ["B"], if_revision=base)  # read the same base
    assert (a.status_code, b.status_code) == (200, 412)
    assert "revision" in b.json()["detail"]
    assert client.get(url(room_id, "tasks"), headers=boostie).json()["value"] == ["A"]


def test_simultaneous_cas_has_one_winner(client, room_id, boostie):
    put(client, room_id, boostie, "n", 0)
    with ThreadPoolExecutor(8) as pool:
        codes = list(
            pool.map(
                lambda i: put(client, room_id, boostie, "n", i, if_revision=1).status_code, range(8)
            )
        )
    assert codes.count(200) == 1 and codes.count(412) == 7
    assert client.get(url(room_id, "n"), headers=boostie).json()["revision"] == 2


def test_conditional_create_race(client, room_id, boostie):
    with ThreadPoolExecutor(6) as pool:
        codes = list(
            pool.map(
                lambda i: put(client, room_id, boostie, "fresh", i, if_revision=0).status_code,
                range(6),
            )
        )
    assert sorted(codes) == [200] + [412] * 5


def test_unconditional_put_is_last_writer_wins(client, room_id, boostie):
    put(client, room_id, boostie, "s", "one")
    r = put(client, room_id, boostie, "s", "two").json()
    assert (r["value"], r["revision"]) == ("two", 2)


def test_overwritten_values_are_recoverable(client, room_id, boostie):
    for v in ["A", "B", "C"]:
        put(client, room_id, boostie, "summary", v)
    history = client.get(url(room_id, "summary", "history"), headers=boostie).json()
    assert [(n["revision"], n["value"]) for n in history] == [(3, "C"), (2, "B"), (1, "A")]


def test_history_is_bounded(client, room_id, boostie):
    for i in range(rooms_routes.NOTE_HISTORY + 5):
        put(client, room_id, boostie, "busy", i)
    history = client.get(url(room_id, "busy", "history"), headers=boostie).json()
    assert len(history) == rooms_routes.NOTE_HISTORY
    assert history[0]["revision"] == rooms_routes.NOTE_HISTORY + 5


def test_note_only_changes_are_discoverable(client, room_id, boostie):
    first = client.get(url(room_id, "changes"), headers=boostie).json()
    put(client, room_id, boostie, "a", 1)
    put(client, room_id, boostie, "b", 2)
    page = client.get(
        url(room_id, "changes"), params={"after": first["next_cursor"]}, headers=boostie
    ).json()
    assert [(c["key"], c["revision"]) for c in page["changes"]] == [("a", 1), ("b", 1)]
    again = client.get(
        url(room_id, "changes"), params={"after": page["next_cursor"]}, headers=boostie
    ).json()
    assert again["changes"] == []
    # the message feed doesn't carry note changes; that's why this endpoint exists
    assert client.get("/v1/me/updates", headers=boostie).json()["messages"] == []


def test_revisions_persist_across_restart(client, settings, room_id, boostie, lobby):
    put(client, room_id, boostie, "k", "v1")
    put(client, room_id, boostie, "k", "v2")
    verifier = TokenVerifier(
        issuer="http://lobby.test",
        domain="test",
        audience="http://rooms-a.test",
        fetch_jwks=lobby.jwks,
    )
    restarted = TestClient(create_app(settings, verifier=verifier))
    assert restarted.get(url(room_id, "k"), headers=boostie).json()["revision"] == 2
    r = put(restarted, room_id, boostie, "k", "v3", if_revision=1)
    assert r.status_code == 412  # stale even after the restart
