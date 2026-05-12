from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .models import HeartbeatState

log = logging.getLogger(__name__)


def summarize_event(event: dict[str, Any]) -> str:
    """Map a stream-json event to a short last-activity label.

    Mirrors the table in §4.3 of SPEC.md.
    """
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        return "initializing"
    if etype == "assistant":
        return "assistant text"
    if etype == "user":
        return "tool result"
    if etype == "result":
        return "finalizing"
    if etype == "tool_use":
        name = event.get("name") or event.get("tool") or "?"
        return f"tool: {name}"
    if etype == "stream_event":
        tool = event.get("tool")
        if tool:
            return f"tool: {tool}"
    return str(etype) if etype else "event"


def extract_assistant_text(event: dict[str, Any]) -> str | None:
    """Pull plain text out of an 'assistant' event if present.

    The stream-json format embeds an Anthropic-API-shaped message. We try a
    couple of likely shapes and fall through silently when the event has no
    text (e.g., tool_use-only turns).
    """
    msg = event.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            if parts:
                return "".join(parts)
        elif isinstance(content, str):
            return content
    return None


@dataclass
class StreamResult:
    """Outcome of consuming a full stream until process exit."""
    result_event: dict[str, Any] | None = None
    assistant_text_parts: list[str] = field(default_factory=list)
    parse_errors: int = 0
    line_count: int = 0

    @property
    def aggregated_text(self) -> str:
        return "".join(self.assistant_text_parts)


async def consume_stream(
    lines: AsyncIterator[bytes],
    heartbeat: HeartbeatState,
    *,
    on_event: callable | None = None,
) -> StreamResult:
    """Read NDJSON lines from `lines`, update heartbeat, capture result.

    `on_event(event_dict)` if given is invoked for each parsed event — used
    by the bot to e.g. push tool-name updates upstream.
    """
    out = StreamResult()
    async for raw in lines:
        out.line_count += 1
        # bytes → str; strip newline
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                out.parse_errors += 1
                continue
        else:
            line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.warning("unparseable stream line: %r", line[:200])
            out.parse_errors += 1
            continue
        if not isinstance(event, dict):
            continue
        heartbeat.last_activity_ts = time.monotonic()
        heartbeat.last_activity_summary = summarize_event(event)

        if event.get("type") == "assistant":
            text = extract_assistant_text(event)
            if text:
                out.assistant_text_parts.append(text)
        if event.get("type") == "result":
            out.result_event = event
        if on_event is not None:
            try:
                on_event(event)
            except Exception:
                log.exception("on_event callback raised; continuing")
    return out
