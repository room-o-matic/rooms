"""Registry of agentd instances.

Each agentd instance holds an `agentd`-scoped token whose agent name is its instance_id,
and keeps its entry alive by re-PUTting it (a heartbeat) before `expires_at`. Entries
past `expires_at` are invisible to lookups. Orchestrators (named agents) query the
registry to pick an instance by worker type, profile, and spare capacity.
"""

import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from roomsd import db
from roomsd.auth import Principal
from roomsd.deps import Caller, Conn, SettingsDep, require_scope
from roomsd.ids import iso_in, now_iso
from roomsd.models import AgentdInstance, AgentdRegistration

router = APIRouter(prefix="/v1/registry/agentd", tags=["registry"])

InstanceId = Annotated[str, Path(max_length=64)]


def instance_from_row(row: sqlite3.Row) -> AgentdInstance:
    return AgentdInstance(
        instance_id=row["instance_id"],
        base_url=row["base_url"],
        worker_types=json.loads(row["worker_types_json"]),
        profiles=json.loads(row["profiles_json"]),
        max_sessions=row["max_sessions"],
        active_sessions=row["active_sessions"],
        available_sessions=max(0, row["max_sessions"] - row["active_sessions"]),
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        registered_at=row["registered_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        expires_at=row["expires_at"],
    )


def require_self(caller: Principal, instance_id: str) -> None:
    require_scope(caller, "agentd")
    if caller.agent != instance_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"token belongs to {caller.agent!r}; cannot manage {instance_id!r}",
        )


@router.put("/{instance_id}")
def register(
    instance_id: InstanceId,
    req: AgentdRegistration,
    conn: Conn,
    caller: Caller,
    settings: SettingsDep,
) -> AgentdInstance:
    """Register or heartbeat. The same call does both; send it every ttl/3 or so."""
    require_self(caller, instance_id)
    ttl = req.ttl_seconds or settings.default_registry_ttl_seconds
    if ttl > settings.max_registry_ttl_seconds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"ttl_seconds may not exceed {settings.max_registry_ttl_seconds}",
        )
    now = now_iso()
    prev = conn.execute(
        "select registered_at, expires_at from agentd_instances where instance_id = ?",
        (instance_id,),
    ).fetchone()
    is_new = prev is None or prev["expires_at"] <= now
    with conn:
        conn.execute(
            "insert or replace into agentd_instances (instance_id, base_url, worker_types_json,"
            " profiles_json, max_sessions, active_sessions, metadata_json, registered_at,"
            " last_heartbeat_at, expires_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instance_id,
                req.base_url,
                json.dumps(req.worker_types),
                json.dumps(req.profiles),
                req.max_sessions,
                req.active_sessions,
                json.dumps(req.metadata) if req.metadata is not None else None,
                now if is_new else prev["registered_at"],
                now,
                iso_in(ttl),
            ),
        )
        # Heartbeats are frequent; only audit (re)registrations.
        if is_new:
            db.audit(conn, caller.agent, "registry.register", None, base_url=req.base_url)
    row = conn.execute(
        "select * from agentd_instances where instance_id = ?", (instance_id,)
    ).fetchone()
    return instance_from_row(row)


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
def deregister(instance_id: InstanceId, conn: Conn, caller: Caller) -> Response:
    require_self(caller, instance_id)
    with conn:
        conn.execute("delete from agentd_instances where instance_id = ?", (instance_id,))
        db.audit(conn, caller.agent, "registry.deregister", None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("")
def list_instances(
    conn: Conn,
    caller: Caller,
    worker_type: str | None = None,
    profile: str | None = None,
    has_capacity: Annotated[bool, Query(description="only instances with a free slot")] = False,
) -> list[AgentdInstance]:
    """Live instances, most spare capacity first."""
    require_scope(caller, "agent")
    sql = "select * from agentd_instances where expires_at > ?"
    params: list = [now_iso()]
    if worker_type:
        sql += " and exists (select 1 from json_each(worker_types_json) where value = ?)"
        params.append(worker_type)
    if profile:
        sql += " and exists (select 1 from json_each(profiles_json) where value = ?)"
        params.append(profile)
    if has_capacity:
        sql += " and active_sessions < max_sessions"
    sql += " order by (max_sessions - active_sessions) desc, last_heartbeat_at desc"
    return [instance_from_row(r) for r in conn.execute(sql, params).fetchall()]


@router.get("/{instance_id}")
def get_instance(instance_id: InstanceId, conn: Conn, caller: Caller) -> AgentdInstance:
    if caller.scope != "agentd" or caller.agent != instance_id:
        require_scope(caller, "agent")
    row = conn.execute(
        "select * from agentd_instances where instance_id = ? and expires_at > ?",
        (instance_id, now_iso()),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instance not registered or expired")
    return instance_from_row(row)
