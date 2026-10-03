from fastapi import APIRouter, Response, status

from roomsd import db
from roomsd.deps import Caller, Conn
from roomsd.ids import now_iso
from roomsd.models import WhoAmI

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.get("/whoami")
def whoami(caller: Caller) -> WhoAmI:
    return WhoAmI(
        agent=caller.agent,
        scope=caller.scope,
        room_id=caller.room_id,
        role=caller.role,
        invite_id=caller.invite_id,
        expires_at=caller.expires_at,
    )


@router.post("/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_self(conn: Conn, caller: Caller) -> Response:
    """Revoke the token used for this request.

    agentd calls this with a worker's invite token when the session ends, so the
    token dies with the session rather than lingering until it expires.
    """
    with conn:
        conn.execute(
            "update tokens set revoked_at = ? where token_hash = ?",
            (now_iso(), caller.token_hash),
        )
        db.audit(
            conn, caller.agent, "token.revoke_self", caller.room_id, invite_id=caller.invite_id
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
