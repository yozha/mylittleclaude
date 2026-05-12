from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import PendingRow, RunRow, RunStatus, TopicRow

log = logging.getLogger(__name__)


MIGRATIONS: list[str] = [
    # v1: initial schema
    """
    CREATE TABLE IF NOT EXISTS topics (
      topic_id      INTEGER NOT NULL,
      group_id      INTEGER NOT NULL,
      instance      TEXT NOT NULL,
      session_id    TEXT,
      topic_name    TEXT,
      created_at    INTEGER NOT NULL,
      closed_at     INTEGER,
      edit_pending  INTEGER NOT NULL DEFAULT 0,
      edit_pending_expires_at INTEGER,
      PRIMARY KEY (group_id, topic_id)
    );
    CREATE INDEX IF NOT EXISTS idx_topics_instance ON topics(instance);

    CREATE TABLE IF NOT EXISTS runs (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      group_id        INTEGER NOT NULL,
      topic_id        INTEGER NOT NULL,
      prompt          TEXT NOT NULL,
      session_before  TEXT,
      session_after   TEXT,
      started_at      INTEGER NOT NULL,
      finished_at     INTEGER,
      exit_code       INTEGER,
      cost_usd        REAL,
      duration_ms     INTEGER,
      num_turns       INTEGER,
      output_path     TEXT,
      status          TEXT NOT NULL CHECK (status IN ('running','done','error','killed')),
      FOREIGN KEY (group_id, topic_id) REFERENCES topics(group_id, topic_id)
    );
    CREATE INDEX IF NOT EXISTS idx_runs_topic ON runs(group_id, topic_id);
    CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

    CREATE TABLE IF NOT EXISTS pending (
      group_id      INTEGER NOT NULL,
      topic_id      INTEGER NOT NULL,
      prompt        TEXT NOT NULL,
      tg_message_id INTEGER NOT NULL,
      queued_at     INTEGER NOT NULL,
      PRIMARY KEY (group_id, topic_id),
      FOREIGN KEY (group_id, topic_id) REFERENCES topics(group_id, topic_id)
    );
    """,
]


def db_path(data_dir: Path) -> Path:
    return data_dir / "mylittleclaude.db"


async def open_db(data_dir: Path) -> aiosqlite.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = db_path(data_dir)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.commit()
    await _migrate(conn)
    return conn


async def _migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    await conn.commit()
    cur = await conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    row = await cur.fetchone()
    current = row[0] if row else 0
    await cur.close()

    for i, sql in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        log.info("applying DB migration v%d", i)
        await conn.executescript(sql)
        await conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (i,)
        )
        await conn.commit()


async def reconcile_orphan_runs(conn: aiosqlite.Connection) -> int:
    """Mark any rows left in 'running' as 'error' on startup."""
    now = int(time.time())
    cur = await conn.execute(
        "UPDATE runs SET status='error', exit_code=-1, finished_at=? "
        "WHERE status='running'",
        (now,),
    )
    n = cur.rowcount or 0
    await conn.commit()
    await cur.close()
    if n:
        log.info("reconciled %d orphaned running run(s) to error", n)
    return n


# --- topic queries ---

def _topic_from_row(r: aiosqlite.Row) -> TopicRow:
    return TopicRow(
        topic_id=r["topic_id"],
        group_id=r["group_id"],
        instance=r["instance"],
        session_id=r["session_id"],
        topic_name=r["topic_name"],
        created_at=r["created_at"],
        closed_at=r["closed_at"],
        edit_pending=bool(r["edit_pending"]),
        edit_pending_expires_at=r["edit_pending_expires_at"],
    )


async def insert_topic(
    conn: aiosqlite.Connection,
    *,
    group_id: int,
    topic_id: int,
    instance: str,
    topic_name: str,
) -> None:
    now = int(time.time())
    await conn.execute(
        "INSERT INTO topics (topic_id, group_id, instance, session_id, "
        "topic_name, created_at) VALUES (?, ?, ?, NULL, ?, ?)",
        (topic_id, group_id, instance, topic_name, now),
    )
    await conn.commit()


async def get_topic(
    conn: aiosqlite.Connection, group_id: int, topic_id: int
) -> TopicRow | None:
    cur = await conn.execute(
        "SELECT * FROM topics WHERE group_id=? AND topic_id=?",
        (group_id, topic_id),
    )
    r = await cur.fetchone()
    await cur.close()
    return _topic_from_row(r) if r else None


async def set_topic_session(
    conn: aiosqlite.Connection, group_id: int, topic_id: int, session_id: str | None
) -> None:
    await conn.execute(
        "UPDATE topics SET session_id=? WHERE group_id=? AND topic_id=?",
        (session_id, group_id, topic_id),
    )
    await conn.commit()


