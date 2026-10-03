import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

from roomsd.ids import iso_in, new_id, now_iso

TOKEN_PREFIX = "rmsd_"
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

# agent:  a named agent (Boostie, Missy, …); full API access subject to room membership.
# agentd: an agentd instance; may only manage its own registry entry.
# invite: a room-scoped guest (e.g. an agentd worker); identity is "<inviter>/<name>".
Scope = Literal["agent", "agentd", "invite"]
ISSUABLE_SCOPES: tuple[Scope, ...] = ("agent", "agentd")


@dataclass(frozen=True)
class Principal:
    agent: str
    scope: Scope
    room_id: str | None = None
    role: str | None = None
    invite_id: str | None = None
    expires_at: str | None = None
    token_hash: str = ""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_name(name: str) -> None:
    if not AGENT_NAME_RE.match(name):
        raise ValueError(f"invalid name {name!r}: use lowercase [a-z0-9_.-], max 64")


def create_token(conn: sqlite3.Connection, agent: str, scope: Scope = "agent") -> str:
    """Issue a long-lived token bound to `agent`. Only the hash is stored."""
    validate_name(agent)
    if scope not in ISSUABLE_SCOPES:
        raise ValueError(f"scope must be one of {ISSUABLE_SCOPES}")
    existing = conn.execute(
        "select scope from tokens where agent = ? and scope != ? limit 1", (agent, scope)
    ).fetchone()
    if existing:
        raise ValueError(f"{agent!r} already has {existing['scope']!r} tokens")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with conn:
        conn.execute(
            "insert into tokens (token_hash, agent, scope, created_at) values (?, ?, ?, ?)",
            (hash_token(token), agent, scope, now_iso()),
        )
    return token


def create_invite(
    conn: sqlite3.Connection,
    *,
    inviter: str,
    name: str,
    room_id: str,
    role: str | None,
    ttl_seconds: int,
) -> tuple[str, sqlite3.Row]:
    """Issue a room-scoped invite token. Caller owns the surrounding transaction.

    The invitee's identity is namespaced under the inviter ("missy/codex-1"), so an
    invite can never mint an identity that collides with a named agent.
    """
    validate_name(name)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    invite_id = new_id("inv")
    conn.execute(
        "insert into tokens (token_hash, agent, scope, room_id, role, invite_id,"
        " created_by, created_at, expires_at) values (?, ?, 'invite', ?, ?, ?, ?, ?, ?)",
        (
            hash_token(token),
            f"{inviter}/{name}",
            room_id,
            role,
            invite_id,
            inviter,
            now_iso(),
            iso_in(ttl_seconds),
        ),
    )
    row = conn.execute("select * from tokens where invite_id = ?", (invite_id,)).fetchone()
    return token, row


def revoke_tokens(conn: sqlite3.Connection, agent: str) -> int:
    with conn:
        cur = conn.execute(
            "update tokens set revoked_at = ? where agent = ? and revoked_at is null",
            (now_iso(), agent),
        )
    return cur.rowcount


def principal_for_token(conn: sqlite3.Connection, token: str) -> Principal | None:
    row = conn.execute(
        "select * from tokens where token_hash = ? and revoked_at is null"
        " and (expires_at is null or expires_at > ?)",
        (hash_token(token), now_iso()),
    ).fetchone()
    if row is None:
        return None
    return Principal(
        agent=row["agent"],
        scope=row["scope"],
        room_id=row["room_id"],
        role=row["role"],
        invite_id=row["invite_id"],
        expires_at=row["expires_at"],
        token_hash=row["token_hash"],
    )
