"""Render WizardState to `.env` and `servers.yaml` with atomic writes.

Spec §2.3.10: render to a `.tmp` sibling, fsync, rename. Then chmod 600.
Old files are rotated to `.backup.YYYYMMDD-HHMMSS`; keep the last 5.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .state import InstanceDraft, WizardState

ENV_HEADER = """\
# mylittleclaude — written by the installer wizard.
# Edit by hand if you like; re-running `mylittleclaude-setup` will preserve
# any changes you make here as the starting state.
"""

SERVERS_HEADER = """\
# mylittleclaude — written by the installer wizard.
# Each entry is a (host, workdir) pair Claude Code is run against.
# Instance names must match ^[a-z0-9][a-z0-9_-]{0,30}$.
"""


@dataclass
class WriteResult:
    env_written: Path
    servers_written: Path
    backups: list[Path]


def render_env(state: WizardState) -> str:
    """Return the textual contents of `.env`. Pure for testability."""
    lines = [ENV_HEADER]

    if state.bot_token:
        lines.append(f"TELEGRAM_BOT_TOKEN={state.bot_token}")
    else:
        lines.append("# TELEGRAM_BOT_TOKEN=  # deferred — bot will not start")
        lines.append("TELEGRAM_BOT_TOKEN=")

    if state.allowed_user_ids:
        lines.append(
            "ALLOWED_USER_IDS="
            + ",".join(str(i) for i in state.allowed_user_ids)
        )
    else:
        lines.append("# ALLOWED_USER_IDS=  # deferred — bot will not start")
        lines.append("ALLOWED_USER_IDS=")

    if state.allowed_group_ids:
        lines.append(
            "ALLOWED_GROUP_IDS="
            + ",".join(str(i) for i in state.allowed_group_ids)
        )
    else:
        lines.append("# ALLOWED_GROUP_IDS=  # empty → bootstrap mode")
        lines.append("ALLOWED_GROUP_IDS=")

    if state.claude_bin:
        lines.append(f"CLAUDE_BIN={state.claude_bin}")
    else:
        lines.append("# CLAUDE_BIN=/home/<user>/.npm-global/bin/claude")

    if state.data_dir:
        lines.append(f"DATA_DIR={state.data_dir}")
    else:
        lines.append("# DATA_DIR=./data")

    lines.append(f"LOG_LEVEL={state.log_level or 'INFO'}")

    return "\n".join(lines) + "\n"


def render_servers(state: WizardState) -> str:
    """Return the textual contents of `servers.yaml`."""
    payload: dict[str, dict] = {}
    for inst in state.instances:
        entry: dict[str, str] = {
            "description": inst.description or "",
            "host": inst.host,
            "workdir": inst.workdir,
        }
        if inst.host != "local" and inst.ssh_key:
            entry["ssh_key"] = inst.ssh_key
        payload[inst.name] = entry

    if not payload:
        body = "instances: {}\n"
    else:
        body = yaml.safe_dump({"instances": payload}, sort_keys=False)
    return SERVERS_HEADER + body


def atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write to a sibling .tmp, fsync, rename, chmod.

    Permissions are applied to the tmp file before the rename so the target
    never exists with looser perms, even momentarily.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, mode)


def backup_existing(path: Path, *, keep: int = 5) -> Path | None:
    """If `path` exists, copy it to `<path>.backup.<UTC>`. Prune to `keep` total."""
    if not path.exists():
        return None
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.backup.{ts}")
    # If somehow we collide (rapid re-run), append a microsecond suffix
    if dst.exists():
        dst = path.with_name(f"{path.name}.backup.{ts}-{os.getpid()}")
    shutil.copy2(path, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    _prune_backups(path, keep=keep)
    return dst


def _prune_backups(path: Path, *, keep: int) -> list[Path]:
    """Keep the `keep` most recent `<name>.backup.<ts>` siblings, delete rest."""
    prefix = f"{path.name}.backup."
    candidates: list[Path] = []
    for sib in path.parent.iterdir():
        if sib.name.startswith(prefix):
            candidates.append(sib)
    candidates.sort(key=lambda p: p.name, reverse=True)
    removed: list[Path] = []
    for old in candidates[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


def write_all(state: WizardState) -> WriteResult:
    """Back up existing configs, then atomically write the new ones."""
    backups: list[Path] = []
    for target in (state.paths.env_file, state.paths.servers_file):
        b = backup_existing(target)
        if b is not None:
            backups.append(b)

    atomic_write(state.paths.env_file, render_env(state))
    atomic_write(state.paths.servers_file, render_servers(state))

    return WriteResult(
        env_written=state.paths.env_file,
        servers_written=state.paths.servers_file,
        backups=backups,
    )


def restore_backups(backups: list[Path], *, original_paths: list[Path]) -> None:
    """Roll back a write_all() if validation fails.

    Maps each backup back to its origin by stripping the timestamp suffix.
    """
    for b in backups:
        # backup name is "<orig>.backup.<ts>"
        name = b.name
        marker = ".backup."
        if marker not in name:
            continue
        orig_name = name.split(marker, 1)[0]
        for orig in original_paths:
            if orig.name == orig_name:
                shutil.copy2(b, orig)
                try:
                    os.chmod(orig, 0o600)
                except OSError:
                    pass
                break


def coerce_instance_to_dict(inst: InstanceDraft) -> dict[str, str]:
    """Used by the review screen to show pre-write state."""
    d: dict[str, str] = {
        "description": inst.description,
        "host": inst.host,
        "workdir": inst.workdir,
    }
    if inst.ssh_key:
        d["ssh_key"] = inst.ssh_key
    return d
