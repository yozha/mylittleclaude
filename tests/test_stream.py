from __future__ import annotations

import json
import time

import pytest

from mylittleclaude.models import HeartbeatState
from mylittleclaude.stream import (
    StreamResult,
    consume_stream,
    extract_assistant_text,
    summarize_event,
)


async def _aiter(lines: list[bytes]):
    for ln in lines:
        yield ln


def test_summarize_event_known_types():
    assert summarize_event({"type": "system", "subtype": "init"}) == "initializing"
    assert summarize_event({"type": "assistant"}) == "assistant text"
    assert summarize_event({"type": "user"}) == "tool result"
    assert summarize_event({"type": "result"}) == "finalizing"
    assert summarize_event({"type": "tool_use", "name": "Bash"}) == "tool: Bash"
    assert summarize_event({"type": "stream_event", "tool": "Edit"}) == "tool: Edit"
    assert summarize_event({"type": "weird"}) == "weird"
    assert summarize_event({}) == "event"


def test_extract_assistant_text_list_shape():
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": " world"},
            ]
        },
    }
    assert extract_assistant_text(ev) == "Hello world"


def test_extract_assistant_text_string_shape():
    ev = {"type": "assistant", "message": {"content": "plain string"}}
    assert extract_assistant_text(ev) == "plain string"


def test_extract_assistant_text_no_text_returns_none():
    ev = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash"}
    ]}}
    assert extract_assistant_text(ev) is None


@pytest.mark.asyncio
async def test_consume_stream_collects_result_and_text():
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi "}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "there"}]}},
        {
            "type": "result",
            "result": "hi there",
            "session_id": "abc",
            "total_cost_usd": 0.0123,
            "duration_ms": 1500,
            "num_turns": 2,
            "is_error": False,
            "subtype": "success",
        },
    ]
    lines = [(json.dumps(e) + "\n").encode() for e in events]
    hb = HeartbeatState(started_at=time.monotonic())
    out = await consume_stream(_aiter(lines), hb)
    assert isinstance(out, StreamResult)
    assert out.result_event is not None
    assert out.result_event["session_id"] == "abc"
    assert out.aggregated_text == "hi there"
    assert hb.last_activity_summary == "finalizing"


@pytest.mark.asyncio
async def test_consume_stream_skips_blank_and_invalid_lines():
    lines = [
        b"\n",
        b"   \n",
        b"not json\n",
        b'{"type": "assistant", "message": {"content": "ok"}}\n',
        b'{"type": "result", "result": "done", "session_id": "z", "total_cost_usd": 0, "duration_ms": 1, "num_turns": 1, "is_error": false}\n',
    ]
    hb = HeartbeatState(started_at=time.monotonic())
    out = await consume_stream(_aiter(lines), hb)
    assert out.parse_errors == 1
    assert out.aggregated_text == "ok"
    assert out.result_event["session_id"] == "z"


@pytest.mark.asyncio
async def test_consume_stream_no_result_event():
    """Process exits before emitting `result` — caller decides what to do."""
    lines = [
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}\n',
    ]
    hb = HeartbeatState(started_at=time.monotonic())
    out = await consume_stream(_aiter(lines), hb)
    assert out.result_event is None
    assert out.aggregated_text == "partial"


@pytest.mark.asyncio
async def test_consume_stream_handles_non_dict_json():
    lines = [b'"a bare string"\n', b'42\n']
    hb = HeartbeatState(started_at=time.monotonic())
    out = await consume_stream(_aiter(lines), hb)
    assert out.parse_errors == 0
    assert out.result_event is None
