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
  -- docs#10: who may join. open = any named agent self-joins with default_rights_json;
  -- closed = only agents with a members grant. Discoverability (listed) is separate.
  admission text not null default 'open',
  default_rights_json text not null default '["read","write","invite"]',
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

-- docs#10: per-room rights for named agents. A row with banned_at set blocks rejoining,
-- even in an open room. The room creator is always admin.
create table if not exists members (
  room_id text not null references rooms(id),
  agent text not null,
  rights_json text not null,
  granted_by text not null,
  granted_at text not null,
  banned_at text,
  banned_by text,
  primary key (room_id, agent)
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

-- docs#12: durable task assignments with lease-fenced claims.
create table if not exists tasks (
  task_id text primary key,
  room_id text not null references rooms(id),
  task_key text,
  title text not null,
  description text,
  issue_url text,
  repo text,
  base_commit text,
  review_required integer not null default 0,
  state text not null,
  created_by text not null,
  created_at text not null,
  updated_at text not null,
  claim_owner text,
  claim_session text,
  claim_generation integer not null default 0,
  lease_expires_at text,
  receipt_json text,
  completed_at text,
  unique (room_id, task_key)
);

create index if not exists tasks_room_state on tasks(room_id, state);

create table if not exists task_events (
  id integer primary key autoincrement,
  task_id text not null references tasks(task_id),
  room_id text not null,
  kind text not null,
  actor text not null,
  generation integer not null,
  detail_json text,
  created_at text not null
);

create index if not exists task_events_room_id on task_events(room_id, id);

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
    # One connection per request, but FastAPI runs a sync dependency and its route in
    # different threadpool threads, so the connection must be allowed to change threads.
    # It is never used by two threads at once.
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
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
