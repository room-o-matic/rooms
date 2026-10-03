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

    Pass the returned next_cursor back as `cursor`. Invite tokens only ever see their
    own room, because they can only join that one.
    """
    require_scope(caller, "agent", "invite")
    rows = conn.execute(
        "select m.* from messages m join participants p"
        " on p.room_id = m.room_id and p.agent = ?"
        " where m.id > ? order by m.id limit ?",
        (caller.agent, cursor, limit),
    ).fetchall()
    messages = [message_from_row(r) for r in rows]
    return UpdatesPage(
        messages=messages,
        room_urls={m.room_id: settings.room_url(m.room_id) for m in messages},
        next_cursor=messages[-1].id if messages else cursor,
    )
