from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import db, files
from .auth import check_update, is_authorized
from .log import short_excerpt
from .models import AppConfig, HeartbeatState, InstanceConfig
from .queue import ActiveRegistry, ActiveRun
from .runner import RunnerProcess, spawn
from .stream import StreamResult, consume_stream

log = logging.getLogger(__name__)

MAX_CONCURRENT = 10
MAX_INLINE_RESULT = 3500
HEARTBEAT_INTERVAL_SEC = 15 * 60
HEARTBEAT_TICK_SEC = 30  # how often the watcher wakes; only edits every 15 min
NO_ACTIVITY_WARN_SEC = 3 * 60
EDIT_PENDING_TIMEOUT_SEC = 10 * 60
KILL_GRACE_SEC = 5

CB_KILL = "kill"
CB_RUN = "run"
CB_CANCEL = "cancel"
CB_EDIT = "edit"
CB_NEW = "new"


# ---------------------------------------------------------------------------
# App state holder
# ---------------------------------------------------------------------------

@dataclass
class BotState:
    cfg: AppConfig
    conn: aiosqlite.Connection
    active: ActiveRegistry
    spawn_lock: asyncio.Lock  # protects the capacity check + add

    @property
    def runs_dir(self) -> Path:
        return self.cfg.data_dir / "runs"


def _state(context: ContextTypes.DEFAULT_TYPE) -> BotState:
    return context.application.bot_data["state"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_general_topic(update: Update) -> bool:
    msg = update.effective_message
    if not msg:
        return False
    return not bool(getattr(msg, "is_topic_message", False))


def _thread_id(update: Update) -> int | None:
    msg = update.effective_message
    if not msg:
        return None
    return getattr(msg, "message_thread_id", None)


def _fmt_relative(ts: int | None, now: int | None = None) -> str:
    if ts is None:
        return "never"
    now = now or int(time.time())
    delta = max(0, now - ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_duration_short(ms: int) -> str:
    s = ms / 1000
    if s < 60:
        return f"{int(round(s))}s"
    m, s = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _kill_keyboard(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Kill", callback_data=f"{CB_KILL}:{run_id}")]]
    )


def _confirm_keyboard(topic_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Run", callback_data=f"{CB_RUN}:{topic_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"{CB_CANCEL}:{topic_id}"),
        InlineKeyboardButton("Edit", callback_data=f"{CB_EDIT}:{topic_id}"),
    ]])


def _instances_keyboard(instance_names: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"New topic → {n}", callback_data=f"{CB_NEW}:{n}")]
         for n in instance_names]
    )


# ---------------------------------------------------------------------------
# Control plane commands (General topic only)
# ---------------------------------------------------------------------------

USAGE = (
    "*mylittleclaude*\n"
    "_Single-operator Telegram → Claude Code bridge._\n\n"
    "*In the General topic:*\n"
    "• `/instances` — list configured instances\n"
    "• `/new <instance>` — start a new session topic\n"
    "• `/chatid` — show this chat's ID\n\n"
    "*Inside a session topic:*\n"
    "• Send text → it becomes a Claude Code prompt\n"
    "• Send a file → saved to the instance's `_inbox/`\n"
    "• `/info` — show session state and totals\n"
    "• `/get <relative_path>` — download a file from the workdir\n"
    "• `/kill` — stop the in-flight run\n"
    "• `/reset` — start a fresh Claude session next prompt\n"
    "• `/close` — close this topic"
)


def _format_result_message(
    *,
    cost: float,
    duration_ms: int,
    num_turns: int,
    is_error: bool,
    inline: str,
    truncated: bool,
) -> str:
    """Format the final result edit using MarkdownV2.

    Header chars (`.`, `-`, `$`, `(`, `)`) are escape_markdown(v2)'d.
    The result body sits inside a MarkdownV2 pre block; only ` and \\ need
    escaping there per the Telegram docs.
    """
    icon = "⚠️" if is_error else "✅"
    raw_header = (
        f"{icon} ${cost:.4f} · {_fmt_duration_short(duration_ms)} · "
        f"{num_turns} turns"
    )
    if is_error:
        raw_header += " (claude reported is_error)"
    header = escape_markdown(raw_header, version=2)
    pre_body = escape_markdown(inline, version=2, entity_type="pre")
    if truncated:
        marker = escape_markdown("…(truncated, see attached)…", version=2,
                                 entity_type="pre")
        return f"{header}\n```\n{marker}\n{pre_body}\n```"
    return f"{header}\n```\n{pre_body}\n```"