async def set_topic_closed(
    conn: aiosqlite.Connection, group_id: int, topic_id: int
) -> None:
    await conn.execute(
        "UPDATE topics SET closed_at=? WHERE group_id=? AND topic_id=?",
        (int(time.time()), group_id, topic_id),
    )
    await conn.commit()


async def set_topic_edit_pending(
    conn: aiosqlite.Connection,
    group_id: int,
    topic_id: int,
    flag: bool,
    expires_at: int | None = None,
) -> None:
    await conn.execute(
        "UPDATE topics SET edit_pending=?, edit_pending_expires_at=? "
        "WHERE group_id=? AND topic_id=?",
        (1 if flag else 0, expires_at, group_id, topic_id),
    )
    await conn.commit()


async def count_topics_with_prefix(
    conn: aiosqlite.Connection, group_id: int, instance: str
) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM topics WHERE group_id=? AND instance=?",
        (group_id, instance),
    )
    r = await cur.fetchone()
    await cur.close()
    return r[0] if r else 0


# --- run queries ---

def _run_from_row(r: aiosqlite.Row) -> RunRow:
    return RunRow(
        id=r["id"],
        group_id=r["group_id"],
        topic_id=r["topic_id"],
        prompt=r["prompt"],
        session_before=r["session_before"],
        session_after=r["session_after"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        exit_code=r["exit_code"],
        cost_usd=r["cost_usd"],
        duration_ms=r["duration_ms"],
        num_turns=r["num_turns"],
        output_path=r["output_path"],
        status=r["status"],
    )


async def insert_run(
    conn: aiosqlite.Connection,
    *,
    group_id: int,
    topic_id: int,
    prompt: str,
    session_before: str | None,
) -> int:
    now = int(time.time())
    cur = await conn.execute(
        "INSERT INTO runs (group_id, topic_id, prompt, session_before, "
        "started_at, status) VALUES (?, ?, ?, ?, ?, 'running')",
        (group_id, topic_id, prompt, session_before, now),
    )
    run_id = cur.lastrowid
    await cur.close()
    await conn.commit()
    assert run_id is not None
    return run_id


async def finish_run(
    conn: aiosqlite.Connection,
    run_id: int,
    *,
    status: RunStatus,
    exit_code: int | None,
    session_after: str | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    num_turns: int | None = None,
    output_path: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE runs SET status=?, exit_code=?, session_after=?, cost_usd=?, "
        "duration_ms=?, num_turns=?, output_path=?, finished_at=? WHERE id=?",
        (
            status, exit_code, session_after, cost_usd,
            duration_ms, num_turns, output_path, int(time.time()), run_id,
        ),
    )
    await conn.commit()


async def topic_run_stats(
    conn: aiosqlite.Connection, group_id: int, topic_id: int
) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost, "
        "MAX(finished_at) AS last FROM runs "
        "WHERE group_id=? AND topic_id=? AND status='done'",
        (group_id, topic_id),
    )
    r = await cur.fetchone()
    await cur.close()
    return {
        "completed": r["n"] if r else 0,
        "total_cost": r["cost"] if r else 0.0,
        "last_finished_at": r["last"] if r else None,
    }


# --- pending queries ---

def _pending_from_row(r: aiosqlite.Row) -> PendingRow:
    return PendingRow(
        group_id=r["group_id"],
        topic_id=r["topic_id"],
        prompt=r["prompt"],
        tg_message_id=r["tg_message_id"],
        queued_at=r["queued_at"],
    )


async def get_pending(
    conn: aiosqlite.Connection, group_id: int, topic_id: int
) -> PendingRow | None:
    cur = await conn.execute(
        "SELECT * FROM pending WHERE group_id=? AND topic_id=?",
        (group_id, topic_id),
    )
    r = await cur.fetchone()
    await cur.close()
    return _pending_from_row(r) if r else None


async def upsert_pending(
    conn: aiosqlite.Connection,
    *,
    group_id: int,
    topic_id: int,
    prompt: str,
    tg_message_id: int,
) -> bool:
    """Returns True if a previous pending was overwritten."""
    existing = await get_pending(conn, group_id, topic_id)
    await conn.execute(
        "INSERT INTO pending (group_id, topic_id, prompt, tg_message_id, queued_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(group_id, topic_id) DO UPDATE SET "
        "prompt=excluded.prompt, tg_message_id=excluded.tg_message_id, "
        "queued_at=excluded.queued_at",
        (group_id, topic_id, prompt, tg_message_id, int(time.time())),
    )
    await conn.commit()
    return existing is not None


async def delete_pending(
    conn: aiosqlite.Connection, group_id: int, topic_id: int
) -> None:
    await conn.execute(
        "DELETE FROM pending WHERE group_id=? AND topic_id=?",
        (group_id, topic_id),
    )
    await conn.commit()


async def count_running(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'")
    r = await cur.fetchone()
    await cur.close()
    return r[0] if r else 0
