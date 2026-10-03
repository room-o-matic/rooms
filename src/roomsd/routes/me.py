from typing import Annotated

from fastapi import APIRouter, Query

from roomsd.deps import Caller, Conn, SettingsDep, require_scope
from roomsd.models import UpdatesPage
from roomsd.routes.rooms import message_from_row

router = APIRouter(prefix="/v1/me", tags=["me"])


@router.get("/updates")
def updates(
    conn: Conn,
    caller: Caller,
    settings: SettingsDep,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> UpdatesPage:
    """One poll for every joined room: message IDs are server-wide, so one cursor works.

    Pass the returned next_cursor back as `cursor`. An invite token sees only its own
    room, enforced here by the query: identity alone is not enough, because a guest
    identity can have stale membership in another room from an earlier invite.
    """
    require_scope(caller, "agent", "invite")
    sql = (
        "select m.* from messages m join participants p"
        " on p.room_id = m.room_id and p.agent = ?"
        " where m.id > ?"
    )
    params: list = [caller.agent, cursor]
    if caller.scope == "invite":
        sql += " and m.room_id = ?"
        params.append(caller.room_id)
    else:  # docs#10: only rooms where the caller holds "read"
        sql += (
            " and (exists (select 1 from rooms r where r.id = m.room_id and r.created_by = ?)"
            " or exists (select 1 from members mb, json_each(mb.rights_json) j"
            " where mb.room_id = m.room_id and mb.agent = ? and mb.banned_at is null"
            " and j.value = 'read'))"
        )
        params += [caller.agent, caller.agent]
    rows = conn.execute(sql + " order by m.id limit ?", [*params, limit]).fetchall()
    messages = [message_from_row(r) for r in rows]
    return UpdatesPage(
        messages=messages,
        room_urls={m.room_id: settings.room_url(m.room_id) for m in messages},
        next_cursor=messages[-1].id if messages else cursor,
    )
