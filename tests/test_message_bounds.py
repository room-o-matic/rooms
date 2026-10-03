"""Regression tests for room-o-matic/docs#21: a message's whole representation is bounded,
not just its body, and basis references must be messages in the same room."""

import dataclasses
import json

import pytest
from conftest import BASE_URL, DOMAIN, ISSUER
from fastapi.testclient import TestClient

from roomsd.app import create_app
from roomsd.models import MAX_MESSAGE_REFS
from roomsd.verify import TokenVerifier

BODY, TOTAL = 1024, 2048


@pytest.fixture
def small(settings, lobby):
    s = dataclasses.replace(
        settings,
        max_message_bytes=BODY,
        max_message_total_bytes=TOTAL,
        max_request_bytes=16 * 1024,
        max_page_bytes=8 * 1024,
    )
    verifier = TokenVerifier(issuer=ISSUER, domain=DOMAIN, audience=BASE_URL, fetch_jwks=lobby.jwks)
    return TestClient(create_app(s, verifier=verifier))


@pytest.fixture
def rid(small, boostie):
    return small.post("/v1/rooms", json={"name": "bounds"}, headers=boostie).json()["room_id"]


def post(c, rid, h, **body):
    return c.post(f"/v1/rooms/{rid}/messages", json=body, headers=h)


def test_small_body_huge_metadata_rejected(small, rid, boostie):
    """The original finding: body='x' plus 4000 references was stored at 22x the limit."""
    r = post(small, rid, boostie, body="x", based_on_messages=list(range(1, 4001)))
    assert r.status_code == 413  # here the request cap catches it before decoding
    r = post(small, rid, boostie, body="x", based_on_messages=[1] * (MAX_MESSAGE_REFS + 1))
    assert r.status_code == 422  # and the reference count is bounded on its own
    assert small.get(f"/v1/rooms/{rid}/messages", headers=boostie).json()["messages"] == []


def test_reference_count_boundary(small, rid, boostie):
    ids = [
        post(small, rid, boostie, body=f"m{i}").json()["id"] for i in range(MAX_MESSAGE_REFS + 1)
    ]
    ok = post(small, rid, boostie, body="x", based_on_messages=ids[:MAX_MESSAGE_REFS])
    assert ok.status_code == 201
    assert post(small, rid, boostie, body="x", based_on_messages=ids).status_code == 422


def test_exact_total_boundary(small, rid, boostie):
    to = ["missy@test/" + "w" * 200] * 5  # typed metadata, no references
    overhead = len(json.dumps({"to": to}).encode())
    fill = TOTAL - overhead
    assert 0 < fill <= BODY
    assert post(small, rid, boostie, body="b" * fill, to=to).status_code == 201
    r = post(small, rid, boostie, body="b" * (fill + 1), to=to)
    assert r.status_code == 413 and "typed fields" in r.json()["detail"]


def test_multibyte_body_is_counted_in_bytes(small, rid, boostie):
    snow = "☃"  # 3 bytes in UTF-8
    assert post(small, rid, boostie, body=snow * (BODY // 3)).status_code == 201
    assert post(small, rid, boostie, body=snow * (BODY // 3 + 1)).status_code == 413


@pytest.mark.parametrize("ref", [0, -1, 2**63])
def test_out_of_range_references_rejected(small, rid, boostie, ref):
    assert post(small, rid, boostie, body="x", based_on_messages=[ref]).status_code == 422
    assert post(small, rid, boostie, body="x", in_reply_to=ref).status_code == 422


def test_missing_and_cross_room_references_rejected(small, rid, boostie):
    other = small.post("/v1/rooms", json={"name": "other"}, headers=boostie).json()["room_id"]
    foreign = post(small, other, boostie, body="elsewhere").json()["id"]
    r = post(small, rid, boostie, body="x", based_on_messages=[foreign])
    assert r.status_code == 422 and str(foreign) in r.json()["detail"]
    assert post(small, rid, boostie, body="x", based_on_messages=[10**9]).status_code == 422


def test_valid_typed_messages_still_work(small, rid, boostie):
    a = post(small, rid, boostie, type="finding", body="disk is full").json()["id"]
    r = post(
        small,
        rid,
        boostie,
        type="decision",
        topic="storage",
        body="Move logs to the big volume.",
        based_on_messages=[a, a],  # duplicates collapse
        confidence=0.9,
        in_reply_to=a,
    )
    assert r.status_code == 201
    assert r.json()["based_on_messages"] == [a]


def test_oversized_request_rejected_before_decoding(small, rid, boostie):
    raw = b'{"body": "' + b"x" * (32 * 1024) + b'"}'
    r = small.post(
        f"/v1/rooms/{rid}/messages",
        content=raw,
        headers={**boostie, "content-type": "application/json"},
    )
    assert r.status_code == 413 and "request body" in r.json()["detail"]

    def chunks():  # no Content-Length: counted while streaming
        for _ in range(8):
            yield b"x" * 4096

    r = small.post(
        f"/v1/rooms/{rid}/messages",
        content=chunks(),
        headers={**boostie, "content-type": "application/json"},
    )
    assert r.status_code == 413


def test_read_pages_are_byte_bounded(small, rid, boostie):
    for i in range(20):
        post(small, rid, boostie, body=f"{i:02d}".ljust(BODY, "x"))
    seen, after = [], 0
    while True:
        page = small.get(
            f"/v1/rooms/{rid}/messages", params={"after_id": after, "limit": 500}, headers=boostie
        ).json()
        if not page["messages"]:
            break
        assert len(page["messages"]) <= 8  # 8 KiB budget, ~1 KiB messages
        seen += page["messages"]
        after = page["latest_message_id"]
    assert len(seen) == 20  # the cursor still reaches everything
    feed = small.get("/v1/me/updates", params={"limit": 500}, headers=boostie).json()
    assert 0 < len(feed["messages"]) <= 8
