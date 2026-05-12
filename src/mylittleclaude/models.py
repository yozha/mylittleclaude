from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

INSTANCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
HOST_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")


class InstanceConfig(BaseModel):
    description: str = ""
    host: str
    workdir: str
    ssh_key: str | None = None

    @field_validator("host")
    @classmethod
    def _check_host(cls, v: str) -> str:
        if v == "local":
            return v
        if not HOST_RE.match(v):
            raise ValueError(
                f"host must be 'local' or match [a-zA-Z0-9._@-]+, got {v!r}"
            )
        if "@" not in v:
            raise ValueError(f"remote host must be in the form user@host, got {v!r}")
        return v

    @field_validator("workdir")
    @classmethod
    def _check_workdir(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"workdir must be absolute, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_ssh_key(self) -> InstanceConfig:
        if self.host == "local":
            return self
        if not self.ssh_key:
            raise ValueError(f"remote host {self.host!r} requires ssh_key")
        if not self.ssh_key.startswith("/"):
            raise ValueError(f"ssh_key must be absolute, got {self.ssh_key!r}")
        return self

    @property
    def is_local(self) -> bool:
        return self.host == "local"


class ServersFile(BaseModel):
    instances: dict[str, InstanceConfig] = Field(default_factory=dict)

    @field_validator("instances")
    @classmethod
    def _check_names(cls, v: dict[str, InstanceConfig]) -> dict[str, InstanceConfig]:
        for name in v:
            if not INSTANCE_NAME_RE.match(name):
                raise ValueError(
                    f"instance name {name!r} must match ^[a-z0-9][a-z0-9_-]{{0,30}}$"
                )
        return v


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_group_ids: frozenset[int]
    claude_bin: str
    data_dir: Path
    log_level: str
    servers: ServersFile
    bootstrap_mode: bool = False

    def instance(self, name: str) -> InstanceConfig | None:
        return self.servers.instances.get(name)


RunStatus = Literal["running", "done", "error", "killed"]


@dataclass
class TopicRow:
    topic_id: int
    group_id: int
    instance: str
    session_id: str | None
    topic_name: str | None
    created_at: int
    closed_at: int | None = None
    edit_pending: bool = False
    edit_pending_expires_at: int | None = None


@dataclass
class RunRow:
    id: int
    group_id: int
    topic_id: int
    prompt: str
    session_before: str | None
    session_after: str | None
    started_at: int
    finished_at: int | None
    exit_code: int | None
    cost_usd: float | None
    duration_ms: int | None
    num_turns: int | None
    output_path: str | None
    status: RunStatus


@dataclass
class PendingRow:
    group_id: int
    topic_id: int
    prompt: str
    tg_message_id: int
    queued_at: int


@dataclass
class HeartbeatState:
    """Tracks last-activity info for an in-flight run."""
    started_at: float
    last_activity_ts: float = field(default=0.0)
    last_activity_summary: str = "starting"
