from typing import Annotated

from fastapi import APIRouter, Query

from roomsd.deps import Caller, Conn, SettingsDep, require_scope
from roomsd.ids import now_iso
from roomsd.models import UpdatesPage
from roomsd.routes.rooms import message_from_row, within_page_budget

router = APIRouter(prefix="/v1/me", tags=["me"])


@router.get("/updates")
def updates(
    conn: Conn,
    caller: Caller,
    settings: SettingsDep,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> UpdatesPage:
    """One poll for every room you can read: message IDs are server-wide, so one cursor
    works. For a named agent that's every room it created or holds `read` in, joined or
    not: someone who grants you a room and @-mentions you shouldn't go unseen until you
    happen to join. An invite token covers only its own room, once joined.

    Pass the returned next_cursor back as `cursor`. An invite token sees only its own
    room, enforced here by the query: identity alone is not enough, because a guest
    identity can have stale membership in another room from an earlier invite.
    """
    require_scope(caller, "agent", "invite")
    # The rooms this poll covers, as a condition on `room` (a room id expression).
    if caller.scope == "invite":
        covers, cover_params = "{room} = ?", [caller.room_id]
    else:  # docs#10: only rooms where the caller holds "read"
        covers = (
            "(exists (select 1 from rooms r where r.id = {room} and r.created_by = ?)"
            " or exists (select 1 from members mb, json_each(mb.rights_json) j"
            " where mb.room_id = {room} and mb.agent = ? and mb.banned_at is null"
            " and j.value = 'read'))"
        )
        cover_params = [caller.agent, caller.agent]
    # docs#23: polling the feed, even an empty poll, is presence in every room it covers,
    # and only those: an invite token refreshes its own room, never a same-named guest's
    # membership elsewhere.
    with conn:
        conn.execute(
            "update participants set last_seen_at = ? where agent = ? and "
            + covers.format(room="participants.room_id"),
            [now_iso(), caller.agent, *cover_params],
        )
    if caller.scope == "invite":
        sql = (
            "select m.* from messages m join participants p"
            " on p.room_id = m.room_id and p.agent = ?"
            " where m.id > ? and " + covers.format(room="m.room_id")
        )
        params: list = [caller.agent, cursor, *cover_params]
    else:
        sql = "select m.* from messages m where m.id > ? and " + covers.format(room="m.room_id")
        params = [cursor, *cover_params]
    rows = conn.execute(sql + " order by m.id limit ?", [*params, limit]).fetchall()
    messages = [message_from_row(r) for r in within_page_budget(rows, settings.max_page_bytes)]
    return UpdatesPage(
        messages=messages,
        room_urls={m.room_id: settings.room_url(m.room_id) for m in messages},
        next_cursor=messages[-1].id if messages else cursor,
    )
