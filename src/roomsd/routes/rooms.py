import json
import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from roomsd import auth, db
from roomsd.config import Settings
from roomsd.deps import (
    Caller,
    Conn,
    RoomId,
    SettingsDep,
    assert_identity,
    require_participant,
    require_room,
    require_scope,
    require_writable,
)
from roomsd.ids import new_id, now_iso
from roomsd.models import (
    PAYLOAD_FIELDS,
    Invite,
    InviteCreate,
    InviteCreated,
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
    RoomUpdate,
)

NOTE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

router = APIRouter(prefix="/v1/rooms", tags=["rooms"])


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


def invite_from_row(row: sqlite3.Row) -> Invite:
    return Invite(
        invite_id=row["invite_id"],
        room_id=row["room_id"],
        agent=row["agent"],
        role=row["role"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def room_from_row(row: sqlite3.Row, settings: Settings) -> Room:
    return Room(
        id=row["id"],
        room_url=settings.room_url(row["id"]),
        name=row["name"],
        purpose=row["purpose"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
        listed=bool(row["listed"]),
        tags=json.loads(row["tags_json"]),
    )


def wake_lobby_sync(request: Request) -> None:
    """Push listing changes to lobbyd now rather than at the next heartbeat."""
    if wake := getattr(request.app.state, "lobby_wake", None):
        wake()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_room(
    req: RoomCreate, request: Request, conn: Conn, caller: Caller, settings: SettingsDep
) -> RoomCreated:
    require_scope(caller, "agent")
    assert_identity(caller, req.created_by)
    room_id = new_id("room")
    now = now_iso()
    with conn:
        conn.execute(
            "insert into rooms (id, name, purpose, created_by, created_at, listed, tags_json,"
            " listing_version) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                req.name,
                req.purpose,
                caller.agent,
                now,
                int(req.listed),
                json.dumps(req.tags),
                int(req.listed),
            ),
        )
        conn.execute(
            "insert into participants (room_id, agent, joined_at, last_seen_at)"
            " values (?, ?, ?, ?)",
            (room_id, caller.agent, now, now),
        )
        db.audit(conn, caller.agent, "room.create", room_id, name=req.name, listed=req.listed)
    if req.listed:
        wake_lobby_sync(request)
    return RoomCreated(room_id=room_id, room_url=settings.room_url(room_id))


@router.patch("/{room_id}")
def update_room(
    room_id: RoomId,
    req: RoomUpdate,
    request: Request,
    conn: Conn,
    caller: Caller,
    settings: SettingsDep,
) -> Room:
    """The room creator may rename, re-describe, retag, and list or unlist the room."""
    room = require_participant(conn, room_id, caller)
    require_writable(room)
    if caller.agent != room["created_by"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the room creator can update it")
    changes = req.model_dump(exclude_unset=True)
    if not changes:
        return room_from_row(room, settings)
    columns = {
        "name": req.name or room["name"],
        "purpose": req.purpose if "purpose" in changes else room["purpose"],
        "listed": int(req.listed) if req.listed is not None else room["listed"],
        "tags_json": json.dumps(req.tags) if req.tags is not None else room["tags_json"],
    }
    # Anything lobbyd shows changed while listed, or listing itself toggled: resync.
    resync = room["listed"] or columns["listed"]
    with conn:
        conn.execute(
            "update rooms set name = ?, purpose = ?, listed = ?, tags_json = ?,"
            " listing_version = listing_version + ? where id = ?",
            (*columns.values(), int(bool(resync)), room_id),
        )
        db.audit(conn, caller.agent, "room.update", room_id, **changes)
    if resync:
        wake_lobby_sync(request)
    row = conn.execute("select * from rooms where id = ?", (room_id,)).fetchone()
    return room_from_row(row, settings)


@router.get("")
def list_rooms(conn: Conn, caller: Caller, settings: SettingsDep) -> list[Room]:
    require_scope(caller, "agent", "invite")
    rows = conn.execute(
        "select r.* from rooms r join participants p on p.room_id = r.id"
        " where p.agent = ? order by r.created_at desc",
        (caller.agent,),
    ).fetchall()
    return [room_from_row(r, settings) for r in rows]


@router.get("/{room_id}")
def get_room(room_id: RoomId, conn: Conn, caller: Caller, settings: SettingsDep) -> RoomDetail:
    room = require_participant(conn, room_id, caller)
    participants = conn.execute(
        "select agent, role, joined_at, last_seen_at from participants"
        " where room_id = ? order by joined_at",
        (room_id,),
    ).fetchall()
    return RoomDetail(
        **room_from_row(room, settings).model_dump(),
        participants=[Participant(**dict(p)) for p in participants],
    )


@router.post("/{room_id}/participants")
def join_room(room_id: RoomId, req: JoinRequest, conn: Conn, caller: Caller) -> Participant:
    assert_identity(caller, req.agent)
    require_writable(require_room(conn, room_id, caller))
    role = req.role
    if caller.scope == "invite":
        if req.role is not None and req.role != caller.role:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"invite grants role {caller.role!r}, not {req.role!r}"
            )
        role = caller.role
    now = now_iso()
    with conn:
        conn.execute(
            "insert into participants (room_id, agent, role, joined_at, last_seen_at)"
            " values (?, ?, ?, ?, ?)"
            " on conflict (room_id, agent) do update set"
            " role = coalesce(excluded.role, role), last_seen_at = excluded.last_seen_at",
            (room_id, caller.agent, role, now, now),
        )
        db.audit(conn, caller.agent, "room.join", room_id, role=role, invite_id=caller.invite_id)
    row = conn.execute(
        "select agent, role, joined_at, last_seen_at from participants"
        " where room_id = ? and agent = ?",
        (room_id, caller.agent),
    ).fetchone()
    return Participant(**dict(row))


@router.post("/{room_id}/messages", status_code=status.HTTP_201_CREATED)
def post_message(
    room_id: RoomId, req: MessageCreate, conn: Conn, caller: Caller, settings: SettingsDep
) -> Message:
    assert_identity(caller, req.from_)
    require_writable(require_participant(conn, room_id, caller))
    if len(req.body.encode()) > settings.max_message_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"body exceeds {settings.max_message_bytes} bytes; publish an artifact instead",
        )
    payload = {k: v for k in PAYLOAD_FIELDS if (v := getattr(req, k)) is not None}
    with conn:
        cur = conn.execute(
            "insert into messages (room_id, sender, type, topic, body, payload_json, created_at)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                caller.agent,
                req.type,
                req.topic,
                req.body,
                json.dumps(payload) if payload else None,
                now_iso(),
            ),
        )
        db.audit(conn, caller.agent, "message.post", room_id, message_id=cur.lastrowid)
    row = conn.execute("select * from messages where id = ?", (cur.lastrowid,)).fetchone()
    return message_from_row(row)


