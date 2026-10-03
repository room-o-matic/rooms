import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from roomsd import auth, db
from roomsd.config import Settings
from roomsd.ids import new_id, now_iso
from roomsd.models import (
    PAYLOAD_FIELDS,
    JoinRequest,
    Message,
    MessageCreate,
    MessagesPage,
    Note,
    NotePut,
    NotesResponse,
    Participant,
    Room,
    RoomCreate,
    RoomCreated,
    RoomDetail,
)

NOTE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_bearer = HTTPBearer(auto_error=False)


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def current_agent(
    conn: Conn,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    agent = auth.agent_for_token(conn, creds.credentials) if creds else None
    if agent is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return agent


Agent = Annotated[str, Depends(current_agent)]
RoomId = Annotated[str, Path(max_length=64)]


def assert_identity(agent: str, claimed: str | None) -> None:
    """Identity comes from the token; a body field may repeat it but never override it."""
    if claimed is not None and claimed != agent:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"token belongs to {agent!r}; cannot act as {claimed!r}",
        )


def require_room(conn: sqlite3.Connection, room_id: str) -> sqlite3.Row:
    row = conn.execute("select * from rooms where id = ?", (room_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "room not found")
    return row


def require_participant(conn: sqlite3.Connection, room_id: str, agent: str) -> sqlite3.Row:
    """Ensure the room exists and `agent` has joined it; refresh last_seen_at."""
    room = require_room(conn, room_id)
    with conn:
        cur = conn.execute(
            "update participants set last_seen_at = ? where room_id = ? and agent = ?",
            (now_iso(), room_id, agent),
        )
    if cur.rowcount == 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "join the room first")
    return room


