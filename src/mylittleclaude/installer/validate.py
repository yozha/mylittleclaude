"""Pure-function input validators used by the wizard.

Each validator takes a raw string and returns None on success, or a short
human-readable error message on failure. Keeping them pure means the wizard
unit tests can hammer them directly.
"""

from __future__ import annotations

import re
from pathlib import Path

# Spec §2.3 step 3 — BotFather tokens look like "<digits>:<base64ish>".
TOKEN_RE = re.compile(r"^\d{8,}:[A-Za-z0-9_-]{30,}$")

# Spec §5.2 / models.py — instance name regex.
INSTANCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

# Spec §5.2 — bare host (defense-in-depth on remotes). The wizard accepts
# `user@host` or `user@host:port`; we strip :port before this check.
HOST_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")

# user@host[:port] — port is optional and numeric.
REMOTE_RE = re.compile(r"^([A-Za-z0-9._-]+)@([A-Za-z0-9._-]+)(?::(\d{1,5}))?$")


def token(s: str) -> str | None:
    if not TOKEN_RE.match(s):
        return (
            "doesn't look like a BotFather token "
            "(expected '<digits>:<30+ chars>')"
        )
    return None


def positive_int(s: str) -> str | None:
    try:
        v = int(s)
    except ValueError:
        return f"not a number: {s!r}"
    if v <= 0:
        return "must be a positive integer"
    return None


def user_id_csv(s: str) -> str | None:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return "at least one user ID is required"
    for p in parts:
        e = positive_int(p)
        if e is not None:
            return f"in {p!r}: {e}"
    return None


def group_id(s: str) -> str | None:
    """Group IDs are integers; supergroups are large negatives starting -100."""
    try:
        int(s)
    except ValueError:
        return f"not a number: {s!r}"
    return None


def instance_name(s: str) -> str | None:
    if not INSTANCE_NAME_RE.match(s):
        return (
            "must be 1-31 chars, [a-z0-9_-], starting with a letter or digit"
        )
    return None


def absolute_path(s: str) -> str | None:
    if not s.startswith("/"):
        return "must be an absolute path (starting with /)"
    return None


def remote_host(s: str) -> str | None:
    """Accept `user@host` or `user@host:port`. Then run defense-in-depth regex."""
    if not REMOTE_RE.match(s):
        return "expected user@host or user@host:port"
    # strip port for the broader regex check
    base = s.split(":", 1)[0]
    if not HOST_RE.match(base):
        return "contains characters outside [a-zA-Z0-9._@-]"
    return None


def ssh_key_path(s: str) -> str | None:
    """Path must be absolute and the file must exist. Mode is checked separately
    because we want to *offer* to chmod rather than refuse outright."""
    e = absolute_path(s)
    if e is not None:
        return e
    p = Path(s)
    if not p.exists():
        return f"no such file: {s}"
    if not p.is_file():
        return f"not a regular file: {s}"
    return None


def log_level(s: str) -> str | None:
    if s.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        return "must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
    return None


def parse_user_id_csv(s: str) -> list[int]:
    """Companion to user_id_csv — caller validates first, then parses."""
    return [int(p.strip()) for p in s.split(",") if p.strip()]
