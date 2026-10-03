import json
import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from roomsd import auth, db
from roomsd.config import Settings
from roomsd.deps import (
    RIGHTS,
    Caller,
    Conn,
    RoomId,
    SettingsDep,
    assert_identity,
    member_row,
    require_participant,
    require_right,
    require_room,
    require_scope,
    require_writable,
    rights_of,
)
from roomsd.ids import iso_in, new_id, now_iso
from roomsd.models import (
    PAYLOAD_FIELDS,
    Invite,
    InviteCreate,
    InviteCreated,
    JoinRequest,
    Member,
    MemberGrant,
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
        admission=row["admission"],
        default_rights=json.loads(row["default_rights_json"]),
        paused=bool(row["paused"]),
        max_hops=row["max_hops"],
        message_rate_per_minute=row["message_rate_per_minute"],
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
    admission = req.admission or settings.default_admission
    default_rights = req.default_rights or ["read", "write", "invite"]
    with conn:
        conn.execute(
            "insert into rooms (id, name, purpose, created_by, created_at, listed, tags_json,"
            " listing_version, admission, default_rights_json, max_hops,"
            " message_rate_per_minute) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                req.name,
                req.purpose,
                caller.agent,
                now,
                int(req.listed),
                json.dumps(req.tags),
                int(req.listed),
                admission,
                json.dumps(default_rights),
                req.max_hops or 8,
                req.message_rate_per_minute,
            ),
        )
        conn.execute(
            "insert into members (room_id, agent, rights_json, granted_by, granted_at)"
            " values (?, ?, ?, ?, ?)",
            (room_id, caller.agent, json.dumps(list(RIGHTS)), caller.agent, now),
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
    """Admins may rename, re-describe, retag, list or unlist, change admission, and
    archive or unarchive the room. An archived room only accepts the unarchive."""
    room = require_right(conn, room_id, caller, "admin")
    changes = req.model_dump(exclude_unset=True)
    if room["archived_at"] is not None and set(changes) != {"archived"}:
        require_writable(room)
    if not changes:
        return room_from_row(room, settings)
    archived_at = room["archived_at"]
    if req.archived is not None:
        archived_at = (archived_at or now_iso()) if req.archived else None
    columns = {
        "name": req.name or room["name"],
        "purpose": req.purpose if "purpose" in changes else room["purpose"],
        "listed": int(req.listed) if req.listed is not None else room["listed"],
        "tags_json": json.dumps(req.tags) if req.tags is not None else room["tags_json"],
        "admission": req.admission or room["admission"],
        "default_rights_json": (
            json.dumps(req.default_rights)
            if req.default_rights is not None
            else room["default_rights_json"]
        ),
        "archived_at": archived_at,
        "paused": int(req.paused) if req.paused is not None else room["paused"],
        "max_hops": req.max_hops or room["max_hops"],
        "message_rate_per_minute": (
            req.message_rate_per_minute
            if "message_rate_per_minute" in changes
            else room["message_rate_per_minute"]
        ),
    }
    # Anything lobbyd shows changed while listed, or listing itself toggled: resync.
    resync = room["listed"] or columns["listed"]
    with conn:
        conn.execute(
            "update rooms set name = ?, purpose = ?, listed = ?, tags_json = ?, admission = ?,"
            " default_rights_json = ?, archived_at = ?, paused = ?, max_hops = ?,"
            " message_rate_per_minute = ?, listing_version = listing_version + ? where id = ?",
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
    sql = "select r.* from rooms r join participants p on p.room_id = r.id where p.agent = ?"
    params: list = [caller.agent]
    if caller.scope == "invite":  # see me.updates: never trust membership alone for guests
        sql += " and r.id = ?"
        params.append(caller.room_id)
    rows = conn.execute(sql + " order by r.created_at desc", params).fetchall()
    return [room_from_row(r, settings) for r in rows]


@router.get("/{room_id}")
def get_room(room_id: RoomId, conn: Conn, caller: Caller, settings: SettingsDep) -> RoomDetail:
    room = require_right(conn, room_id, caller, "read")
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
    room = require_room(conn, room_id, caller)
    require_writable(room)
    grant = member_row(conn, room_id, caller.agent)
    if grant is not None and grant["banned_at"] is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you were removed from this room")
    if (
        caller.scope == "agent"
        and grant is None
        and caller.agent != room["created_by"]
        and room["admission"] != "open"
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this room is closed; an admin must grant you access"
        )
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
        if caller.scope == "agent" and grant is None:
            # Self-join of an open room: the room's default rights.
            conn.execute(
                "insert or ignore into members"
                " (room_id, agent, rights_json, granted_by, granted_at) values (?, ?, ?, ?, ?)",
                (room_id, caller.agent, room["default_rights_json"], "admission:open", now),
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
    room = require_right(conn, room_id, caller, "write")
    require_writable(room)
    if len(req.body.encode()) > settings.max_message_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"body exceeds {settings.max_message_bytes} bytes; publish an artifact instead",
        )
    is_admin = "admin" in rights_of(conn, room, caller)
    if room["paused"] and not is_admin:
        raise HTTPException(status.HTTP_423_LOCKED, "room is paused by its owner")
    hop = None
    if req.in_reply_to is not None:
        parent = conn.execute(
            "select payload_json from messages where id = ? and room_id = ?",
            (req.in_reply_to, room_id),
        ).fetchone()
        if parent is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "in_reply_to is not a message in this room"
            )
        hop = (json.loads(parent["payload_json"] or "{}").get("hop") or 0) + 1
        if hop > room["max_hops"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"reply chain is {hop} deep, over this room's max_hops ({room['max_hops']});"
                " start a new thread or ask a human",
            )
    payload = {k: v for k in PAYLOAD_FIELDS if (v := getattr(req, k, None)) is not None}
    if hop is not None:
        payload["hop"] = hop
    with conn:
        if room["message_rate_per_minute"] and not is_admin:
            conn.execute("begin immediate")
            since = iso_in(-60)
            recent = conn.execute(
                "select count(*) from messages where room_id = ? and created_at > ?"
                " and sender != ?",
                (room_id, since, room["created_by"]),
            ).fetchone()[0]
            if recent >= room["message_rate_per_minute"]:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"room message budget reached ({room['message_rate_per_minute']}/min)",
                    headers={"Retry-After": "60"},
                )
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
        db.audit(
            conn,
            caller.agent,
            "message.post",
            room_id,
            message_id=cur.lastrowid,
            invite_id=caller.invite_id,
        )
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
    require_right(conn, room_id, caller, "read")
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
    require_writable(require_right(conn, room_id, caller, "write"))
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
        db.audit(conn, caller.agent, "note.put", room_id, key=key, invite_id=caller.invite_id)
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
    require_right(conn, room_id, caller, "read")
    sql, params = "select * from notes where room_id = ?", [room_id]
    wanted = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if wanted:
        sql += f" and key in ({','.join('?' * len(wanted))})"
        params += wanted
    rows = conn.execute(sql + " order by key", params).fetchall()
    return NotesResponse(room_id=room_id, notes={r["key"]: note_from_row(r) for r in rows})


