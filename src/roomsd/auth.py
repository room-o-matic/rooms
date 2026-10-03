"""Two kinds of bearer token reach roomsd:

- lobbyd access tokens (JWTs) for named agents. Identity is `name@domain`, verified
  against lobbyd's JWKS by `verify.TokenVerifier`. roomsd stores no credentials for them.
- Invite tokens (opaque, prefixed `rmsd_`), minted by roomsd for guests such as agentd
  workers. They work in one room only, and the identity is `<inviter identity>/<name>`,
  e.g. `missy@local/agentd-host1.codex`. That always contains a `/` and a named
  identity never does, so an invite can't impersonate an agent.
"""

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

from roomsd.ids import iso_in, new_id, now_iso
from roomsd.verify import Claims

INVITE_PREFIX = "rmsd_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

Scope = Literal["agent", "invite"]


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
    if not NAME_RE.match(name):
        raise ValueError(f"invalid name {name!r}: use lowercase [a-z0-9_.-], max 64")


def principal_from_claims(claims: Claims) -> Principal | None:
    """Only agent-scope lobbyd tokens may use roomsd."""
    if claims.scope != "agent":
        return None
    return Principal(agent=claims.identity, scope="agent")


def create_invite(
    conn: sqlite3.Connection,
    *,
    inviter: str,
    name: str,
    room_id: str,
    role: str | None,
    ttl_seconds: int,
) -> tuple[str, sqlite3.Row]:
    """Mint a room-scoped invite token. The caller owns the surrounding transaction."""
    validate_name(name)
    token = INVITE_PREFIX + secrets.token_urlsafe(32)
    invite_id = new_id("inv")
    conn.execute(
        "insert into invites (invite_id, token_hash, agent, room_id, role, created_by,"
        " created_at, expires_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invite_id,
            hash_token(token),
            f"{inviter}/{name}",
            room_id,
            role,
            inviter,
            now_iso(),
            iso_in(ttl_seconds),
        ),
    )
    row = conn.execute("select * from invites where invite_id = ?", (invite_id,)).fetchone()
    return token, row


def principal_for_invite(conn: sqlite3.Connection, token: str) -> Principal | None:
    row = conn.execute(
        "select * from invites where token_hash = ? and revoked_at is null and expires_at > ?",
        (hash_token(token), now_iso()),
    ).fetchone()
    if row is None:
        return None
    return Principal(
        agent=row["agent"],
        scope="invite",
        room_id=row["room_id"],
        role=row["role"],
        invite_id=row["invite_id"],
        expires_at=row["expires_at"],
        token_hash=row["token_hash"],
    )
