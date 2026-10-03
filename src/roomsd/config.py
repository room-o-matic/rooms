import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_message_bytes: int = 64 * 1024
    max_note_bytes: int = 256 * 1024
    default_invite_ttl_seconds: int = 3600
    max_invite_ttl_seconds: int = 24 * 3600
    default_registry_ttl_seconds: int = 60
    max_registry_ttl_seconds: int = 600

    @property
    def db_path(self) -> Path:
        return self.data_dir / "roomsd.sqlite"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("ROOMSD_DATA_DIR", "/var/lib/roomsd")),
            max_message_bytes=int(os.environ.get("ROOMSD_MAX_MESSAGE_BYTES", 64 * 1024)),
            max_note_bytes=int(os.environ.get("ROOMSD_MAX_NOTE_BYTES", 256 * 1024)),
        )
