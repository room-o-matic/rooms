"""Durable task assignments with lease-fenced claims (room-o-matic/docs#12).

A message saying "claimed" or "done" can't arbitrate work between independent sessions;
these resources can. The rules:

- One active claim per task. Every new claim increments `generation`, the fencing token.
  Renewing, releasing, changing state and completing all require the current owner,
  the current generation and an unexpired lease, so a stale session (whose lease lapsed and
  whose task was reclaimed) can never mutate or complete the task.
- Leases use the server's UTC clock and are evaluated per request. An expired lease leaves
  the task claimable; the old holder can't renew it.
- Completion needs a receipt: a result message in this room posted by the claimant, plus
  at least one piece of evidence (commits, a PR, artifact hashes or test results). Prose
  alone is refused. Tasks may require review (creator/admin accepts or rejects).
- Every mutation and its task_events row commit in one transaction; GET .../tasks/events
  resumes from a cursor, and all state survives restarts.

Fencing protects roomsd's state, not the outside world: an adapter must still gate its
own side effects (pushes, deployments) on holding a live claim, and make them idempotent.
"""

import json
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from roomsd import db
from roomsd.deps import (
    Caller,
    Conn,
    RoomId,
    require_right,
    require_scope,
    require_writable,
    rights_of,
)
from roomsd.ids import iso_in, new_id, now_iso

router = APIRouter(prefix="/v1/rooms/{room_id}/tasks", tags=["tasks"])

TaskState = Literal["open", "claimed", "in_progress", "blocked", "review", "done", "cancelled"]
ACTIVE = ("claimed", "in_progress", "blocked")
TERMINAL = ("done", "cancelled")
SHA = r"^[0-9a-f]{7,64}$"
URL = r"^(?i:https?)://\S+$"


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=16 * 1024)
    task_key: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:#/-]{1,120}$",
        description="idempotency key within the room, e.g. 'docs#12'",
    )
    issue_url: str | None = Field(default=None, pattern=URL, max_length=2048)
    repo: str | None = Field(default=None, max_length=200, description="e.g. room-o-matic/rooms")
    base_commit: str | None = Field(default=None, pattern=SHA, description="immutable base")
    review_required: bool = False


class Commit(BaseModel):
    repo: str = Field(max_length=200)
    sha: str = Field(pattern=SHA)


class ArtifactRef(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TestRun(BaseModel):
    command: str = Field(min_length=1, max_length=1000)
    result: Literal["passed", "failed"]
    summary: str | None = Field(default=None, max_length=2000)


class Receipt(BaseModel):
    result_message_id: int = Field(description="a message in this room posted by the claimant")
    commits: list[Commit] = Field(default_factory=list, max_length=50)
    pr_url: str | None = Field(default=None, pattern=URL, max_length=2048)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    tests: list[TestRun] = Field(default_factory=list, max_length=50)
    reviewed_base: str | None = Field(default=None, pattern=SHA)


class Claim(BaseModel):
    session_id: str | None = Field(default=None, max_length=120)
    lease_seconds: int = Field(default=900, ge=30, le=24 * 3600)


class Fenced(BaseModel):
    generation: int = Field(ge=1, description="the fencing token from your claim")


class Renew(Fenced):
    lease_seconds: int = Field(default=900, ge=30, le=24 * 3600)


class StateChange(Fenced):
    state: Literal["in_progress", "blocked"]


class Complete(Fenced):
    receipt: Receipt


class Review(BaseModel):
    decision: Literal["accept", "reject"]
    note: str | None = Field(default=None, max_length=2000)


class Task(BaseModel):
    task_id: str
    room_id: str
    task_key: str | None
    title: str
    description: str | None
    issue_url: str | None
    repo: str | None
    base_commit: str | None
    review_required: bool
    state: TaskState
    created_by: str
    created_at: str
    updated_at: str
    claim_owner: str | None
    claim_session: str | None
    claim_generation: int
    lease_expires_at: str | None
    receipt: Receipt | None
    completed_at: str | None


class TaskResult(BaseModel):
    task: Task
    changed: bool


class TaskEvent(BaseModel):
    id: int
    task_id: str
    kind: str
    actor: str
    generation: int
    detail: dict | None
    created_at: str


class TaskEvents(BaseModel):
    events: list[TaskEvent]
    next_cursor: int


def task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        **{k: row[k] for k in row.keys() if k not in ("receipt_json", "review_required")},
        review_required=bool(row["review_required"]),
        receipt=Receipt(**json.loads(row["receipt_json"])) if row["receipt_json"] else None,
    )


