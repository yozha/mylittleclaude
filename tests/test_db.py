from __future__ import annotations

from pathlib import Path

import pytest

from mylittleclaude import db


@pytest.mark.asyncio
async def test_migrations_apply_from_empty(tmp_path: Path):
    conn = await db.open_db(tmp_path)
    cur = await conn.execute("SELECT MAX(version) FROM schema_version")
    row = await cur.fetchone()
    assert row[0] == len(db.MIGRATIONS)
    await conn.close()


@pytest.mark.asyncio
async def test_migrations_are_idempotent(tmp_path: Path):
    conn1 = await db.open_db(tmp_path)
    await conn1.close()
    conn2 = await db.open_db(tmp_path)
    cur = await conn2.execute("SELECT COUNT(*) FROM schema_version")
    row = await cur.fetchone()
    assert row[0] == len(db.MIGRATIONS)
    await conn2.close()


@pytest.mark.asyncio
async def test_reconcile_orphan_runs(tmp_path: Path):
    conn = await db.open_db(tmp_path)
    await db.insert_topic(conn, group_id=-1, topic_id=10, instance="x", topic_name="x #1")
    rid = await db.insert_run(conn, group_id=-1, topic_id=10, prompt="p", session_before=None)
    n = await db.reconcile_orphan_runs(conn)
    assert n == 1
    # Running it again finds nothing.
    n2 = await db.reconcile_orphan_runs(conn)
    assert n2 == 0
    cur = await conn.execute("SELECT status, exit_code FROM runs WHERE id=?", (rid,))
    row = await cur.fetchone()
    assert row["status"] == "error"
    assert row["exit_code"] == -1
    await conn.close()


@pytest.mark.asyncio
async def test_pending_upsert_returns_overwrote(tmp_path: Path):
    conn = await db.open_db(tmp_path)
    await db.insert_topic(conn, group_id=-1, topic_id=10, instance="x", topic_name="t")
    first = await db.upsert_pending(
        conn, group_id=-1, topic_id=10, prompt="a", tg_message_id=1
    )
    assert first is False
    second = await db.upsert_pending(
        conn, group_id=-1, topic_id=10, prompt="b", tg_message_id=2
    )
    assert second is True
    pend = await db.get_pending(conn, -1, 10)
    assert pend is not None and pend.prompt == "b" and pend.tg_message_id == 2
    await db.delete_pending(conn, -1, 10)
    assert await db.get_pending(conn, -1, 10) is None
    await conn.close()


@pytest.mark.asyncio
async def test_topic_run_stats(tmp_path: Path):
    conn = await db.open_db(tmp_path)
    await db.insert_topic(conn, group_id=-1, topic_id=10, instance="x", topic_name="t")
    rid = await db.insert_run(conn, group_id=-1, topic_id=10, prompt="p", session_before=None)
    await db.finish_run(
        conn, rid, status="done", exit_code=0,
        cost_usd=0.5, duration_ms=2000, num_turns=3,
        session_after="sess",
    )
    rid2 = await db.insert_run(conn, group_id=-1, topic_id=10, prompt="p2", session_before="sess")
    await db.finish_run(
        conn, rid2, status="done", exit_code=0,
        cost_usd=1.0, duration_ms=1000, num_turns=2,
        session_after="sess",
    )
    stats = await db.topic_run_stats(conn, -1, 10)
    assert stats["completed"] == 2
    assert abs(stats["total_cost"] - 1.5) < 1e-9
    await conn.close()


@pytest.mark.asyncio
async def test_count_topics_with_prefix(tmp_path: Path):
    conn = await db.open_db(tmp_path)
    await db.insert_topic(conn, group_id=-1, topic_id=1, instance="a", topic_name="a #1")
    await db.insert_topic(conn, group_id=-1, topic_id=2, instance="a", topic_name="a #2")
    await db.insert_topic(conn, group_id=-1, topic_id=3, instance="b", topic_name="b #1")
    assert await db.count_topics_with_prefix(conn, -1, "a") == 2
    assert await db.count_topics_with_prefix(conn, -1, "b") == 1
    assert await db.count_topics_with_prefix(conn, -1, "c") == 0
    await conn.close()


@pytest.mark.asyncio
async def test_set_topic_session_and_close(tmp_path: Path):
    conn = await db.open_db(tmp_path)
    await db.insert_topic(conn, group_id=-1, topic_id=1, instance="a", topic_name="a #1")
    await db.set_topic_session(conn, -1, 1, "sess-xyz")
    t = await db.get_topic(conn, -1, 1)
    assert t.session_id == "sess-xyz"
    await db.set_topic_closed(conn, -1, 1)
    t = await db.get_topic(conn, -1, 1)
    assert t.closed_at is not None
    await conn.close()