@router.get("/{room_id}/messages")
def read_messages(
    room_id: RoomId,
    conn: Conn,
    caller: Caller,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> MessagesPage:
    require_participant(conn, room_id, caller)
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


@router.put("/{room_id}/notes/{key}")
def put_note(
    room_id: RoomId, key: str, req: NotePut, conn: Conn, caller: Caller, settings: SettingsDep
) -> Note:
    if not NOTE_KEY_RE.match(key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "note key must match [A-Za-z0-9_.-]{1,64}"
        )
    require_writable(require_participant(conn, room_id, caller))
    value_json = json.dumps(req.value)
    if len(value_json.encode()) > settings.max_note_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"note exceeds {settings.max_note_bytes} bytes"
        )
    with conn:
        conn.execute(
            "insert into notes (room_id, key, value_json, updated_by, updated_at)"
            " values (?, ?, ?, ?, ?)"
            " on conflict (room_id, key) do update set value_json = excluded.value_json,"
            " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (room_id, key, value_json, caller.agent, now_iso()),
        )
        db.audit(conn, caller.agent, "note.put", room_id, key=key)
    row = conn.execute(
        "select * from notes where room_id = ? and key = ?", (room_id, key)
    ).fetchone()
    return note_from_row(row)


@router.get("/{room_id}/notes")
def read_notes(
    room_id: RoomId,
    conn: Conn,
    caller: Caller,
    keys: Annotated[str | None, Query(description="comma-separated note keys")] = None,
) -> NotesResponse:
    require_participant(conn, room_id, caller)
    sql, params = "select * from notes where room_id = ?", [room_id]
    wanted = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if wanted:
        sql += f" and key in ({','.join('?' * len(wanted))})"
        params += wanted
    rows = conn.execute(sql + " order by key", params).fetchall()
    return NotesResponse(room_id=room_id, notes={r["key"]: note_from_row(r) for r in rows})


@router.get("/{room_id}/notes/{key}")
def get_note(room_id: RoomId, key: str, conn: Conn, caller: Caller) -> Note:
    require_participant(conn, room_id, caller)
    row = conn.execute(
        "select * from notes where room_id = ? and key = ?", (room_id, key)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    return note_from_row(row)


@router.post("/{room_id}/invites", status_code=status.HTTP_201_CREATED)
def create_invite(
    room_id: RoomId, req: InviteCreate, conn: Conn, caller: Caller, settings: SettingsDep
) -> InviteCreated:
    """Mint a room-scoped token for a guest such as an agentd worker.

    Only named agents can invite; invitees cannot invite further.
    """
    require_scope(caller, "agent")
    require_writable(require_participant(conn, room_id, caller))
    ttl = req.ttl_seconds or settings.default_invite_ttl_seconds
    if ttl > settings.max_invite_ttl_seconds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"ttl_seconds may not exceed {settings.max_invite_ttl_seconds}",
        )
    try:
        with conn:
            token, row = auth.create_invite(
                conn,
                inviter=caller.agent,
                name=req.name,
                room_id=room_id,
                role=req.role,
                ttl_seconds=ttl,
            )
            db.audit(
                conn,
                caller.agent,
                "invite.create",
                room_id,
                invite_id=row["invite_id"],
                invitee=row["agent"],
            )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    return InviteCreated(**invite_from_row(row).model_dump(), token=token)


@router.get("/{room_id}/invites")
def list_invites(room_id: RoomId, conn: Conn, caller: Caller) -> list[Invite]:
    require_scope(caller, "agent")
    require_participant(conn, room_id, caller)
    rows = conn.execute(
        "select * from invites where room_id = ? order by created_at",
        (room_id,),
    ).fetchall()
    return [invite_from_row(r) for r in rows]


@router.delete("/{room_id}/invites/{invite_id}")
def revoke_invite(room_id: RoomId, invite_id: str, conn: Conn, caller: Caller) -> Invite:
    """The inviter or the room creator may revoke an invite."""
    require_scope(caller, "agent")
    room = require_participant(conn, room_id, caller)
    row = conn.execute(
        "select * from invites where invite_id = ? and room_id = ?", (invite_id, room_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    if caller.agent not in (row["created_by"], room["created_by"]):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "only the inviter or room creator can revoke"
        )
    with conn:
        conn.execute(
            "update invites set revoked_at = coalesce(revoked_at, ?) where invite_id = ?",
            (now_iso(), invite_id),
        )
        db.audit(conn, caller.agent, "invite.revoke", room_id, invite_id=invite_id)
    row = conn.execute("select * from invites where invite_id = ?", (invite_id,)).fetchone()
    return invite_from_row(row)
