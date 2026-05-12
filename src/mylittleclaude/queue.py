from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .models import HeartbeatState


@dataclass
class ActiveRun:
    """Represents an in-flight Claude Code subprocess in a topic."""
    run_id: int
    group_id: int
    topic_id: int
    instance_name: str
    started_at: float  # monotonic
    working_message_id: int  # heartbeat message we edit
    heartbeat: HeartbeatState
    task: asyncio.Task | None = None
    kill_requested: bool = False
    # Mutable proc handle; assigned once spawned.
    proc: object | None = None


@dataclass
class ActiveRegistry:
    """In-memory map of (group_id, topic_id) -> ActiveRun, plus a global count."""
    runs: dict[tuple[int, int], ActiveRun] = field(default_factory=dict)
    by_run_id: dict[int, ActiveRun] = field(default_factory=dict)

    def get(self, group_id: int, topic_id: int) -> ActiveRun | None:
        return self.runs.get((group_id, topic_id))

    def get_by_run_id(self, run_id: int) -> ActiveRun | None:
        return self.by_run_id.get(run_id)

    def add(self, run: ActiveRun) -> None:
        self.runs[(run.group_id, run.topic_id)] = run
        self.by_run_id[run.run_id] = run

    def remove(self, group_id: int, topic_id: int, run_id: int | None = None) -> None:
        self.runs.pop((group_id, topic_id), None)
        if run_id is not None:
            self.by_run_id.pop(run_id, None)

    def total(self) -> int:
        return len(self.runs)
