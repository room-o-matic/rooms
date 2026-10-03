import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from roomsd import auth, db
from roomsd.auth import Principal, Scope
from roomsd.config import Settings
from roomsd.ids import now_iso

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
RoomId = Annotated[str, Path(max_length=64)]


def current_principal(
    conn: Conn,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    principal = auth.principal_for_token(conn, creds.credentials) if creds else None
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing, invalid, expired, or revoked bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


Caller = Annotated[Principal, Depends(current_principal)]


def require_scope(caller: Principal, *scopes: Scope) -> None:
    if caller.scope not in scopes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"{caller.scope!r} tokens cannot use this endpoint"
        )


def assert_identity(caller: Principal, claimed: str | None) -> None:
    """Identity comes from the token; a body field may repeat it but never override it."""
    if claimed is not None and claimed != caller.agent:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"token belongs to {caller.agent!r}; cannot act as {claimed!r}",
        )


def require_room(conn: sqlite3.Connection, room_id: str, caller: Principal) -> sqlite3.Row:
    """Room routes accept named agents anywhere and invitees only in their own room."""
    require_scope(caller, "agent", "invite")
    if caller.scope == "invite" and caller.room_id != room_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invite token is for a different room")
    row = conn.execute("select * from rooms where id = ?", (room_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "room not found")
    return row


def require_participant(conn: sqlite3.Connection, room_id: str, caller: Principal) -> sqlite3.Row:
    """Ensure the room exists and the caller has joined it; refresh last_seen_at."""
    room = require_room(conn, room_id, caller)
    with conn:
        cur = conn.execute(
            "update participants set last_seen_at = ? where room_id = ? and agent = ?",
            (now_iso(), room_id, caller.agent),
        )
    if cur.rowcount == 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "join the room first")
    return room


def require_writable(room: sqlite3.Row) -> None:
    if room["archived_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "room is archived")
