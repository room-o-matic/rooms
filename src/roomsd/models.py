from typing import Literal

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
PAYLOAD_FIELDS = ("confidence", "reply_requested", "severity", "based_on_messages")


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


class RoomUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=4000)
    listed: bool | None = None
    tags: Tags | None = Field(default=None, max_length=32)
    admission: Admission | None = None
    default_rights: list[Right] | None = None
    archived: bool | None = None


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
    based_on_messages: list[int] | None = None


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
    created_at: str


class MessagesPage(BaseModel):
    room_id: str
    messages: list[Message]
    latest_message_id: int


class NotePut(BaseModel):
    value: JsonValue


class Note(BaseModel):
    key: str
    value: JsonValue
    updated_by: str
    updated_at: str


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