def require_writable(room: sqlite3.Row) -> None:
    if room["archived_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "room is archived")


def message_from_row(row: sqlite3.Row) -> Message:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    return Message(
        id=row["id"],
        room_id=row["room_id"],
        from_=row["sender"],
        type=row["type"],
        topic=row["topic"],
        body=row["body"],
        created_at=row["created_at"],
        **{k: payload[k] for k in PAYLOAD_FIELDS if k in payload},
    )


def note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        key=row["key"],
        value=json.loads(row["value_json"]),
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init_db(settings.db_path)

    app = FastAPI(title="roomsd", version="0.1.0")
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/rooms", status_code=status.HTTP_201_CREATED)
    def create_room(req: RoomCreate, conn: Conn, agent: Agent) -> RoomCreated:
        assert_identity(agent, req.created_by)
        room_id = new_id("room")
        now = now_iso()
        with conn:
            conn.execute(
                "insert into rooms (id, name, purpose, created_by, created_at)"
                " values (?, ?, ?, ?, ?)",
                (room_id, req.name, req.purpose, agent, now),
            )
            conn.execute(
                "insert into participants (room_id, agent, joined_at, last_seen_at)"
                " values (?, ?, ?, ?)",
                (room_id, agent, now, now),
            )
            db.audit(conn, agent, "room.create", room_id, name=req.name)
        return RoomCreated(room_id=room_id)

    @app.get("/v1/rooms")
    def list_rooms(conn: Conn, agent: Agent) -> list[Room]:
        rows = conn.execute(
            "select r.* from rooms r join participants p on p.room_id = r.id"
            " where p.agent = ? order by r.created_at desc",
            (agent,),
        ).fetchall()
        return [Room(**dict(r)) for r in rows]

    @app.get("/v1/rooms/{room_id}")
    def get_room(room_id: RoomId, conn: Conn, agent: Agent) -> RoomDetail:
        room = require_participant(conn, room_id, agent)
        participants = conn.execute(
            "select agent, role, joined_at, last_seen_at from participants"
            " where room_id = ? order by joined_at",
            (room_id,),
        ).fetchall()
        return RoomDetail(**dict(room), participants=[Participant(**dict(p)) for p in participants])

    @app.post("/v1/rooms/{room_id}/participants")
    def join_room(room_id: RoomId, req: JoinRequest, conn: Conn, agent: Agent) -> Participant:
        assert_identity(agent, req.agent)
        require_writable(require_room(conn, room_id))
        now = now_iso()
        with conn:
            conn.execute(
                "insert into participants (room_id, agent, role, joined_at, last_seen_at)"
                " values (?, ?, ?, ?, ?)"
                " on conflict (room_id, agent) do update set"
                " role = coalesce(excluded.role, role), last_seen_at = excluded.last_seen_at",
                (room_id, agent, req.role, now, now),
            )
            db.audit(conn, agent, "room.join", room_id, role=req.role)
        row = conn.execute(
            "select agent, role, joined_at, last_seen_at from participants"
            " where room_id = ? and agent = ?",
            (room_id, agent),
        ).fetchone()
        return Participant(**dict(row))

    @app.post("/v1/rooms/{room_id}/messages", status_code=status.HTTP_201_CREATED)
    def post_message(room_id: RoomId, req: MessageCreate, conn: Conn, agent: Agent) -> Message:
        assert_identity(agent, req.from_)
        require_writable(require_participant(conn, room_id, agent))
        if len(req.body.encode()) > settings.max_message_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"body exceeds {settings.max_message_bytes} bytes; publish an artifact instead",
            )
        payload = {k: v for k in PAYLOAD_FIELDS if (v := getattr(req, k)) is not None}
        with conn:
            cur = conn.execute(
                "insert into messages"
                " (room_id, sender, type, topic, body, payload_json, created_at)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    agent,
                    req.type,
                    req.topic,
                    req.body,
                    json.dumps(payload) if payload else None,
                    now_iso(),
                ),
            )
            db.audit(conn, agent, "message.post", room_id, message_id=cur.lastrowid)
        row = conn.execute("select * from messages where id = ?", (cur.lastrowid,)).fetchone()
        return message_from_row(row)

    @app.get("/v1/rooms/{room_id}/messages")
    def read_messages(
        room_id: RoomId,
        conn: Conn,
        agent: Agent,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> MessagesPage:
        require_participant(conn, room_id, agent)
        rows = conn.execute(
            "select * from messages where room_id = ? and id > ? order by id limit ?",
            (room_id, after_id, limit),
        ).fetchall()
        messages = [message_from_row(r) for r in rows]
        return MessagesPage(
            room_id=room_id,
            messages=messages,
            latest_message_id=messages[-1].id if messages else after_id,
        )

    @app.put("/v1/rooms/{room_id}/notes/{key}")
    def put_note(room_id: RoomId, key: str, req: NotePut, conn: Conn, agent: Agent) -> Note:
        if not NOTE_KEY_RE.match(key):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "note key must match [A-Za-z0-9_.-]{1,64}"
            )
        require_writable(require_participant(conn, room_id, agent))
        value_json = json.dumps(req.value)
        if len(value_json.encode()) > settings.max_note_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"note exceeds {settings.max_note_bytes} bytes",
            )
        with conn:
            conn.execute(
                "insert into notes (room_id, key, value_json, updated_by, updated_at)"
                " values (?, ?, ?, ?, ?)"
                " on conflict (room_id, key) do update set value_json = excluded.value_json,"
                " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (room_id, key, value_json, agent, now_iso()),
            )
            db.audit(conn, agent, "note.put", room_id, key=key)
        row = conn.execute(
            "select * from notes where room_id = ? and key = ?", (room_id, key)
        ).fetchone()
        return note_from_row(row)

    @app.get("/v1/rooms/{room_id}/notes")
    def read_notes(
        room_id: RoomId,
        conn: Conn,
        agent: Agent,
        keys: Annotated[str | None, Query(description="comma-separated note keys")] = None,
    ) -> NotesResponse:
        require_participant(conn, room_id, agent)
        sql, params = "select * from notes where room_id = ?", [room_id]
        wanted = [k.strip() for k in (keys or "").split(",") if k.strip()]
        if wanted:
            sql += f" and key in ({','.join('?' * len(wanted))})"
            params += wanted
        rows = conn.execute(sql + " order by key", params).fetchall()
        return NotesResponse(room_id=room_id, notes={r["key"]: note_from_row(r) for r in rows})

    @app.get("/v1/rooms/{room_id}/notes/{key}")
    def get_note(room_id: RoomId, key: str, conn: Conn, agent: Agent) -> Note:
        require_participant(conn, room_id, agent)
        row = conn.execute(
            "select * from notes where room_id = ? and key = ?", (room_id, key)
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
        return note_from_row(row)

    return app
