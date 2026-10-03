import json
import sqlite3
from pathlib import Path

from roomsd.ids import now_iso

SCHEMA = """
create table if not exists rooms (
  id text primary key,
  name text not null,
  purpose text,
  created_by text not null,
  created_at text not null,
  archived_at text,
  listed integer not null default 0,
  tags_json text not null default '[]',
  -- lobbyd listing sync: bumped on any listing-relevant change, synced when pushed.
  listing_version integer not null default 0,
  listing_synced_version integer not null default 0
);

create table if not exists participants (
  room_id text not null references rooms(id),
  agent text not null,
  role text,
  joined_at text not null,
  last_seen_at text,
  primary key (room_id, agent)
);

create table if not exists messages (
  id integer primary key autoincrement,
  room_id text not null references rooms(id),
  sender text not null,
  type text not null,
  topic text,
  body text not null,
  payload_json text,
  created_at text not null
);

create index if not exists messages_room_id_id on messages(room_id, id);

create table if not exists notes (
  room_id text not null references rooms(id),
  key text not null,
  value_json text not null,
  updated_by text not null,
  updated_at text not null,
  primary key (room_id, key)
);

create table if not exists invites (
  invite_id text primary key,
  token_hash text not null unique,
  agent text not null,
  room_id text not null references rooms(id),
  role text,
  created_by text not null,
  created_at text not null,
  expires_at text not null,
  revoked_at text
);

create index if not exists invites_room_id on invites(room_id);

create table if not exists audit (
  id integer primary key autoincrement,
  room_id text,
  agent text not null,
  action text not null,
  detail_json text,
  created_at text not null
);

create index if not exists audit_room_id_id on audit(room_id, id);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 10000")
    return conn


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("pragma journal_mode = wal")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, agent: str, action: str, room_id: str | None, **detail) -> None:
    detail = {k: v for k, v in detail.items() if v is not None}
    conn.execute(
        "insert into audit (room_id, agent, action, detail_json, created_at)"
        " values (?, ?, ?, ?, ?)",
        (room_id, agent, action, json.dumps(detail) if detail else None, now_iso()),
    )
