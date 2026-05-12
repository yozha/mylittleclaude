from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from .models import AppConfig, ServersFile

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


def _parse_int_csv(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as e:
            raise ConfigError(f"expected integer in CSV, got {part!r}") from e
    return frozenset(out)


def _check_file_mode(path: Path, label: str) -> None:
    """Refuse to start if file is world-readable. Section 8."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
        raise ConfigError(
            f"{label} at {path} is world-accessible (mode {oct(mode & 0o777)}). "
            f"Fix with: chmod 600 {path}"
        )


def load_config(
    env_path: Path | None = None,
    servers_path: Path | None = None,
) -> AppConfig:
    project_root = Path(__file__).resolve().parents[2]
    env_path = env_path or (project_root / ".env")
    servers_path = servers_path or (project_root / "servers.yaml")

    if env_path.exists():
        _check_file_mode(env_path, ".env")
        load_dotenv(env_path, override=False)
    # else: rely on already-set environment (e.g., systemd EnvironmentFile)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is required")

    allowed_users = _parse_int_csv(os.environ.get("ALLOWED_USER_IDS"))
    if not allowed_users:
        raise ConfigError("ALLOWED_USER_IDS must contain at least one user ID")

    allowed_groups = _parse_int_csv(os.environ.get("ALLOWED_GROUP_IDS"))
    bootstrap = len(allowed_groups) == 0

    runtime_user = os.environ.get("USER", "claude")
    claude_bin = os.environ.get("CLAUDE_BIN", "").strip()
    if not claude_bin:
        claude_bin = f"/home/{runtime_user}/.npm-global/bin/claude"

    data_dir = Path(os.environ.get("DATA_DIR", "").strip() or (project_root / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    log_level = (os.environ.get("LOG_LEVEL") or "INFO").upper()

    servers = ServersFile(instances={})
    if servers_path.exists():
        _check_file_mode(servers_path, "servers.yaml")
        try:
            raw = yaml.safe_load(servers_path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"servers.yaml is not valid YAML: {e}") from e
        try:
            servers = ServersFile.model_validate(raw)
        except ValidationError as e:
            raise ConfigError(f"servers.yaml validation failed:\n{e}") from e

        for name, inst in servers.instances.items():
            if inst.is_local:
                continue
            key_path = Path(inst.ssh_key) if inst.ssh_key else None
            if key_path is None or not key_path.exists():
                log.warning(
                    "instance %s: ssh_key %s does not exist (will fail on first use)",
                    name, key_path,
                )
                continue
            mode = key_path.stat().st_mode & 0o777
            if mode not in (0o600, 0o400):
                log.warning(
                    "instance %s: ssh_key %s has mode %s; expected 600 or 400",
                    name, key_path, oct(mode),
                )
    else:
        log.warning("servers.yaml not found at %s; no instances configured", servers_path)

    return AppConfig(
        telegram_bot_token=token,
        allowed_user_ids=allowed_users,
        allowed_group_ids=allowed_groups,
        claude_bin=claude_bin,
        data_dir=data_dir,
        log_level=log_level,
        servers=servers,
        bootstrap_mode=bootstrap,
    )


def check_config_cli() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    print(
        f"OK: instances={len(cfg.servers.instances)} "
        f"users={len(cfg.allowed_user_ids)} "
        f"groups={'bootstrap' if cfg.bootstrap_mode else len(cfg.allowed_group_ids)} "
        f"claude_bin={cfg.claude_bin} "
        f"data_dir={cfg.data_dir}"
    )
    for name, inst in cfg.servers.instances.items():
        print(f"  - {name}: host={inst.host} workdir={inst.workdir}")
    return 0
