"""Tests for room-o-matic/docs#24: versioned schema upgrades, consistent backups, restore
that can't revive revoked access or stale work, and readiness/metrics. Every drill here
runs on a disposable copy under tmp_path."""

import hashlib
import os
import sqlite3
import stat

import pytest
from conftest import BASE_URL, DOMAIN, ISSUER
from fastapi.testclient import TestClient

from roomsd import cli, db, ops, recovery
from roomsd.app import create_app
from roomsd.verify import TokenVerifier

BASE = "0123456789abcdef0123456789abcdef01234567"


def app_for(settings, lobby):
    verifier = TokenVerifier(issuer=ISSUER, domain=DOMAIN, audience=BASE_URL, fetch_jwks=lobby.jwks)
    return TestClient(create_app(settings, verifier=verifier))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_of(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("pragma user_version").fetchone()[0]
    finally:
        conn.close()


# ----- schema versions ------------------------------------------------------------------


def test_fresh_database_is_versioned(client, settings):
    assert version_of(settings.db_path) == db.SCHEMA_VERSION


@pytest.fixture
def fresh(settings, tmp_path):
    import dataclasses

    return dataclasses.replace(settings, data_dir=tmp_path / "fresh")


def test_unversioned_baseline_is_adopted_in_place(fresh, lobby, boostie):
    settings = fresh
    settings.data_dir.mkdir()
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(db.SCHEMA)  # what every release before versioning created
    conn.execute(
        "insert into rooms (id, name, created_by, created_at) values"
        " ('room_legacy', 'legacy', 'boostie@test', '2026-01-01T00:00:00.000Z')"
    )
    conn.commit()
    conn.close()
    assert version_of(settings.db_path) == 0
    c = app_for(settings, lobby)
    assert version_of(settings.db_path) == 1
    assert c.get("/v1/rooms/room_legacy", headers=boostie).status_code in (200, 403)


def test_unknown_unversioned_schema_is_refused_untouched(fresh, lobby):
    settings = fresh
    settings.data_dir.mkdir()
    conn = sqlite3.connect(settings.db_path)
    conn.execute("create table notes (room_id text, key text, value_json text)")  # pre-#20
    conn.commit()
    conn.close()
    before = digest(settings.db_path)
    with pytest.raises(ops.SchemaError, match="notes.revision"):
        app_for(settings, lobby)
    assert digest(settings.db_path) == before


def test_newer_schema_is_refused_at_startup(client, settings, lobby):
    conn = sqlite3.connect(settings.db_path)
    conn.execute("pragma user_version = 99")
    conn.close()
    with pytest.raises(ops.SchemaError, match="newer than this release"):
        app_for(settings, lobby)


def test_upgrade_runs_in_order_after_a_backup(client, settings, room_id, boostie, monkeypatch):
    client.put(f"/v1/rooms/{room_id}/notes/k", json={"value": 1}, headers=boostie)
    calls = []

    def to_v2(conn):
        calls.append(2)
        conn.execute("alter table rooms add column colour text default 'blue'")

    def to_v3(conn):
        calls.append(3)
        conn.execute("update rooms set colour = 'green'")

    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(db, "MIGRATIONS", {1: to_v2, 2: to_v3})
    result = db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    assert calls == [2, 3] and result["from"] == 1 and result["to"] == 3
    assert version_of(settings.db_path) == 3
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("select colour from rooms").fetchone()[0] == "green"
    conn.close()
    pre = ops.verify_backup(settings.backup_dir / os.listdir(settings.backup_dir)[0])
    assert pre["schema_version"] == 1  # the rollback point


def test_failed_upgrade_rolls_back(client, settings, room_id, monkeypatch):
    def broken(conn):
        conn.execute("alter table rooms add column half_done text")
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(db, "MIGRATIONS", {1: broken})
    with pytest.raises(ops.SchemaError, match="rolled back"):
        db.init_db(settings.db_path)
    assert version_of(settings.db_path) == 1
    conn = sqlite3.connect(settings.db_path)
    cols = {r[1] for r in conn.execute("pragma table_info(rooms)")}
    assert "half_done" not in cols
    assert conn.execute("select count(*) from rooms").fetchone()[0] == 1
    conn.close()


def test_gap_in_migrations_is_refused(client, settings, monkeypatch):
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(db, "MIGRATIONS", {2: lambda c: None})
    with pytest.raises(ops.SchemaError, match="no roomsd migration from schema v1"):
        db.init_db(settings.db_path)
    assert version_of(settings.db_path) == 1


# ----- backup and restore -----------------------------------------------------------------


def test_backup_is_consistent_private_and_tamper_evident(
    client, settings, room_id, boostie, tmp_path
):
    for i in range(20):
        client.post(f"/v1/rooms/{room_id}/messages", json={"body": f"m{i}"}, headers=boostie)
    dest = tmp_path / "bk"
    manifest = recovery.backup(settings, dest)
    assert manifest["integrity"] == "ok" and manifest["service"] == "roomsd"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert all(stat.S_IMODE((dest / f).stat().st_mode) == 0o600 for f in manifest["files"])
    assert ops.verify_backup(dest)["schema_version"] == db.SCHEMA_VERSION
    with open(dest / "roomsd.sqlite", "r+b") as f:
        f.seek(200)
        f.write(b"\x00garbage")
    with pytest.raises(ops.BackupError, match="checksum"):
        ops.verify_backup(dest)


def test_restore_round_trip_without_reviving_access_or_work(
    client, settings, lobby, room_id, boostie, missy, make_agent, tmp_path
):
    c = client
    odin = make_agent("odin")
    for who in (missy, odin):
        c.post(f"/v1/rooms/{room_id}/participants", json={}, headers=who)
    c.put(f"/v1/rooms/{room_id}/notes/plan", json={"value": "v1"}, headers=boostie)
    c.put(f"/v1/rooms/{room_id}/notes/plan", json={"value": "v2"}, headers=boostie)
    m = c.post(f"/v1/rooms/{room_id}/messages", json={"body": "before"}, headers=boostie).json()
    inv = c.post(f"/v1/rooms/{room_id}/invites", json={"name": "w"}, headers=boostie).json()
    guest = {"Authorization": f"Bearer {inv['token']}"}
    c.post(f"/v1/rooms/{room_id}/participants", json={}, headers=guest)
    tid = c.post(
        f"/v1/rooms/{room_id}/tasks",
        headers=boostie,
        json={
            "title": "t",
            "repo": "room-o-matic/rooms",
            "base_commit": BASE,
        },
    ).json()["task_id"]
    gen = c.post(
        f"/v1/rooms/{room_id}/tasks/{tid}/claim", json={"session_id": "s"}, headers=missy
    ).json()["task"]["claim_generation"]

    recovery.backup(settings, tmp_path / "bk")

    # after the snapshot: access removed, rights reduced, more traffic
    assert (
        c.delete(
            f"/v1/rooms/{room_id}/members/missy@test", params={"ban": True}, headers=boostie
        ).status_code
        == 204
    )
    c.put(f"/v1/rooms/{room_id}/members/odin@test", json={"rights": ["read"]}, headers=boostie)
    for i in range(5):
        last = c.post(
            f"/v1/rooms/{room_id}/messages", json={"body": f"after {i}"}, headers=boostie
        ).json()["id"]

    report = recovery.restore(settings, tmp_path / "bk", force=True)
    assert report["integrity"] == "ok" and report["elapsed_seconds"] >= 0
    assert report["moved_aside"] and os.path.exists(report["report_path"])
    inv_summary = report["invalidated"]
    assert inv_summary["invites_revoked"] == 1 and inv_summary["task_leases_expired"] == 1
    assert inv_summary["journal_entries_replayed"] == 2

    r = app_for(settings, lobby)
    # restored: room, notes and their history, messages
    hist = r.get(f"/v1/rooms/{room_id}/notes/plan/history", headers=boostie).json()
    assert [n["value"] for n in hist] == ["v2", "v1"]
    msgs = r.get(f"/v1/rooms/{room_id}/messages", headers=boostie).json()["messages"]
    assert msgs[-1]["id"] == m["id"]
    # not revived: the guest, the banned member, odin's old rights
    assert r.get(f"/v1/rooms/{room_id}/messages", headers=guest).status_code == 401
    assert r.get(f"/v1/rooms/{room_id}/messages", headers=missy).status_code == 403
    assert (
        r.post(f"/v1/rooms/{room_id}/messages", json={"body": "x"}, headers=odin).status_code == 403
    )
    # not reissued: IDs past every cursor handed out after the snapshot
    new = r.post(f"/v1/rooms/{room_id}/messages", json={"body": "new"}, headers=boostie).json()
    assert new["id"] > last
    # the old claim can't continue, and a new claim can't collide with its generation
    task = r.get(f"/v1/rooms/{room_id}/tasks/{tid}", headers=boostie).json()
    assert task["claim_generation"] > gen + 100
    r.put(
        f"/v1/rooms/{room_id}/members/odin@test",
        json={"rights": ["read", "write"]},
        headers=boostie,
    )
    again = r.post(f"/v1/rooms/{room_id}/tasks/{tid}/claim", json={"session_id": "t"}, headers=odin)
    assert again.status_code == 200 and again.json()["task"]["claim_generation"] > gen + 100


def test_restore_refusals(client, settings, tmp_path):
    recovery.backup(settings, tmp_path / "bk")
    with pytest.raises(ops.BackupError, match="--force"):
        recovery.restore(settings, tmp_path / "bk")
    other = tmp_path / "other"
    ops.backup(settings.db_path, other, service="lobbyd")
    with pytest.raises(ops.BackupError, match="not roomsd"):
        recovery.restore(settings, other, force=True)


def test_cli_drill(client, settings, room_id, boostie, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ROOMSD_DATA_DIR", str(settings.data_dir))
    assert cli.main(["backup", "--out", str(tmp_path / "bk")]) == 0
    assert cli.main(["verify-backup", str(tmp_path / "bk")]) == 0
    assert cli.main(["restore", str(tmp_path / "bk")]) == 1  # refuses without --force
    assert cli.main(["restore", str(tmp_path / "bk"), "--force"]) == 0
    assert '"integrity": "ok"' in capsys.readouterr().out


# ----- readiness and metrics ----------------------------------------------------------------


def test_readyz_and_metrics(client, settings, lobby, boostie):
    client.post("/v1/rooms", json={"name": "r"}, headers=boostie)  # first JWKS fetch
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ready"] is True
    checks = ready.json()["checks"]
    assert checks["schema"]["version"] == db.SCHEMA_VERSION and checks["jwks"]["fetched"]
    text = client.get("/metrics").text
    assert "roomsd_ready 1" in text and "roomsd_active_rooms 1" in text


def test_readyz_fails_on_storage_pressure(settings, lobby):
    import dataclasses

    c = app_for(dataclasses.replace(settings, min_free_bytes=1 << 62), lobby)
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["storage"]["ok"] is False


def test_readyz_fails_when_jwks_fails_closed(settings, boostie):
    def down():
        raise OSError("lobbyd unreachable")

    verifier = TokenVerifier(
        issuer=ISSUER,
        domain=DOMAIN,
        audience=BASE_URL,
        fetch_jwks=down,
        background=lambda fn: fn(),
    )
    c = TestClient(create_app(settings, verifier=verifier))
    assert c.get("/v1/rooms", headers=boostie).status_code == 401
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["jwks"]["failing_closed"] is True


def test_backup_while_serving_writes(client, settings, room_id, boostie, tmp_path):
    import threading

    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            client.post(f"/v1/rooms/{room_id}/messages", json={"body": f"w{i}"}, headers=boostie)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # let some writes land first
        if client.get(f"/v1/rooms/{room_id}/messages", headers=boostie).json()["messages"]:
            break
    try:
        for n in range(3):
            recovery.backup(settings, tmp_path / f"bk{n}")
    finally:
        stop.set()
        t.join()
    for n in range(3):
        assert ops.verify_backup(tmp_path / f"bk{n}")["integrity"] == "ok"
        conn = sqlite3.connect(tmp_path / f"bk{n}" / "roomsd.sqlite")
        ids = [r[0] for r in conn.execute("select id from messages order by id")]
        conn.close()
        assert ids and ids == list(range(ids[0], ids[0] + len(ids)))  # no torn or partial writes
