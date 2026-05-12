"""Regression tests for Telegram message formatting.

Originally the bot crashed with:
    Can't parse entities: can't find end of the entity starting at byte offset 104

because the /chatid reply used parse_mode=MARKDOWN but the message text
contained unbalanced underscores ("chat_id", "user_id", "_Bootstrap mode_",
"this chat_id"). These tests pin the post-fix behaviour: the chatid reply
must be plain text, and the result-edit MarkdownV2 path must survive
realistic Claude output that contains backticks and triple-fences.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ParseMode

from mylittleclaude.bot import (
    _format_result_message,
    _format_result_message_plain,
    build_chatid_message,
    cmd_chatid,
)
from mylittleclaude.models import AppConfig, ServersFile


# ---------------------------------------------------------------------------
# build_chatid_message — the regression
# ---------------------------------------------------------------------------

def test_build_chatid_message_realistic_inputs():
    """Realistic inputs include underscores in field labels; the previous
    Markdown rendering put those underscores outside of code spans and
    Telegram rejected the message."""
    text = build_chatid_message(
        chat_id=-1001234567890,  # negative supergroup ID
        user_id=12345678,
        bootstrap=True,
    )
    # The chat ID and user ID render verbatim.
    assert "-1001234567890" in text
    assert "12345678" in text
    # The "chat_id" / "user_id" / "Bootstrap mode" labels are present —
    # these are the underscored tokens that previously broke parsing.
    assert "chat_id:" in text
    assert "user_id:" in text
    assert "Bootstrap mode" in text
    # No backslash escaping: it's plain text, not Markdown.
    assert "\\" not in text
    # No legacy-Markdown wrappers around the dynamic content.
    assert "`" not in text
    assert "*" not in text


def test_build_chatid_message_no_bootstrap():
    text = build_chatid_message(chat_id=-100, user_id=1, bootstrap=False)
    assert "Bootstrap mode" not in text
    assert "chat_id: -100" in text
    assert "your user_id: 1" in text


@pytest.mark.asyncio
async def test_cmd_chatid_sends_plain_text():
    """The handler must not pass parse_mode= to reply_text — otherwise we
    regress to the original bug. This test simulates the failing payload:
    a negative supergroup chat_id and bootstrap mode, with special chars
    in the user's display name (which the handler doesn't even use, but
    is here to mimic a realistic update object)."""
    cfg = AppConfig(
        telegram_bot_token="dummy",
        allowed_user_ids=frozenset({12345}),
        allowed_group_ids=frozenset(),
        claude_bin="/bin/true",
        data_dir=__import__("pathlib").Path("/tmp"),
        log_level="INFO",
        servers=ServersFile(instances={}),
        bootstrap_mode=True,
    )

    reply_text = AsyncMock()
    message = MagicMock()
    message.reply_text = reply_text

    update = MagicMock()
    update.effective_message = message
    chat = MagicMock()
    chat.id = -1001234567890
    update.effective_chat = chat
    # A user whose first_name contains chars that broke legacy Markdown
    # in the past. Not consumed by the handler, but exercises a realistic
    # update shape.
    user = MagicMock()
    user.id = 12345
    user.first_name = "Some_User*Name (test)"
    update.effective_user = user

    context = MagicMock()
    app = MagicMock()
    app.bot_data = {
        "state": MagicMock(cfg=cfg),
    }
    context.application = app

    await cmd_chatid(update, context)

    reply_text.assert_awaited_once()
    args, kwargs = reply_text.await_args
    # parse_mode must not be set — that's the whole fix.
    assert "parse_mode" not in kwargs, (
        f"cmd_chatid passed parse_mode={kwargs.get('parse_mode')!r}; "
        "this regresses the original 'can't find end of the entity' bug."
    )
    sent_text = args[0] if args else kwargs.get("text", "")
    assert "-1001234567890" in sent_text
    assert "12345" in sent_text
    assert "Bootstrap mode" in sent_text


# ---------------------------------------------------------------------------
# _format_result_message — the other risky parse_mode site
# ---------------------------------------------------------------------------

def test_format_result_message_escapes_header_for_v2():
    """MarkdownV2 requires `.`, `-`, `(`, `)` etc. to be escaped outside of
    code/pre entities. The header has all of these (cost has `.`, duration
    has digit+letter, parens appear when is_error=True)."""
    body = _format_result_message(
        cost=0.0234,
        duration_ms=47000,
        num_turns=6,
        is_error=False,
        inline="ok",
        truncated=False,
    )
    # Period in $0.0234 must be backslash-escaped for MarkdownV2.
    assert r"0\.0234" in body
    # The dot in the · separator is unicode middle-dot (U+00B7), not a `.`
    # — that's why we only need to escape the literal periods in the cost.
    # The pre-block uses triple-backticks as fence.
    assert "```" in body


def test_format_result_message_handles_triple_backticks_in_result():
    """The whole point of switching to MarkdownV2 pre-entity escaping is
    that Claude commonly returns content containing ``` (e.g., it shows
    code blocks in its answer). Legacy Markdown's fence would close on
    the first ``` and the remainder would be parsed as markdown, blowing
    up the whole message."""
    claude_output = (
        "Here's the script:\n"
        "```python\n"
        "def hi(): return 'world'\n"
        "```\n"
        "Done."
    )
    body = _format_result_message(
        cost=0.01,
        duration_ms=1000,
        num_turns=1,
        is_error=False,
        inline=claude_output,
        truncated=False,
    )
    # Backticks inside the pre block must be backslash-escaped.
    assert r"\`" in body
    # And our outer fence still exists.
    assert body.count("```") == 2


def test_format_result_message_handles_backslashes_in_result():
    body = _format_result_message(
        cost=0.0,
        duration_ms=1,
        num_turns=1,
        is_error=False,
        inline=r"path\to\file and a single \\",
        truncated=False,
    )
    # Each backslash must be doubled inside the pre block.
    assert r"\\to\\file" in body


def test_format_result_message_truncated_marker():
    body = _format_result_message(
        cost=0.0, duration_ms=1, num_turns=1,
        is_error=False, inline="tail-only", truncated=True,
    )
    # Marker sits inside the pre block: only ` and \ are escaped there,
    # so '(', ')', '.' pass through literally — that's what Telegram wants.
    assert "(truncated, see attached)" in body
    assert "tail-only" in body


def test_format_result_message_plain_fallback_is_unescaped():
    plain = _format_result_message_plain(
        cost=0.5, duration_ms=2000, num_turns=3,
        is_error=False, inline="just text",
        truncated=False,
    )
    # No escape backslashes in plain mode.
    assert "\\" not in plain
    assert "$0.5000" in plain
    assert "just text" in plain


def test_format_result_message_is_error_header():
    body = _format_result_message(
        cost=0.0, duration_ms=0, num_turns=0,
        is_error=True, inline="oops", truncated=False,
    )
    # The header runs through escape_markdown(v2), so the underscore in
    # "is_error" is escaped to "is\_error". That's the *correct* output
    # for MarkdownV2 — Telegram displays it as a literal underscore.
    assert r"is\_error" in body
    # warning icon, not check mark
    assert "⚠️" in body


# ---------------------------------------------------------------------------
# Sanity: no obvious unbalanced legacy-Markdown markers in our static texts
# ---------------------------------------------------------------------------

def _count_unbalanced_markers(text: str) -> dict[str, int]:
    """Crude check: count `_`, `*`, `` ` `` markers outside of code spans
    and report any that look unbalanced. This isn't a real Markdown parser,
    but it catches the class of bug that hit /chatid (odd-count markers
    in a small message)."""
    # Strip inline code spans first.
    stripped = re.sub(r"`[^`\n]*`", "", text)
    return {
        "_": stripped.count("_"),
        "*": stripped.count("*"),
        "`": stripped.count("`"),
    }


def test_static_markdown_strings_are_balanced():
    """The kept-MARKDOWN sites use static strings only. Verify they have
    even counts of legacy-Markdown markers outside of code spans."""
    from mylittleclaude.bot import USAGE
    counts = _count_unbalanced_markers(USAGE)
    for marker, n in counts.items():
        assert n % 2 == 0, (
            f"USAGE has odd count of '{marker}' ({n}) outside code spans — "
            "Telegram will reject this with 'can't find end of the entity'."
        )