@router.get("/{room_id}/notes/{key}")
def get_note(room_id: RoomId, key: str, conn: Conn, caller: Caller) -> Note:
    require_right(conn, room_id, caller, "read")
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
    require_writable(require_right(conn, room_id, caller, "invite"))
    ttl = req.ttl_seconds or settings.default_invite_ttl_seconds
    if ttl > settings.max_invite_ttl_seconds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"ttl_seconds may not exceed {settings.max_invite_ttl_seconds}",
        )
    identity = f"{caller.agent}/{req.name}"
    try:
        with conn:
            # IMMEDIATE takes the write lock now, so the live-invite check and the insert
            # are atomic across concurrent requests.
            conn.execute("begin immediate")
            live = conn.execute(
                "select invite_id, room_id from invites"
                " where agent = ? and revoked_at is null and expires_at > ?",
                (identity, now_iso()),
            ).fetchone()
            if live:
                # One live invite per guest identity, so concurrent guest sessions are
                # always distinguishable. To renew a guest's credential (same identity, same
                # membership), revoke the old invite first.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{identity!r} already has a live invite ({live['invite_id']} in"
                    f" {live['room_id']}); pick another name or revoke that invite first",
                )
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
    require_right(conn, room_id, caller, "invite")
    rows = conn.execute(
        "select * from invites where room_id = ? order by created_at",
        (room_id,),
    ).fetchall()
    return [invite_from_row(r) for r in rows]


