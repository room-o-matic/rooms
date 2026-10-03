from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MessageType = Literal[
    "message",
    "proposal",
    "objection",
    "question",
    "answer",
    "finding",
    "status",
    "decision_request",
    "decision",
    "artifact",
    "task_update",
    "handoff",
]

# Optional typed-message fields persisted in messages.payload_json.
PAYLOAD_FIELDS = (
    "confidence",
    "reply_requested",
    "severity",
    "based_on_messages",
    "to",
    "in_reply_to",
    "hop",
)
# docs#16: informational types that should never wake an agent on their own.
NON_WAKING_TYPES = ("status", "handoff", "decision", "artifact", "task_update")


# docs#21: message references are same-room message IDs (SQLite integer range).
MessageRef = Annotated[int, Field(ge=1, le=2**63 - 1)]
MAX_MESSAGE_REFS = 64

Tags = list[str]
Right = Literal["read", "write", "invite", "admin"]
Admission = Literal["open", "closed"]


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=4000)
    created_by: str | None = None
    listed: bool = Field(default=False, description="publish name/purpose to the lobbyd directory")
    tags: Tags = Field(default_factory=list, max_length=32)
    admission: Admission | None = Field(default=None, description="default: server setting")
    default_rights: list[Right] | None = Field(
        default=None, description="rights for self-joiners of an open room"
    )
    max_hops: int | None = Field(default=None, ge=1, le=100, description="reply-chain depth cap")
    message_rate_per_minute: int | None = Field(
        default=None, ge=1, le=10_000, description="per-room cap for non-admins"
    )


class RoomUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=4000)
    listed: bool | None = None
    tags: Tags | None = Field(default=None, max_length=32)
    admission: Admission | None = None
    default_rights: list[Right] | None = None
    archived: bool | None = None
    paused: bool | None = Field(default=None, description="stop/drain: only admins may post")
    expected_revision: int | None = Field(
        default=None, description="optimistic concurrency: 409 if the room has moved on"
    )
    max_hops: int | None = Field(default=None, ge=1, le=100)
    message_rate_per_minute: int | None = Field(default=None, ge=1, le=10_000)


class Participant(BaseModel):
    agent: str
    role: str | None
    joined_at: str
    last_seen_at: str | None


class Room(BaseModel):
    id: str
    room_url: str
    name: str
    purpose: str | None
    created_by: str
    created_at: str
    archived_at: str | None
    listed: bool
    tags: Tags
    admission: Admission
    default_rights: list[Right]
    paused: bool
    max_hops: int
    message_rate_per_minute: int | None
    revision: int


class RoomDetail(Room):
    participants: list[Participant]


class RoomCreated(BaseModel):
    room_id: str
    room_url: str


class JoinRequest(BaseModel):
    agent: str | None = None
    role: str | None = Field(default=None, max_length=64)


class MessageCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from")
    type: MessageType = "message"
    topic: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    reply_requested: bool | None = None
    severity: str | None = Field(default=None, max_length=32)
    based_on_messages: list[MessageRef] | None = Field(
        default=None,
        max_length=MAX_MESSAGE_REFS,
        description="earlier messages in this room this one builds on; external context"
        " belongs in the body or an artifact",
    )
    to: list[str] | None = Field(
        default=None, max_length=32, description="structured recipients (identities)"
    )
    in_reply_to: MessageRef | None = Field(default=None, description="a message in this room")


class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    room_id: str
    from_: str = Field(alias="from")
    type: MessageType
    topic: str | None
    body: str
    confidence: float | None = None
    reply_requested: bool | None = None
    severity: str | None = None
    based_on_messages: list[int] | None = None
    to: list[str] | None = None
    in_reply_to: int | None = None
    hop: int | None = Field(default=None, description="reply-chain depth, computed by roomsd")
    created_at: str


class MessagesPage(BaseModel):
    room_id: str
    messages: list[Message]
    latest_message_id: int


class NotePut(BaseModel):
    value: JsonValue
    if_revision: int | None = Field(
        default=None,
        ge=0,
        description="compare-and-set: write only if the note is at this revision; "
        "0 means only if it doesn't exist yet. A mismatch is 412 and nothing changes.",
    )


class Note(BaseModel):
    key: str
    value: JsonValue
    revision: int
    updated_by: str
    updated_at: str


class NoteChange(BaseModel):
    id: int
    key: str
    revision: int
    updated_by: str
    updated_at: str


class NoteChanges(BaseModel):
    changes: list[NoteChange]
    next_cursor: int


class NotesResponse(BaseModel):
    room_id: str
    notes: dict[str, Note]


class WhoAmI(BaseModel):
    agent: str
    scope: str
    room_id: str | None
    role: str | None
    invite_id: str | None
    expires_at: str | None


class InviteCreate(BaseModel):
    name: str = Field(description="invitee name; identity becomes '<inviter>/<name>'")
    role: str | None = Field(default=None, max_length=64)
    ttl_seconds: int | None = Field(default=None, ge=60)


class Invite(BaseModel):
    invite_id: str
    room_id: str
    agent: str
    role: str | None
    created_by: str
    created_at: str
    expires_at: str
    revoked_at: str | None


class InviteCreated(Invite):
    token: str


class UpdatesPage(BaseModel):
    """New messages across every room the caller has joined on this server."""

    messages: list[Message]
    room_urls: dict[str, str] = Field(description="room_id -> room_url for rooms in messages")
    next_cursor: int


class WellKnown(BaseModel):
    server_id: str
    base_url: str
    issuer: str
    domain: str
    version: str
    features: list[str]


class MemberGrant(BaseModel):
    rights: list[Right] = Field(min_length=1)


class Member(BaseModel):
    agent: str
    rights: list[Right]
    granted_by: str
    granted_at: str
    banned_at: str | None
    joined: bool