def _get(conn: sqlite3.Connection, room_id: str, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "select * from tasks where task_id = ? and room_id = ?", (task_id, room_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return row


def _event(conn, row, kind: str, actor: str, generation: int, **detail) -> None:
    conn.execute(
        "insert into task_events (task_id, room_id, kind, actor, generation, detail_json,"
        " created_at) values (?, ?, ?, ?, ?, ?, ?)",
        (
            row["task_id"],
            row["room_id"],
            kind,
            actor,
            generation,
            json.dumps(detail) if detail else None,
            now_iso(),
        ),
    )
    db.audit(
        conn, actor, f"task.{kind}", row["room_id"], task_id=row["task_id"], generation=generation
    )


def _result(conn: sqlite3.Connection, room_id: str, task_id: str, changed: bool) -> TaskResult:
    return TaskResult(task=task_from_row(_get(conn, room_id, task_id)), changed=changed)


def _lease_live(row: sqlite3.Row) -> bool:
    return row["lease_expires_at"] is not None and row["lease_expires_at"] > now_iso()


def _require_holder(row: sqlite3.Row, caller, generation: int) -> None:
    """The fence: current owner, current generation, unexpired lease."""
    if row["state"] not in ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"task is {row['state']}")
    if row["claim_owner"] != caller.agent or row["claim_generation"] != generation:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"stale claim: task is held by generation {row['claim_generation']}",
        )
    if not _lease_live(row):
        raise HTTPException(status.HTTP_409_CONFLICT, "your lease expired; claim again")


def _begin(conn: sqlite3.Connection, room_id: str, caller, right: str) -> sqlite3.Row:
    room = require_right(conn, room_id, caller, right)
    require_writable(room)
    conn.execute("begin immediate")
    return room


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(
    room_id: RoomId, req: TaskCreate, response: Response, conn: Conn, caller: Caller
) -> Task:
    require_scope(caller, "agent")
    with conn:
        _begin(conn, room_id, caller, "write")
        if req.task_key:
            existing = conn.execute(
                "select * from tasks where room_id = ? and task_key = ?", (room_id, req.task_key)
            ).fetchone()
            if existing:
                response.status_code = status.HTTP_200_OK
                return task_from_row(existing)
        task_id, now = new_id("task"), now_iso()
        conn.execute(
            "insert into tasks (task_id, room_id, task_key, title, description, issue_url,"
            " repo, base_commit, review_required, state, created_by, created_at, updated_at)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (
                task_id,
                room_id,
                req.task_key,
                req.title,
                req.description,
                req.issue_url,
                req.repo,
                req.base_commit,
                int(req.review_required),
                caller.agent,
                now,
                now,
            ),
        )
        row = _get(conn, room_id, task_id)
        _event(conn, row, "create", caller.agent, 0, title=req.title)
    return task_from_row(_get(conn, room_id, task_id))


