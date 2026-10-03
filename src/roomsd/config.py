import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # This server's identity in lobbyd, and the URL clients use to reach it. base_url is
    # also the audience lobbyd access tokens must carry, and the prefix of every room URL.
    server_id: str = "rooms-local"
    base_url: str = "http://127.0.0.1:8766"
    tags: tuple[str, ...] = ()
    # docs#10: admission for rooms created without an explicit one. "open" suits a trusted
    # single operator; public deployments should use "closed".
    default_admission: str = "open"

    # lobbyd: the issuer whose access tokens this server accepts.
    lobbyd_url: str = "http://127.0.0.1:8767"
    lobbyd_domain: str = "local"
    lobbyd_jwks_url: str | None = None
    # A roomsd-scope lobbyd API key named `server_id`. When set, this server heartbeats
    # into the lobbyd directory and publishes listed rooms there.
    lobbyd_api_key: str | None = field(default=None, repr=False)
    lobby_heartbeat_ttl_seconds: int = 60

    # docs#21: the body limit, the limit on a message's whole stored representation
    # (body + topic + typed payload), the per-request cap enforced before JSON decoding,
    # and the byte budget of one page of messages.
    max_message_bytes: int = 64 * 1024
    max_message_total_bytes: int = 72 * 1024
    max_request_bytes: int = 1024 * 1024
    max_page_bytes: int = 4 * 1024 * 1024
    max_note_bytes: int = 256 * 1024
    default_invite_ttl_seconds: int = 3600
    # docs#23: a participant seen within this window is "available" (see PARTICIPANT_SQL).
    presence_ttl_seconds: int = 120
    max_invite_ttl_seconds: int = 24 * 3600

    @property
    def db_path(self) -> Path:
        return self.data_dir / "roomsd.sqlite"

    @property
    def lobby_sync_enabled(self) -> bool:
        return bool(self.lobbyd_api_key)

    def room_url(self, room_id: str) -> str:
        return f"{self.base_url}/v1/rooms/{room_id}"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            data_dir=Path(env("ROOMSD_DATA_DIR", "/var/lib/roomsd")),
            server_id=env("ROOMSD_SERVER_ID", cls.server_id),
            base_url=env("ROOMSD_BASE_URL", cls.base_url).rstrip("/"),
            tags=tuple(t for t in env("ROOMSD_TAGS", "").split(",") if t),
            default_admission=env("ROOMSD_DEFAULT_ADMISSION", cls.default_admission),
            lobbyd_url=env("LOBBYD_URL", cls.lobbyd_url).rstrip("/"),
            lobbyd_domain=env("LOBBYD_DOMAIN", cls.lobbyd_domain),
            lobbyd_jwks_url=env("LOBBYD_JWKS_URL"),
            lobbyd_api_key=env("ROOMSD_LOBBYD_API_KEY"),
            max_message_bytes=int(env("ROOMSD_MAX_MESSAGE_BYTES", 64 * 1024)),
            max_message_total_bytes=int(env("ROOMSD_MAX_MESSAGE_TOTAL_BYTES", 72 * 1024)),
            max_request_bytes=int(env("ROOMSD_MAX_REQUEST_BYTES", 1024 * 1024)),
            max_page_bytes=int(env("ROOMSD_MAX_PAGE_BYTES", 4 * 1024 * 1024)),
            max_note_bytes=int(env("ROOMSD_MAX_NOTE_BYTES", 256 * 1024)),
            presence_ttl_seconds=int(env("ROOMSD_PRESENCE_TTL_SECONDS", 120)),
        )