@router.delete("/{room_id}/invites/{invite_id}")
def revoke_invite(room_id: RoomId, invite_id: str, conn: Conn, caller: Caller) -> Invite:
    """The inviter or a room admin may revoke an invite."""
    require_scope(caller, "agent")
    room = require_participant(conn, room_id, caller)
    row = conn.execute(
        "select * from invites where invite_id = ? and room_id = ?", (invite_id, room_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    if caller.agent != row["created_by"] and "admin" not in rights_of(conn, room, caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the inviter or an admin can revoke")
    with conn:
        conn.execute(
            "update invites set revoked_at = coalesce(revoked_at, ?) where invite_id = ?",
            (now_iso(), invite_id),
        )
        db.audit(conn, caller.agent, "invite.revoke", room_id, invite_id=invite_id)
    row = conn.execute("select * from invites where invite_id = ?", (invite_id,)).fetchone()
    return invite_from_row(row)


# ----- membership (docs#10) ------------------------------------------------------------


def member_from_row(row: sqlite3.Row, joined: bool) -> Member:
    return Member(
        agent=row["agent"],
        rights=json.loads(row["rights_json"]),
        granted_by=row["granted_by"],
        granted_at=row["granted_at"],
        banned_at=row["banned_at"],
        joined=joined,
    )


@router.get("/{room_id}/members")
def list_members(room_id: RoomId, conn: Conn, caller: Caller) -> list[Member]:
    require_right(conn, room_id, caller, "read")
    joined = {
        r[0] for r in conn.execute("select agent from participants where room_id = ?", (room_id,))
    }
    rows = conn.execute(
        "select * from members where room_id = ? order by granted_at", (room_id,)
    ).fetchall()
    return [member_from_row(r, r["agent"] in joined) for r in rows]


@router.put("/{room_id}/members/{agent:path}")
def grant_member(
    room_id: RoomId, agent: str, req: MemberGrant, conn: Conn, caller: Caller
) -> Member:
    """Grant or change a named agent's rights (admins only). Also lifts a ban: this is how
    an admin re-admits someone, and how a closed room admits an approved peer."""
    require_scope(caller, "agent")
    room = require_right(conn, room_id, caller, "admin")
    if "/" in agent or "@" not in agent:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "grants are for named agents (name@domain)"
        )
    if agent == room["created_by"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "the room creator is always admin")
    with conn:
        conn.execute(
            "insert into members (room_id, agent, rights_json, granted_by, granted_at)"
            " values (?, ?, ?, ?, ?) on conflict (room_id, agent) do update set"
            " rights_json = excluded.rights_json, granted_by = excluded.granted_by,"
            " granted_at = excluded.granted_at, banned_at = null, banned_by = null",
            (room_id, agent, json.dumps(sorted(set(req.rights))), caller.agent, now_iso()),
        )
        db.audit(conn, caller.agent, "member.grant", room_id, target=agent, rights=req.rights)
    row = member_row(conn, room_id, agent)
    joined = conn.execute(
        "select 1 from participants where room_id = ? and agent = ?", (room_id, agent)
    ).fetchone()
    return member_from_row(row, joined is not None)


@router.delete("/{room_id}/members/{agent:path}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    room_id: RoomId,
    agent: str,
    conn: Conn,
    caller: Caller,
    ban: bool = False,
) -> Response:
    """Remove a named agent or a guest (admins only). Removal takes effect on the target's
    next request, whatever tokens it holds. It also invalidates what the target delegated:
    invites it issued are revoked and their guests removed. With ban=true the agent can't
    rejoin until an admin grants it again, even in an open room."""
    require_scope(caller, "agent")
    room = require_right(conn, room_id, caller, "admin")
    if agent == room["created_by"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "the room creator can't be removed")
    now = now_iso()
    with conn:
        guest_prefix = agent + "/"
        conn.execute(
            "delete from participants where room_id = ? and (agent = ? or substr(agent, 1, ?) = ?)",
            (room_id, agent, len(guest_prefix), guest_prefix),
        )
        conn.execute(
            "update invites set revoked_at = coalesce(revoked_at, ?)"
            " where room_id = ? and (agent = ? or created_by = ?)",
            (now, room_id, agent, agent),
        )
        if ban:
            conn.execute(
                "insert into members (room_id, agent, rights_json, granted_by, granted_at,"
                " banned_at, banned_by) values (?, ?, '[]', ?, ?, ?, ?)"
                " on conflict (room_id, agent) do update set rights_json = '[]',"
                " banned_at = excluded.banned_at, banned_by = excluded.banned_by",
                (room_id, agent, caller.agent, now, now, caller.agent),
            )
        else:
            conn.execute("delete from members where room_id = ? and agent = ?", (room_id, agent))
        db.audit(conn, caller.agent, "member.remove", room_id, target=agent, ban=ban)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
