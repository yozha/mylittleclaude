from __future__ import annotations

import os
from pathlib import Path

import pytest

from mylittleclaude.config import ConfigError, load_config
from mylittleclaude.models import InstanceConfig, ServersFile


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    for k in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", "ALLOWED_GROUP_IDS",
              "CLAUDE_BIN", "DATA_DIR", "LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_missing_token_raises(isolated_env, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(env_path=isolated_env / "no-such-env",
                    servers_path=isolated_env / "no-such-servers")


def test_missing_users_raises(isolated_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_config(env_path=isolated_env / "no-such-env",
                    servers_path=isolated_env / "no-such-servers")


def test_bootstrap_mode_when_groups_empty(isolated_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("ALLOWED_GROUP_IDS", "")
    monkeypatch.setenv("DATA_DIR", str(isolated_env / "data"))
    cfg = load_config(env_path=isolated_env / "no-env",
                      servers_path=isolated_env / "no-servers")
    assert cfg.bootstrap_mode
    assert 42 in cfg.allowed_user_ids
    assert cfg.allowed_group_ids == frozenset()


def test_servers_file_validates(isolated_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("DATA_DIR", str(isolated_env / "data"))
    servers = isolated_env / "servers.yaml"
    _write(servers, """
instances:
  local-one:
    description: ok
    host: local
    workdir: /tmp/x
""")
    cfg = load_config(env_path=isolated_env / "no-env", servers_path=servers)
    assert "local-one" in cfg.servers.instances
    assert cfg.servers.instances["local-one"].is_local


def test_servers_rejects_bad_name():
    with pytest.raises(Exception):
        ServersFile.model_validate({"instances": {"Bad-Name!": {
            "host": "local", "workdir": "/tmp/x",
        }}})


def test_instance_remote_requires_ssh_key():
    with pytest.raises(Exception):
        InstanceConfig.model_validate({
            "host": "user@host.example", "workdir": "/srv",
        })


def test_instance_rejects_relative_workdir():
    with pytest.raises(Exception):
        InstanceConfig.model_validate({
            "host": "local", "workdir": "relative/path",
        })


def test_instance_rejects_bad_host():
    with pytest.raises(Exception):
        InstanceConfig.model_validate({
            "host": "user@host;rm-rf",
            "workdir": "/srv",
            "ssh_key": "/k",
        })


def test_world_readable_env_refused(isolated_env, monkeypatch):
    env = isolated_env / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=t\nALLOWED_USER_IDS=1\n")
    os.chmod(env, 0o644)  # world-readable
    with pytest.raises(ConfigError, match="world-accessible"):
        load_config(env_path=env, servers_path=isolated_env / "no-servers")
