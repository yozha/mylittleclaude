"""Wizard state: in-memory collection of operator answers.

Spec §2.3: nothing is written to disk until the Review step confirms. This
module owns the dataclasses that flow through the steps and the helpers for
loading current state on reconfigure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import InstallPaths


@dataclass
class InstanceDraft:
    name: str = ""
    description: str = ""
    host: str = "local"  # "local" or "user@host[:port]"
    workdir: str = ""
    ssh_key: str | None = None


@dataclass
class WizardState:
    paths: InstallPaths

    # .env fields
    bot_token: str | None = None
    allowed_user_ids: list[int] = field(default_factory=list)
    allowed_group_ids: list[int] = field(default_factory=list)
    claude_bin: str | None = None  # None means "use the default in load_config"
    data_dir: str | None = None    # absolute path, optional
    log_level: str = "INFO"

    # servers.yaml fields
    instances: list[InstanceDraft] = field(default_factory=list)

    # tracking
    deferred: set[str] = field(default_factory=set)
    is_reconfigure: bool = False

    def mark_deferred(self, field_name: str) -> None:
        self.deferred.add(field_name)

    def unmark_deferred(self, field_name: str) -> None:
        self.deferred.discard(field_name)

    @property
    def has_token(self) -> bool:
        return bool(self.bot_token)

    @property
    def has_users(self) -> bool:
        return bool(self.allowed_user_ids)

    @property
    def has_groups(self) -> bool:
        return bool(self.allowed_group_ids)

    @property
    def has_instances(self) -> bool:
        return bool(self.instances)


def load_existing(paths: InstallPaths) -> WizardState:
    """Build a WizardState pre-filled from disk for reconfigure flows.

    Reads .env via simple parser (NOT python-dotenv, which mutates os.environ)
    and servers.yaml via PyYAML. Missing files leave fields at defaults.
    """
    state = WizardState(paths=paths, is_reconfigure=paths.configured)

    if paths.env_file.exists():
        env = _parse_env_file(paths.env_file)
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            state.bot_token = token
        users = _parse_int_csv(env.get("ALLOWED_USER_IDS", ""))
        state.allowed_user_ids = list(users)
        groups = _parse_int_csv(env.get("ALLOWED_GROUP_IDS", ""))
        state.allowed_group_ids = list(groups)
        cb = env.get("CLAUDE_BIN", "").strip()
        if cb:
            state.claude_bin = cb
        dd = env.get("DATA_DIR", "").strip()
        if dd:
            state.data_dir = dd
        ll = env.get("LOG_LEVEL", "").strip()
        if ll:
            state.log_level = ll

    if paths.servers_file.exists():
        state.instances = _load_instances(paths.servers_file)

    # Mark anything missing as deferred so the summary reflects reality.
    if not state.has_token:
        state.mark_deferred("TELEGRAM_BOT_TOKEN")
    if not state.has_users:
        state.mark_deferred("ALLOWED_USER_IDS")
    if not state.has_groups:
        state.mark_deferred("ALLOWED_GROUP_IDS")

    return state


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # strip surrounding quotes if matched
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def _parse_int_csv(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _load_instances(path: Path) -> list[InstanceDraft]:
    import yaml
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    raw = data.get("instances") or {}
    if not isinstance(raw, dict):
        return []
    out: list[InstanceDraft] = []
    for name, conf in raw.items():
        if not isinstance(conf, dict):
            continue
        out.append(InstanceDraft(
            name=str(name),
            description=str(conf.get("description", "")),
            host=str(conf.get("host", "local")),
            workdir=str(conf.get("workdir", "")),
            ssh_key=(str(conf["ssh_key"]) if conf.get("ssh_key") else None),
        ))
    return out


def default_claude_bin() -> str:
    user = os.environ.get("USER", "claude")
    return f"/home/{user}/.npm-global/bin/claude"