@router.get("")
def list_tasks(
    room_id: RoomId,
    conn: Conn,
    caller: Caller,
    state: TaskState | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[Task]:
    require_right(conn, room_id, caller, "read")
    sql, params = "select * from tasks where room_id = ?", [room_id]
    if state:
        sql += " and state = ?"
        params.append(state)
    rows = conn.execute(sql + " order by created_at limit ?", [*params, limit]).fetchall()
    return [task_from_row(r) for r in rows]


@router.get("/events")
def task_events(
    room_id: RoomId,
    conn: Conn,
    caller: Caller,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TaskEvents:
    """Assignment, lease and result changes, resumable from `after`."""
    require_right(conn, room_id, caller, "read")
    rows = conn.execute(
        "select * from task_events where room_id = ? and id > ? order by id limit ?",
        (room_id, after, limit),
    ).fetchall()
    events = [
        TaskEvent(
            id=r["id"],
            task_id=r["task_id"],
            kind=r["kind"],
            actor=r["actor"],
            generation=r["generation"],
            detail=json.loads(r["detail_json"]) if r["detail_json"] else None,
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return TaskEvents(events=events, next_cursor=events[-1].id if events else after)


@router.get("/{task_id}")
def get_task(room_id: RoomId, task_id: str, conn: Conn, caller: Caller) -> Task:
    require_right(conn, room_id, caller, "read")
    return task_from_row(_get(conn, room_id, task_id))


@router.post("/{task_id}/claim")
def claim_task(room_id: RoomId, task_id: str, req: Claim, conn: Conn, caller: Caller) -> TaskResult:
    """Claim an open task, or take over one whose lease expired. Retrying while you hold a
    live claim from the same session returns it unchanged (changed=false)."""
    with conn:
        _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        if (
            row["state"] in ACTIVE
            and row["claim_owner"] == caller.agent
            and row["claim_session"] == req.session_id
            and _lease_live(row)
        ):
            return _result(conn, room_id, task_id, changed=False)
        claimable = row["state"] == "open" or (row["state"] in ACTIVE and not _lease_live(row))
        if not claimable:
            raise HTTPException(status.HTTP_409_CONFLICT, f"task is {row['state']}")
        generation = row["claim_generation"] + 1
        conn.execute(
            "update tasks set state = 'claimed', claim_owner = ?, claim_session = ?,"
            " claim_generation = ?, lease_expires_at = ?, updated_at = ?"
            " where task_id = ? and claim_generation = ?",
            (
                caller.agent,
                req.session_id,
                generation,
                iso_in(req.lease_seconds),
                now_iso(),
                task_id,
                row["claim_generation"],
            ),
        )
        kind = "takeover" if row["state"] in ACTIVE else "claim"
        _event(
            conn,
            row,
            kind,
            caller.agent,
            generation,
            session_id=req.session_id,
            previous_owner=row["claim_owner"],
        )
        return _result(conn, room_id, task_id, changed=True)


@router.post("/{task_id}/renew")
def renew_task(room_id: RoomId, task_id: str, req: Renew, conn: Conn, caller: Caller) -> TaskResult:
    with conn:
        _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        _require_holder(row, caller, req.generation)
        conn.execute(
            "update tasks set lease_expires_at = ?, updated_at = ? where task_id = ?",
            (iso_in(req.lease_seconds), now_iso(), task_id),
        )
        _event(conn, row, "renew", caller.agent, req.generation)
        return _result(conn, room_id, task_id, changed=True)


@router.post("/{task_id}/release")
def release_task(
    room_id: RoomId, task_id: str, req: Fenced, conn: Conn, caller: Caller
) -> TaskResult:
    with conn:
        _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        _require_holder(row, caller, req.generation)
        conn.execute(
            "update tasks set state = 'open', claim_owner = null, claim_session = null,"
            " lease_expires_at = null, updated_at = ? where task_id = ?",
            (now_iso(), task_id),
        )
        _event(conn, row, "release", caller.agent, req.generation)
        return _result(conn, room_id, task_id, changed=True)


@router.post("/{task_id}/state")
def change_state(
    room_id: RoomId, task_id: str, req: StateChange, conn: Conn, caller: Caller
) -> TaskResult:
    with conn:
        _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        _require_holder(row, caller, req.generation)
        if row["state"] == req.state:
            return _result(conn, room_id, task_id, changed=False)
        conn.execute(
            "update tasks set state = ?, updated_at = ? where task_id = ?",
            (req.state, now_iso(), task_id),
        )
        _event(conn, row, req.state, caller.agent, req.generation)
        return _result(conn, room_id, task_id, changed=True)


def _validate_receipt(conn: sqlite3.Connection, room_id: str, caller, receipt: Receipt) -> None:
    msg = conn.execute(
        "select sender from messages where id = ? and room_id = ?",
        (receipt.result_message_id, room_id),
    ).fetchone()
    if msg is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "result_message_id is not a message in this room"
        )
    if msg["sender"] != caller.agent:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the result message must be your own"
        )
    if not (receipt.commits or receipt.pr_url or receipt.artifacts or receipt.tests):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a completion receipt needs evidence: commits, pr_url, artifacts or tests",
        )


@router.post("/{task_id}/complete")
def complete_task(
    room_id: RoomId, task_id: str, req: Complete, conn: Conn, caller: Caller
) -> TaskResult:
    """Finish a task you hold. Retrying the same completion is idempotent."""
    with conn:
        _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        receipt_json = req.receipt.model_dump_json()
        if (
            row["state"] in ("review", "done")
            and row["claim_owner"] == caller.agent
            and row["claim_generation"] == req.generation
            and row["receipt_json"] == receipt_json
        ):
            return _result(conn, room_id, task_id, changed=False)
        _require_holder(row, caller, req.generation)
        _validate_receipt(conn, room_id, caller, req.receipt)
        state = "review" if row["review_required"] else "done"
        now = now_iso()
        conn.execute(
            "update tasks set state = ?, receipt_json = ?, completed_at = ?,"
            " lease_expires_at = null, updated_at = ? where task_id = ?",
            (state, receipt_json, now if state == "done" else None, now, task_id),
        )
        _event(
            conn,
            row,
            "complete",
            caller.agent,
            req.generation,
            state=state,
            result_message_id=req.receipt.result_message_id,
        )
        return _result(conn, room_id, task_id, changed=True)


@router.post("/{task_id}/review")
def review_task(
    room_id: RoomId, task_id: str, req: Review, conn: Conn, caller: Caller
) -> TaskResult:
    """The task's creator or a room admin accepts (done) or rejects (back to its holder,
    in progress) a completed task awaiting review."""
    with conn:
        room = _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        if caller.agent != row["created_by"] and "admin" not in rights_of(conn, room, caller):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only the creator or an admin reviews")
        if row["state"] != "review":
            raise HTTPException(status.HTTP_409_CONFLICT, f"task is {row['state']}")
        if req.decision == "accept":
            conn.execute(
                "update tasks set state = 'done', completed_at = ?, updated_at = ?"
                " where task_id = ?",
                (now_iso(), now_iso(), task_id),
            )
        else:
            conn.execute(
                "update tasks set state = 'in_progress', receipt_json = null,"
                " lease_expires_at = ?, updated_at = ? where task_id = ?",
                (iso_in(900), now_iso(), task_id),
            )
        _event(
            conn,
            row,
            f"review.{req.decision}",
            caller.agent,
            row["claim_generation"],
            note=req.note,
        )
        return _result(conn, room_id, task_id, changed=True)


@router.post("/{task_id}/cancel")
def cancel_task(room_id: RoomId, task_id: str, conn: Conn, caller: Caller) -> TaskResult:
    with conn:
        room = _begin(conn, room_id, caller, "write")
        row = _get(conn, room_id, task_id)
        if caller.agent != row["created_by"] and "admin" not in rights_of(conn, room, caller):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only the creator or an admin cancels")
        if row["state"] == "cancelled":
            return _result(conn, room_id, task_id, changed=False)
        if row["state"] == "done":
            raise HTTPException(status.HTTP_409_CONFLICT, "task is done")
        conn.execute(
            "update tasks set state = 'cancelled', lease_expires_at = null, updated_at = ?"
            " where task_id = ?",
            (now_iso(), task_id),
        )
        _event(conn, row, "cancel", caller.agent, row["claim_generation"])
        return _result(conn, room_id, task_id, changed=True)
