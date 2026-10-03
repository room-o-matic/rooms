"""Backup and restore for roomsd (room-o-matic/docs#24). See ops.py for the mechanics.

A restored snapshot is older than the service it replaces. Before serving, a restore:

- revokes every invite that was live in the snapshot: guests must be re-invited, so a
  worker token revoked after the snapshot can't come back;
- replays the revocation journal written since the snapshot (member grants, removals and
  bans), so access removed after the snapshot stays removed;
- expires every task lease and moves claim generations forward, so a claim held (or
  fenced) after the snapshot can neither continue nor be confused with a new one;
- moves message, note-revision, task-event and audit IDs past `id_gap`, so cursors and IDs
  clients already hold are never reissued for different records.

Everything else (rooms, messages, notes and their history, members, audit) is restored as
it was at the snapshot. Listings are republished to lobbyd by the normal startup sync.
"""

import json
import sqlite3
from pathlib import Path

from roomsd import db, ops
from roomsd.config import Settings
from roomsd.ids import now_iso

DEFAULT_ID_GAP = 1_000_000
GENERATION_GAP = 1_000


def backup(settings: Settings, dest: Path) -> dict:
    return ops.backup(settings.db_path, dest, service="roomsd", schema_version=db.SCHEMA_VERSION)


def post_restore(conn: sqlite3.Connection, journal: ops.Journal, since: str, id_gap: int) -> dict:
    now = now_iso()
    summary: dict = {}
    with conn:
        summary["invites_revoked"] = conn.execute(
            "update invites set revoked_at = ? where revoked_at is null and expires_at > ?",
            (now, now),
        ).rowcount
        replayed = 0
        for e in journal.entries(since=since):
            if e["kind"] == "member.grant":
                conn.execute(
                    "insert into members (room_id, agent, rights_json, granted_by, granted_at)"
                    " select ?, ?, ?, 'restore:journal', ? where exists"
                    " (select 1 from rooms where id = ?)"
                    " on conflict (room_id, agent) do update set rights_json ="
                    " excluded.rights_json, banned_at = null, banned_by = null",
                    (e["room_id"], e["agent"], json.dumps(e["rights"]), e["at"], e["room_id"]),
                )
            elif e["kind"] == "member.remove":
                prefix = e["agent"] + "/"
                conn.execute(
                    "delete from participants where room_id = ?"
                    " and (agent = ? or substr(agent, 1, ?) = ?)",
                    (e["room_id"], e["agent"], len(prefix), prefix),
                )
                if e.get("ban"):
                    conn.execute(
                        "insert into members (room_id, agent, rights_json, granted_by,"
                        " granted_at, banned_at, banned_by)"
                        " select ?, ?, '[]', ?, ?, ?, ? where exists"
                        " (select 1 from rooms where id = ?)"
                        " on conflict (room_id, agent) do update set rights_json = '[]',"
                        " banned_at = excluded.banned_at, banned_by = excluded.banned_by",
                        (
                            e["room_id"],
                            e["agent"],
                            e["by"],
                            e["at"],
                            e["at"],
                            e["by"],
                            e["room_id"],
                        ),
                    )
                else:
                    conn.execute(
                        "delete from members where room_id = ? and agent = ?",
                        (e["room_id"], e["agent"]),
                    )
            else:
                continue
            replayed += 1
        summary["journal_entries_replayed"] = replayed
        summary["task_leases_expired"] = conn.execute(
            "update tasks set lease_expires_at = ?, claim_generation = claim_generation + ?"
            " where lease_expires_at is not null and lease_expires_at > ?",
            (now, GENERATION_GAP, now),
        ).rowcount
        conn.execute(
            "update tasks set claim_generation = claim_generation + ?"
            " where lease_expires_at is null or lease_expires_at <= ?",
            (GENERATION_GAP, now),
        )
        ops.bump_sequences(conn, db.SEQUENCES, id_gap)
        summary["id_gap"] = id_gap
        db.audit(conn, "operator", "ops.restore", None, **summary)
    return summary


def restore(settings: Settings, src: Path, *, force: bool = False, id_gap: int = DEFAULT_ID_GAP):
    report = ops.restore(
        src,
        settings.data_dir,
        service="roomsd",
        max_schema_version=db.SCHEMA_VERSION,
        force=force,
    )
    report["schema"] = db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    conn = db.connect(settings.db_path)
    try:
        report["invalidated"] = post_restore(
            conn, ops.Journal(settings.journal_path), report["snapshot_started_at"], id_gap
        )
    finally:
        conn.close()
    report["report_path"] = str(ops.write_report(settings.data_dir, report))
    return report
