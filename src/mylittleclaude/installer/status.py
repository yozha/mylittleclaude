"""`mylittleclaude-setup status` — read-only inspection.

Exit code 0 if everything is configured AND service is active; non-zero
otherwise. The detailed printout is human-targeted, not parser-friendly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import discover
from .state import load_existing
from .tui import good, info, say, warn
from .version import installed_version


def run() -> int:
    paths = discover()
    say(f"Install dir:     {paths.install_dir}")
    if not paths.install_dir.exists():
        warn("(install dir does not exist)")
        return 1

    say(f"Version:         {installed_version()}")
    say(f".env file:       {paths.env_file} {'(present)' if paths.env_file.exists() else '(missing)'}")
    say(f"servers.yaml:    {paths.servers_file} {'(present)' if paths.servers_file.exists() else '(missing)'}")
    say(f"Data dir:        {paths.data_dir} {'(present)' if paths.data_dir.exists() else '(missing)'}")
    say()

    state = load_existing(paths)

    say("Configuration:")
    _row("TELEGRAM_BOT_TOKEN", state.has_token, hint="set token in .env or re-run wizard")
    _row("ALLOWED_USER_IDS", state.has_users, hint="add your Telegram user ID")
    _row("ALLOWED_GROUP_IDS", state.has_groups, hint="bootstrap mode active until set")
    _row("instances", state.has_instances, hint="add at least one instance")

    say()
    svc_active = _systemctl_active("mylittleclaude")
    say(f"Service active:  {'yes' if svc_active else 'no'}")
    say()

    fully_ready = (
        state.has_token and state.has_users
        and state.has_groups and state.has_instances
        and svc_active
    )
    if fully_ready:
        good("All systems go.")
        return 0
    info("Run `mylittleclaude-setup` to address any deferred fields.")
    return 1


def _row(label: str, ok: bool, *, hint: str) -> None:
    if ok:
        say(f"  ✓ {label}")
    else:
        say(f"  ✗ {label}  → {hint}")


def _systemctl_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "active"