def _format_result_message_plain(
    *,
    cost: float,
    duration_ms: int,
    num_turns: int,
    is_error: bool,
    inline: str,
    truncated: bool,
) -> str:
    """Plain-text fallback for the result edit if MarkdownV2 rendering fails."""
    icon = "⚠️" if is_error else "✅"
    header = (
        f"{icon} ${cost:.4f} · {_fmt_duration_short(duration_ms)} · "
        f"{num_turns} turns"
    )
    if is_error:
        header += " (claude reported is_error)"
    if truncated:
        return f"{header}\n…(truncated, see attached)…\n{inline}"
    return f"{header}\n{inline}"


def build_chatid_message(chat_id: int, user_id: int, bootstrap: bool) -> str:
    """Build the /chatid reply as plain text.

    Returns a string with no Markdown formatting so it can be sent without a
    parse_mode. The previous Markdown version unbalanced underscores
    (chat_id, user_id, _Bootstrap mode_, this chat_id) and Telegram rejected
    it with "can't find end of the entity".
    """
    lines = [
        f"chat_id: {chat_id}",
        f"your user_id: {user_id}",
    ]
    if bootstrap:
        lines += [
            "",
            "Bootstrap mode: add this chat_id to ALLOWED_GROUP_IDS and restart.",
        ]
    return "\n".join(lines)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid works for any allowed user, even before group whitelist is set."""
    state = _state(context)
    auth = check_update(state.cfg, update)
    if not auth.user_ok:
        return  # drop silently
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    text = build_chatid_message(chat.id, user.id, state.cfg.bootstrap_mode)
    try:
        await msg.reply_text(text)  # plain text — no parse_mode
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("chatid reply failed: %s", e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if not _is_general_topic(update):
        return
    try:
        await update.effective_message.reply_text(USAGE, parse_mode=ParseMode.MARKDOWN)
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("start reply failed: %s", e)


async def cmd_instances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if not _is_general_topic(update):
        return
    insts = state.cfg.servers.instances
    if not insts:
        await update.effective_message.reply_text(
            "No instances configured. Edit `servers.yaml` and restart.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = ["*Instances:*"]
    for name, inst in insts.items():
        # `name` is regex-validated to [a-z0-9_-]+, safe inside backticks
        # (underscores inside `code` spans aren't interpreted in legacy MD).
        desc = escape_markdown(inst.description or "", version=1)
        host = escape_markdown("local" if inst.is_local else inst.host, version=1)
        lines.append(f"• `{name}` ({host}) — {desc}")
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_instances_keyboard(list(insts.keys())),
    )


async def _create_topic_for(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    instance_name: str,
    reply_chat_id: int | None,
) -> None:
    inst = state.cfg.instance(instance_name)
    if inst is None:
        if reply_chat_id is not None:
            await context.bot.send_message(
                chat_id=reply_chat_id,
                text=f"Unknown instance: {instance_name}",
            )
        return
    n = await db.count_topics_with_prefix(state.conn, group_id, instance_name)
    topic_name = f"{instance_name} #{n + 1}"
    try:
        topic = await context.bot.create_forum_topic(chat_id=group_id, name=topic_name)
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("create_forum_topic failed: %s", e)
        if reply_chat_id is not None:
            await context.bot.send_message(
                chat_id=reply_chat_id,
                text=f"Failed to create topic: {e}",
            )
        return
    thread_id = topic.message_thread_id
    await db.insert_topic(
        state.conn,
        group_id=group_id,
        topic_id=thread_id,
        instance=instance_name,
        topic_name=topic_name,
    )
    welcome = (
        f"Session topic for {instance_name} "
        f"({inst.host}:{inst.workdir}).\n"
        f"Send a text message to prompt Claude Code. "
        f"Files go to _inbox/. Use /info, /kill, /reset, /close."
    )
    try:
        await context.bot.send_message(
            chat_id=group_id,
            message_thread_id=thread_id,
            text=welcome,
        )
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("welcome send failed: %s", e)
    log.info("topic created group=%s id=%s instance=%s", group_id, thread_id, instance_name)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if not _is_general_topic(update):
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/new <instance>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    instance_name = args[0].strip()
    await _create_topic_for(
        state, context, update.effective_chat.id, instance_name,
        reply_chat_id=update.effective_chat.id,
    )


# ---------------------------------------------------------------------------
# In-topic commands
# ---------------------------------------------------------------------------

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if _is_general_topic(update):
        return
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return
    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if not topic:
        return
    stats = await db.topic_run_stats(state.conn, chat_id, thread_id)
    session = topic.session_id or "no session yet"
    short_session = session if len(session) <= 12 else session[:8] + "…"
    last = _fmt_relative(stats["last_finished_at"])
    text = (
        f"{topic.instance}\n"
        f"session: {short_session}\n"
        f"completed runs: {stats['completed']}\n"
        f"total cost: ${stats['total_cost']:.4f}\n"
        f"last finished: {last}"
    )
    await update.effective_message.reply_text(text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if _is_general_topic(update):
        return
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return
    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if not topic:
        return
    await db.set_topic_session(state.conn, chat_id, thread_id, None)
    await update.effective_message.reply_text(
        "Session cleared. Next prompt starts a fresh Claude Code session."
    )


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if _is_general_topic(update):
        return
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return
    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if not topic:
        return
    await db.set_topic_closed(state.conn, chat_id, thread_id)
    try:
        await context.bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("close_forum_topic failed: %s", e)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if _is_general_topic(update):
        return
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return
    run = state.active.get(chat_id, thread_id)
    if run is None:
        await update.effective_message.reply_text("Nothing is running in this topic.")
        return
    await _request_kill(state, run)


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if _is_general_topic(update):
        return
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return
    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if not topic:
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: `/get <relative_path>`",
                                                  parse_mode=ParseMode.MARKDOWN)
        return
    rel = " ".join(args).strip()
    inst = state.cfg.instance(topic.instance)
    if inst is None:
        await update.effective_message.reply_text(
            f"Instance {topic.instance} no longer in servers.yaml.",
        )
        return

    msg = update.effective_message
    try:
        if inst.is_local:
            path = files.resolve_get_path(inst, rel)
            size = path.stat().st_size
            if size > files.MAX_TG_BYTES:
                await msg.reply_text(
                    f"File is {size // (1024*1024)} MB; Telegram bot upload cap is 50 MB.",
                )
                return
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    document=f,
                    filename=path.name,
                )
        else:
            # Pull remote file into a temp, then upload.
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                await files.scp_from_remote(inst, rel, tmp_path)
                size = tmp_path.stat().st_size
                if size > files.MAX_TG_BYTES:
                    await msg.reply_text(
                        f"File is {size // (1024*1024)} MB; Telegram bot upload cap is 50 MB.",
                    )
                    return
                with open(tmp_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        document=f,
                        filename=os.path.basename(rel),
                    )
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    except files.FileError as e:
        await msg.reply_text(f"Error: {e}")


# ---------------------------------------------------------------------------
# Files in / out
# ---------------------------------------------------------------------------

async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    if _is_general_topic(update):
        return
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return
    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if not topic or topic.closed_at is not None:
        return
    inst = state.cfg.instance(topic.instance)
    if inst is None:
        return

    msg = update.effective_message
    tg_file = None
    original_name = "file"
    if msg.document:
        tg_file = msg.document
        original_name = msg.document.file_name or "document"
    elif msg.photo:
        # Highest-resolution photo size.
        tg_file = msg.photo[-1]
        original_name = f"photo_{tg_file.file_unique_id}.jpg"
    else:
        return

    if tg_file.file_size and tg_file.file_size > files.MAX_TG_BYTES:
        await msg.reply_text("File too large (>50 MB).")
        return

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        tg_obj = await context.bot.get_file(tg_file.file_id)
        await tg_obj.download_to_drive(custom_path=str(tmp_path))
        if inst.is_local:
            final = await files.save_local_inbox(inst, tmp_path, original_name)
        else:
            try:
                final = await files.scp_to_remote(inst, tmp_path, original_name)
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        await msg.reply_text(
            f"📥 Saved to _inbox/{final}",
            reply_to_message_id=msg.message_id,
        )
    except files.FileError as e:
        await msg.reply_text(f"Save failed: {e}")
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Prompt handling — the core
# ---------------------------------------------------------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if not is_authorized(state.cfg, update):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    if _is_general_topic(update):
        return  # text in General is ignored
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update)
    if thread_id is None:
        return

    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if topic is None:
        await msg.reply_text(
            "This topic isn't tracked. Use `/new <instance>` in General to start.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if topic.closed_at is not None:
        return

    prompt = msg.text

    # /cancel during edit-pending mode drops the queued prompt.
    now = int(time.time())
    if topic.edit_pending and (
        topic.edit_pending_expires_at is None
        or now <= topic.edit_pending_expires_at
    ):
        if prompt.strip() == "/cancel":
            await db.delete_pending(state.conn, chat_id, thread_id)
            await db.set_topic_edit_pending(state.conn, chat_id, thread_id, False, None)
            await msg.reply_text("Queued prompt dropped.")
            return
        # Replace pending instead of treating as a new run.
        await db.upsert_pending(
            state.conn,
            group_id=chat_id, topic_id=thread_id,
            prompt=prompt, tg_message_id=msg.message_id,
        )
        await db.set_topic_edit_pending(state.conn, chat_id, thread_id, False, None)
        await msg.reply_text("Queued prompt updated.")
        return
    elif topic.edit_pending:
        # Expired — clear the flag and fall through to normal handling.
        await db.set_topic_edit_pending(state.conn, chat_id, thread_id, False, None)

    # In-flight? queue and confirm.
    active = state.active.get(chat_id, thread_id)
    if active is not None:
        overwrote = await db.upsert_pending(
            state.conn,
            group_id=chat_id, topic_id=thread_id,
            prompt=prompt, tg_message_id=msg.message_id,
        )
        try:
            await context.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=msg.message_id,
                reaction="⏸",
            )
        except (BadRequest, NetworkError, TimedOut) as e:
            log.info("reaction unavailable, falling back: %s", e)
            await msg.reply_text("⏸ Held — will ask when the current run finishes.")
        if overwrote:
            await msg.reply_text("Replaced earlier queued prompt with this one.")
        return

    # Capacity ceiling.
    async with state.spawn_lock:
        if state.active.total() >= MAX_CONCURRENT:
            await msg.reply_text(
                f"Bot is at capacity ({MAX_CONCURRENT} concurrent runs). Try again shortly."
            )
            return

    await _start_run(state, context, topic.instance, chat_id, thread_id, prompt)


async def _start_run(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    instance_name: str,
    chat_id: int,
    thread_id: int,
    prompt: str,
) -> None:
    inst = state.cfg.instance(instance_name)
    if inst is None:
        await context.bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=f"Instance {instance_name} not found in servers.yaml.",
        )
        return
    topic = await db.get_topic(state.conn, chat_id, thread_id)
    if topic is None:
        return
    session_before = topic.session_id

    run_id = await db.insert_run(
        state.conn,
        group_id=chat_id, topic_id=thread_id,
        prompt=prompt, session_before=session_before,
    )
    log.info(
        "run.start id=%d topic=%d instance=%s session=%s",
        run_id, thread_id, instance_name,
        (session_before[:8] if session_before else "new"),
    )

    try:
        working_msg = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text="⏳ Working…",
            reply_markup=_kill_keyboard(run_id),
        )
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("send working msg failed: %s", e)
        await db.finish_run(state.conn, run_id, status="error", exit_code=-1)
        return

    hb = HeartbeatState(started_at=time.monotonic())
    active = ActiveRun(
        run_id=run_id,
        group_id=chat_id,
        topic_id=thread_id,
        instance_name=instance_name,
        started_at=hb.started_at,
        working_message_id=working_msg.message_id,
        heartbeat=hb,
    )
    async with state.spawn_lock:
        state.active.add(active)

    task = asyncio.create_task(
        _run_lifecycle(state, context, active, inst, session_before, prompt)
    )
    active.task = task


async def _run_lifecycle(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    active: ActiveRun,
    inst: InstanceConfig,
    session_before: str | None,
    prompt: str,
) -> None:
    chat_id = active.group_id
    thread_id = active.topic_id
    run_id = active.run_id
    runs_dir = state.runs_dir / str(thread_id)
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts_label = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    # Spawn
    try:
        proc = await spawn(inst, claude_bin=state.cfg.claude_bin, session_id=session_before)
    except FileNotFoundError as e:
        log.warning("spawn failed: %s", e)
        await _finish_error(
            state, context, active, exit_code=-1,
            stderr=f"failed to launch claude: {e}".encode(),
            assistant_text="",
        )
        return
    except Exception as e:
        log.exception("spawn raised")
        await _finish_error(
            state, context, active, exit_code=-1,
            stderr=str(e).encode(),
            assistant_text="",
        )
        return

    active.proc = proc
    active.heartbeat.last_activity_ts = time.monotonic()

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(state, context, active)
    )
    stderr_task = asyncio.create_task(proc.stderr_bytes())

    # Pipe prompt into stdin and close.
    try:
        await proc.stdin_write_and_close(prompt)
    except Exception as e:
        log.warning("stdin write failed: %s", e)

    # Consume stdout.
    stream_result: StreamResult
    try:
        stream_result = await consume_stream(proc.stdout_lines(), active.heartbeat)
    except Exception:
        log.exception("stream consume crashed")
        stream_result = StreamResult()

    exit_code = await proc.wait()
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except (asyncio.CancelledError, Exception):
        pass

    stderr_bytes = await stderr_task

    if active.kill_requested:
        await _finish_killed(state, context, active, stream_result, runs_dir, ts_label)
        return

    if exit_code != 0 or stream_result.result_event is None:
        await _finish_error(
            state, context, active,
            exit_code=exit_code,
            stderr=stderr_bytes,
            assistant_text=stream_result.aggregated_text,
        )
        return

    await _finish_done(state, context, active, stream_result, runs_dir, ts_label)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

async def _heartbeat_loop(
    state: BotState, context: ContextTypes.DEFAULT_TYPE, active: ActiveRun
) -> None:
    last_edit = time.monotonic()
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_TICK_SEC)
            now = time.monotonic()
            if now - last_edit < HEARTBEAT_INTERVAL_SEC:
                continue
            elapsed = int(now - active.started_at)
            since_act = now - active.heartbeat.last_activity_ts
            if since_act > NO_ACTIVITY_WARN_SEC:
                tail = f"⚠️ No activity for {int(since_act)//60}m — likely stuck"
            else:
                tail = (
                    f"Last activity {int(since_act)}s ago "
                    f"({active.heartbeat.last_activity_summary})"
                )
            text = f"⏳ Working — {elapsed // 60} min elapsed. {tail}"
            try:
                await context.bot.edit_message_text(
                    chat_id=active.group_id,
                    message_id=active.working_message_id,
                    text=text,
                    reply_markup=_kill_keyboard(active.run_id),
                )
            except RetryAfter as e:
                log.info("heartbeat rate-limited; sleeping %.1fs", e.retry_after)
                await asyncio.sleep(float(e.retry_after))
            except (BadRequest, NetworkError, TimedOut) as e:
                log.warning("heartbeat edit failed: %s", e)
            last_edit = now
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Run completion paths
# ---------------------------------------------------------------------------

async def _finish_done(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    active: ActiveRun,
    stream_result: StreamResult,
    runs_dir: Path,
    ts_label: str,
) -> None:
    ev = stream_result.result_event or {}
    result_text = str(ev.get("result", "") or "")
    session_id = ev.get("session_id")
    cost = float(ev.get("total_cost_usd") or 0.0)
    duration_ms = int(ev.get("duration_ms") or 0)
    num_turns = int(ev.get("num_turns") or 0)
    is_error = bool(ev.get("is_error"))

    # Persist full result to disk.
    out_path = runs_dir / f"{ts_label}.md"
    try:
        out_path.write_text(result_text, encoding="utf-8")
        try:
            os.chmod(out_path, 0o640)
        except OSError:
            pass
    except OSError as e:
        log.warning("write result file failed: %s", e)

    # DB updates.
    final_status = "error" if is_error else "done"
    await db.finish_run(
        state.conn, active.run_id,
        status=final_status,
        exit_code=0,
        session_after=session_id,
        cost_usd=cost,
        duration_ms=duration_ms,
        num_turns=num_turns,
        output_path=str(out_path),
    )
    if not is_error and session_id:
        await db.set_topic_session(state.conn, active.group_id, active.topic_id, session_id)

    state.active.remove(active.group_id, active.topic_id, active.run_id)

    # Edit working message with summary + result preview.
    # MarkdownV2 + pre-block escaping: Claude routinely emits ``` in its
    # output, which would close a legacy-Markdown fence. Inside a MarkdownV2
    # pre block we only need to escape ` and \.
    if len(result_text) <= MAX_INLINE_RESULT:
        inline = result_text
        truncated = False
    else:
        inline = result_text[-MAX_INLINE_RESULT:]
        truncated = True

    body = _format_result_message(
        cost=cost,
        duration_ms=duration_ms,
        num_turns=num_turns,
        is_error=is_error,
        inline=inline,
        truncated=truncated,
    )

    try:
        await context.bot.edit_message_text(
            chat_id=active.group_id,
            message_id=active.working_message_id,
            text=body,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("final edit failed: %s", e)
        # Fallback: send as plain text so the operator still sees the result.
        try:
            plain = _format_result_message_plain(
                cost=cost, duration_ms=duration_ms, num_turns=num_turns,
                is_error=is_error, inline=inline, truncated=truncated,
            )
            await context.bot.edit_message_text(
                chat_id=active.group_id,
                message_id=active.working_message_id,
                text=plain,
            )
        except (BadRequest, NetworkError, TimedOut) as e2:
            log.warning("final edit plain fallback failed: %s", e2)

    if len(result_text) > MAX_INLINE_RESULT and out_path.exists():
        try:
            with open(out_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=active.group_id,
                    message_thread_id=active.topic_id,
                    document=f,
                    filename=out_path.name,
                )
        except (BadRequest, NetworkError, TimedOut) as e:
            log.warning("send full result failed: %s", e)

    log.info(
        "run.finish id=%d status=%s cost=$%.4f dur=%dms turns=%d",
        active.run_id, final_status, cost, duration_ms, num_turns,
    )

    await _maybe_resume_pending(state, context, active.group_id, active.topic_id)


async def _finish_error(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    active: ActiveRun,
    *,
    exit_code: int,
    stderr: bytes,
    assistant_text: str,
) -> None:
    runs_dir = state.runs_dir / str(active.topic_id)
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts_label = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    partial_path: Path | None = None
    if assistant_text:
        partial_path = runs_dir / f"{ts_label}.partial.md"
        try:
            partial_path.write_text(assistant_text, encoding="utf-8")
        except OSError:
            partial_path = None

    stderr_path: Path | None = None
    if stderr:
        stderr_path = runs_dir / f"{ts_label}.stderr.log"
        try:
            stderr_path.write_bytes(stderr)
        except OSError:
            stderr_path = None

    output_path = str(partial_path) if partial_path else (str(stderr_path) if stderr_path else None)
    await db.finish_run(
        state.conn, active.run_id,
        status="error",
        exit_code=exit_code,
        output_path=output_path,
    )
    state.active.remove(active.group_id, active.topic_id, active.run_id)

    try:
        await context.bot.edit_message_text(
            chat_id=active.group_id,
            message_id=active.working_message_id,
            text=f"❌ Failed (exit {exit_code}). stderr attached.",
        )
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("error edit failed: %s", e)

    if stderr_path and stderr_path.stat().st_size > 0:
        try:
            with open(stderr_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=active.group_id,
                    message_thread_id=active.topic_id,
                    document=f,
                    filename=stderr_path.name,
                )
        except (BadRequest, NetworkError, TimedOut) as e:
            log.warning("send stderr failed: %s", e)

    if partial_path and partial_path.stat().st_size > 0:
        try:
            with open(partial_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=active.group_id,
                    message_thread_id=active.topic_id,
                    document=f,
                    filename=partial_path.name,
                )
        except (BadRequest, NetworkError, TimedOut) as e:
            log.warning("send partial failed: %s", e)

    log.info("run.finish id=%d status=error exit=%d", active.run_id, exit_code)
    await _maybe_resume_pending(state, context, active.group_id, active.topic_id)


async def _finish_killed(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    active: ActiveRun,
    stream_result: StreamResult,
    runs_dir: Path,
    ts_label: str,
) -> None:
    partial_path: Path | None = None
    text = stream_result.aggregated_text
    if text:
        partial_path = runs_dir / f"{ts_label}.partial.md"
        try:
            partial_path.write_text(text, encoding="utf-8")
        except OSError:
            partial_path = None

    elapsed = int(time.monotonic() - active.started_at)
    await db.finish_run(
        state.conn, active.run_id,
        status="killed",
        exit_code=(active.proc.proc.returncode if active.proc else None),
        output_path=str(partial_path) if partial_path else None,
    )
    state.active.remove(active.group_id, active.topic_id, active.run_id)

    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    if h:
        elapsed_str = f"{h}h{m:02d}m{s:02d}s"
    else:
        elapsed_str = f"{m}m{s:02d}s"
    try:
        await context.bot.edit_message_text(
            chat_id=active.group_id,
            message_id=active.working_message_id,
            text=f"🛑 Killed at {elapsed_str}. "
                 + ("Partial output attached." if partial_path else "No output captured."),
        )
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("kill edit failed: %s", e)

    if partial_path and partial_path.stat().st_size > 0:
        try:
            with open(partial_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=active.group_id,
                    message_thread_id=active.topic_id,
                    document=f,
                    filename=partial_path.name,
                )
        except (BadRequest, NetworkError, TimedOut) as e:
            log.warning("send killed partial failed: %s", e)

    log.info("run.finish id=%d status=killed", active.run_id)
    await _maybe_resume_pending(state, context, active.group_id, active.topic_id)


async def _request_kill(state: BotState, run: ActiveRun) -> None:
    if run.kill_requested:
        return
    run.kill_requested = True
    proc = run.proc
    if proc is None:
        return

    async def _kill() -> None:
        proc.terminate_group()
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SEC)
        except asyncio.TimeoutError:
            proc.kill_group()

    asyncio.create_task(_kill())


# ---------------------------------------------------------------------------
# Pending resume after a run finishes
# ---------------------------------------------------------------------------

async def _maybe_resume_pending(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    topic_id: int,
) -> None:
    pending = await db.get_pending(state.conn, group_id, topic_id)
    if pending is None:
        return
    excerpt = pending.prompt[:200]
    if len(pending.prompt) > 200:
        excerpt += "…"
    rel = _fmt_relative(pending.queued_at)
    text = (
        f"Previous run finished. You had queued (sent {rel}):\n"
        f"> {excerpt}\n"
        f"Run it now?"
    )
    try:
        await context.bot.send_message(
            chat_id=group_id,
            message_thread_id=topic_id,
            text=text,
            reply_markup=_confirm_keyboard(topic_id),
        )
    except (BadRequest, NetworkError, TimedOut) as e:
        log.warning("pending prompt failed: %s", e)


# ---------------------------------------------------------------------------
# Callback queries (buttons)
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    q = update.callback_query
    if q is None or not q.data:
        return
    if not is_authorized(state.cfg, update):
        try:
            await q.answer()
        except Exception:
            pass
        return
    try:
        action, _, payload = q.data.partition(":")
    except ValueError:
        await q.answer()
        return

    try:
        await q.answer()
    except (BadRequest, NetworkError, TimedOut):
        pass

    chat_id = q.message.chat_id if q.message else None
    if chat_id is None:
        return

    if action == CB_KILL:
        try:
            run_id = int(payload)
        except ValueError:
            return
        run = state.active.get_by_run_id(run_id)
        if run is None:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except (BadRequest, NetworkError, TimedOut):
                pass
            return
        await _request_kill(state, run)
        return

    if action in (CB_RUN, CB_CANCEL, CB_EDIT):
        try:
            topic_id = int(payload)
        except ValueError:
            return
        await _handle_pending_button(state, context, action, chat_id, topic_id, q)
        return

    if action == CB_NEW:
        instance_name = payload
        await _create_topic_for(state, context, chat_id, instance_name,
                                reply_chat_id=chat_id)
        return


async def _handle_pending_button(
    state: BotState,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    chat_id: int,
    topic_id: int,
    q: Any,
) -> None:
    pending = await db.get_pending(state.conn, chat_id, topic_id)
    if pending is None:
        try:
            await q.edit_message_text("No queued prompt to act on.")
        except (BadRequest, NetworkError, TimedOut):
            pass
        return

    if action == CB_CANCEL:
        await db.delete_pending(state.conn, chat_id, topic_id)
        try:
            await q.edit_message_text("Cancelled.")
        except (BadRequest, NetworkError, TimedOut):
            pass
        return

    if action == CB_EDIT:
        expires = int(time.time()) + EDIT_PENDING_TIMEOUT_SEC
        await db.set_topic_edit_pending(state.conn, chat_id, topic_id, True, expires)
        try:
            await q.edit_message_text(
                "Send a new message in this topic to replace the queued prompt, "
                "or send /cancel to drop it."
            )
        except (BadRequest, NetworkError, TimedOut):
            pass
        return

    if action == CB_RUN:
        await db.delete_pending(state.conn, chat_id, topic_id)
        topic = await db.get_topic(state.conn, chat_id, topic_id)
        if topic is None:
            try:
                await q.edit_message_text("Topic not tracked anymore.")
            except (BadRequest, NetworkError, TimedOut):
                pass
            return
        # Capacity check.
        async with state.spawn_lock:
            if state.active.total() >= MAX_CONCURRENT:
                try:
                    await q.edit_message_text(
                        f"Bot is at capacity ({MAX_CONCURRENT}). Try again shortly."
                    )
                except (BadRequest, NetworkError, TimedOut):
                    pass
                return
        try:
            await q.edit_message_text("Running queued prompt…")
        except (BadRequest, NetworkError, TimedOut):
            pass
        await _start_run(state, context, topic.instance, chat_id, topic_id, pending.prompt)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError, RetryAfter)):
        log.warning("telegram transient error: %s", err)
        return
    log.exception("unhandled handler error: %s", err)


# ---------------------------------------------------------------------------
# App construction + entrypoint
# ---------------------------------------------------------------------------

async def _build_state(cfg: AppConfig) -> BotState:
    conn = await db.open_db(cfg.data_dir)
    await db.reconcile_orphan_runs(conn)
    return BotState(
        cfg=cfg,
        conn=conn,
        active=ActiveRegistry(),
        spawn_lock=asyncio.Lock(),
    )


def _build_application(cfg: AppConfig, state: BotState) -> Application:
    app: Application = ApplicationBuilder().token(cfg.telegram_bot_token).build()
    app.bot_data["state"] = state

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("instances", cmd_instances))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("get", cmd_get))

    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app


async def run(cfg: AppConfig) -> None:
    state = await _build_state(cfg)
    app = _build_application(cfg, state)

    log.info(
        "mylittleclaude starting, instances=%d, allowed_users=%d, allowed_groups=%s",
        len(cfg.servers.instances),
        len(cfg.allowed_user_ids),
        "bootstrap" if cfg.bootstrap_mode else len(cfg.allowed_group_ids),
    )
    if cfg.bootstrap_mode:
        log.warning(
            "BOOTSTRAP MODE: ALLOWED_GROUP_IDS is empty. "
            "Only /chatid from allowed users is accepted. "
            "Set ALLOWED_GROUP_IDS and restart to enable normal operation."
        )

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        # Idle: wait forever until cancelled.
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await state.conn.close()
