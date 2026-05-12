from __future__ import annotations

import time

import pytest

from mylittleclaude.models import HeartbeatState
from mylittleclaude.queue import ActiveRegistry, ActiveRun


def _make(run_id: int, group_id: int = -1, topic_id: int = 10) -> ActiveRun:
    return ActiveRun(
        run_id=run_id,
        group_id=group_id,
        topic_id=topic_id,
        instance_name="x",
        started_at=time.monotonic(),
        working_message_id=100 + run_id,
        heartbeat=HeartbeatState(started_at=time.monotonic()),
    )


def test_add_and_get():
    reg = ActiveRegistry()
    run = _make(1)
    reg.add(run)
    assert reg.get(-1, 10) is run
    assert reg.get_by_run_id(1) is run
    assert reg.total() == 1


def test_remove():
    reg = ActiveRegistry()
    run = _make(1)
    reg.add(run)
    reg.remove(-1, 10, 1)
    assert reg.get(-1, 10) is None
    assert reg.get_by_run_id(1) is None
    assert reg.total() == 0


def test_different_topics_dont_collide():
    reg = ActiveRegistry()
    r1 = _make(1, topic_id=10)
    r2 = _make(2, topic_id=11)
    reg.add(r1)
    reg.add(r2)
    assert reg.total() == 2
    reg.remove(-1, 10, 1)
    assert reg.total() == 1
    assert reg.get(-1, 11) is r2


def test_capacity_ceiling_check():
    """The bot's capacity check is `len(active) >= MAX_CONCURRENT`. Sanity-check
    that `total()` reports what we expect for that comparison."""
    from mylittleclaude.bot import MAX_CONCURRENT
    reg = ActiveRegistry()
    for i in range(MAX_CONCURRENT):
        reg.add(_make(i, topic_id=100 + i))
    assert reg.total() == MAX_CONCURRENT
    # The bot rejects when total() >= MAX_CONCURRENT, so this is the boundary.
    assert reg.total() >= MAX_CONCURRENT
