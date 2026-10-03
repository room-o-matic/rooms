"""Tests for room-o-matic/docs#12: lease-fenced task claims and verifiable completion."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from roomsd import db
from roomsd.app import create_app
from roomsd.verify import TokenVerifier

BASE = "0123456789abcdef0123456789abcdef01234567"
SHA = "89abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def room_id(client, boostie, missy):
    rid = client.post("/v1/rooms", json={"name": "work"}, headers=boostie).json()["room_id"]
    client.post(f"/v1/rooms/{rid}/participants", json={}, headers=missy)
    return rid


def url(rid, *parts):
    return "/".join([f"/v1/rooms/{rid}/tasks", *parts])


def create(client, rid, headers, **kw):
    body = {
        "title": "Fix #12",
        "issue_url": "https://github.com/room-o-matic/docs/issues/12",
        "repo": "room-o-matic/rooms",
        "base_commit": BASE,
        **kw,
    }
    return client.post(url(rid), json=body, headers=headers)


def claim(client, rid, tid, headers, **kw):
    return client.post(url(rid, tid, "claim"), json=kw, headers=headers)


def expire(settings, tid):
    conn = db.connect(settings.db_path)
    with conn:
        conn.execute(
            "update tasks set lease_expires_at = '2000-01-01T00:00:00.000Z' where task_id = ?",
            (tid,),
        )
    conn.close()


def result_message(client, rid, headers, body="done, see PR"):
    return client.post(
        f"/v1/rooms/{rid}/messages", json={"body": body, "type": "finding"}, headers=headers
    ).json()["id"]


def receipt(mid, **kw):
    return {
        "result_message_id": mid,
        "commits": [{"repo": "room-o-matic/rooms", "sha": SHA}],
        "tests": [{"command": "uv run pytest -q", "result": "passed"}],
        **kw,
    }


def test_task_keeps_immutable_references(client, room_id, boostie):
    t = create(client, room_id, boostie).json()
    assert (t["state"], t["repo"], t["base_commit"]) == ("open", "room-o-matic/rooms", BASE)
    assert t["issue_url"].endswith("/issues/12") and t["task_id"].startswith("task_")
    assert create(client, room_id, boostie, base_commit="not-a-sha").status_code == 422


def test_task_key_is_idempotent(client, room_id, boostie):
    a = create(client, room_id, boostie, task_key="docs#12")
    b = create(client, room_id, boostie, task_key="docs#12")
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["task_id"] == b.json()["task_id"]


def test_concurrent_claims_have_exactly_one_winner(client, room_id, boostie, missy):
    tid = create(client, room_id, boostie).json()["task_id"]
    who = [boostie, missy] * 4
    with ThreadPoolExecutor(8) as pool:
        results = list(
            pool.map(lambda i: claim(client, room_id, tid, who[i], session_id=f"s{i}"), range(8))
        )
    winners = [r for r in results if r.status_code == 200]
    assert len(winners) == 1
    assert all(r.status_code == 409 for r in results if r.status_code != 200)
    assert winners[0].json()["task"]["claim_generation"] == 1


def test_claim_retry_is_idempotent(client, room_id, boostie):
    tid = create(client, room_id, boostie).json()["task_id"]
    first = claim(client, room_id, tid, boostie, session_id="s1").json()
    again = claim(client, room_id, tid, boostie, session_id="s1").json()
    assert first["changed"] and not again["changed"]
    assert again["task"]["claim_generation"] == 1


def test_expired_lease_is_taken_over_and_old_holder_is_fenced(
    client, settings, room_id, boostie, missy
):
    tid = create(client, room_id, boostie).json()["task_id"]
    gen1 = claim(client, room_id, tid, boostie, session_id="a").json()["task"]["claim_generation"]
    assert claim(client, room_id, tid, missy).status_code == 409  # lease still live
    expire(settings, tid)
    # the old holder can't renew an expired lease...
    r = client.post(url(room_id, tid, "renew"), json={"generation": gen1}, headers=boostie)
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    # ...and someone else takes over with a new fencing token
    takeover = claim(client, room_id, tid, missy, session_id="b").json()["task"]
    assert (takeover["claim_owner"], takeover["claim_generation"]) == ("missy@test", 2)
    # the stale holder can't finish the reassigned task, even with a valid receipt
    mid = result_message(client, room_id, boostie)
    r = client.post(
        url(room_id, tid, "complete"),
        json={"generation": gen1, "receipt": receipt(mid)},
        headers=boostie,
    )
    assert r.status_code == 409 and "stale" in r.json()["detail"]


def test_renew_vs_takeover_race(client, settings, room_id, boostie, missy):
    tid = create(client, room_id, boostie).json()["task_id"]
    claim(client, room_id, tid, boostie)
    expire(settings, tid)
    with ThreadPoolExecutor(2) as pool:
        renew = pool.submit(
            client.post, url(room_id, tid, "renew"), json={"generation": 1}, headers=boostie
        )
        take = pool.submit(claim, client, room_id, tid, missy)
        renew, take = renew.result(), take.result()
    assert renew.status_code == 409  # an expired lease is never renewed
    assert take.status_code == 200


def test_renew_extends_and_release_reopens(client, room_id, boostie, missy):
    tid = create(client, room_id, boostie).json()["task_id"]
    before = claim(client, room_id, tid, boostie).json()["task"]["lease_expires_at"]
    after = client.post(
        url(room_id, tid, "renew"), json={"generation": 1, "lease_seconds": 7200}, headers=boostie
    ).json()["task"]["lease_expires_at"]
    assert after > before
    assert (
        client.post(url(room_id, tid, "release"), json={"generation": 1}, headers=missy).status_code
        == 409
    )  # not the holder
    released = client.post(
        url(room_id, tid, "release"), json={"generation": 1}, headers=boostie
    ).json()["task"]
    assert (released["state"], released["claim_owner"]) == ("open", None)
    assert claim(client, room_id, tid, missy).json()["task"]["claim_generation"] == 2


def test_completion_requires_a_valid_receipt(client, room_id, boostie, missy):
    tid = create(client, room_id, boostie).json()["task_id"]
    claim(client, room_id, tid, boostie)

    def done(rec):
        return client.post(
            url(room_id, tid, "complete"), json={"generation": 1, "receipt": rec}, headers=boostie
        )

    mine = result_message(client, room_id, boostie)
    theirs = result_message(client, room_id, missy)
    other_room = client.post("/v1/rooms", json={"name": "elsewhere"}, headers=boostie).json()
    foreign = result_message(client, other_room["room_id"], boostie)

    assert done({"result_message_id": mine}).status_code == 422  # prose only: no evidence
    assert done(receipt(foreign)).status_code == 422  # cross-room reference
    assert done(receipt(theirs)).status_code == 422  # someone else's message
    assert done(receipt(999_999)).status_code == 422
    ok = done(receipt(mine))
    assert ok.status_code == 200 and ok.json()["task"]["state"] == "done"
    assert ok.json()["task"]["receipt"]["commits"][0]["sha"] == SHA
    assert done(receipt(mine)).json()["changed"] is False  # duplicate retry


def test_unauthorized_completion_rejected(client, room_id, boostie, make_agent):
    tid = create(client, room_id, boostie).json()["task_id"]
    claim(client, room_id, tid, boostie)
    outsider = make_agent("outsider")  # never joined the room
    r = client.post(
        url(room_id, tid, "complete"),
        json={"generation": 1, "receipt": receipt(1)},
        headers=outsider,
    )
    assert r.status_code == 403
    inv = client.post(f"/v1/rooms/{room_id}/invites", json={"name": "g"}, headers=boostie).json()
    guest = {"Authorization": f"Bearer {inv['token']}"}
    client.post(f"/v1/rooms/{room_id}/participants", json={}, headers=guest)
    r = client.post(
        url(room_id, tid, "complete"), json={"generation": 1, "receipt": receipt(1)}, headers=guest
    )
    assert r.status_code == 409  # not the holder


def test_review_flow(client, room_id, boostie, missy):
    tid = create(client, room_id, boostie, review_required=True).json()["task_id"]
    claim(client, room_id, tid, missy)
    mid = result_message(client, room_id, missy)
    t = client.post(
        url(room_id, tid, "complete"),
        json={"generation": 1, "receipt": receipt(mid)},
        headers=missy,
    ).json()["task"]
    assert t["state"] == "review"
    assert (
        client.post(
            url(room_id, tid, "review"), json={"decision": "accept"}, headers=missy
        ).status_code
        == 403
    )  # can't approve your own work
    back = client.post(
        url(room_id, tid, "review"), json={"decision": "reject", "note": "tests"}, headers=boostie
    ).json()["task"]
    assert (back["state"], back["claim_owner"]) == ("in_progress", "missy@test")
    t = client.post(
        url(room_id, tid, "complete"),
        json={"generation": 1, "receipt": receipt(mid)},
        headers=missy,
    ).json()["task"]
    assert t["state"] == "review"
    done = client.post(
        url(room_id, tid, "review"), json={"decision": "accept"}, headers=boostie
    ).json()["task"]
    assert done["state"] == "done" and done["completed_at"]


def test_events_are_resumable_and_durable_across_restart(client, settings, room_id, boostie, lobby):
    tid = create(client, room_id, boostie).json()["task_id"]
    claim(client, room_id, tid, boostie)
    page = client.get(url(room_id, "events"), headers=boostie).json()
    assert [e["kind"] for e in page["events"]] == ["create", "claim"]
    client.post(
        url(room_id, tid, "state"), json={"generation": 1, "state": "in_progress"}, headers=boostie
    )

    verifier = TokenVerifier(
        issuer="http://lobby.test",
        domain="test",
        audience="http://rooms-a.test",
        fetch_jwks=lobby.jwks,
    )
    restarted = TestClient(create_app(settings, verifier=verifier))  # same database
    rest = restarted.get(
        url(room_id, "events"), params={"after": page["next_cursor"]}, headers=boostie
    ).json()
    assert [e["kind"] for e in rest["events"]] == ["in_progress"]
    t = restarted.get(url(room_id, tid), headers=boostie).json()
    assert (t["state"], t["claim_generation"], t["base_commit"]) == ("in_progress", 1, BASE)


def test_cancel_and_archive(client, room_id, boostie, missy):
    tid = create(client, room_id, boostie).json()["task_id"]
    assert client.post(url(room_id, tid, "cancel"), headers=missy).status_code == 403
    assert client.post(url(room_id, tid, "cancel"), headers=boostie).json()["task"]["state"] == (
        "cancelled"
    )
    assert claim(client, room_id, tid, missy).status_code == 409
    client.patch(f"/v1/rooms/{room_id}", json={"archived": True}, headers=boostie)
    assert create(client, room_id, boostie).status_code == 409
