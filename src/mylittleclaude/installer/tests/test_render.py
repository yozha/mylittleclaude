from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from mylittleclaude.installer.paths import InstallPaths
from mylittleclaude.installer.render import (
    atomic_write,
    backup_existing,
    render_env,
    render_servers,
    write_all,
)
from mylittleclaude.installer.state import InstanceDraft, WizardState


def _paths(tmp_path: Path) -> InstallPaths:
    return InstallPaths(
        install_dir=tmp_path,
        env_file=tmp_path / ".env",
        servers_file=tmp_path / "servers.yaml",
        data_dir=tmp_path / "data",
        venv_dir=tmp_path / ".venv",
        backup_dir=tmp_path / ".backup",
        systemd_unit_src=tmp_path / "systemd" / "mylittleclaude.service",
        systemd_unit_dst=Path("/etc/systemd/system/mylittleclaude.service"),
        pyproject=tmp_path / "pyproject.toml",
    )


def test_render_env_complete(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.bot_token = "123456789:ABCdef-XYZ_lots-of-base64ish-characters-here"
    state.allowed_user_ids = [11111111, 22222222]
    state.allowed_group_ids = [-1001234567890]
    state.log_level = "INFO"
    out = render_env(state)
    assert "TELEGRAM_BOT_TOKEN=123456789:" in out
    assert "ALLOWED_USER_IDS=11111111,22222222" in out
    assert "ALLOWED_GROUP_IDS=-1001234567890" in out
    assert "LOG_LEVEL=INFO" in out


def test_render_env_deferred_fields_blank(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    # No token, no users, no groups.
    out = render_env(state)
    assert "TELEGRAM_BOT_TOKEN=\n" in out
    assert "ALLOWED_USER_IDS=\n" in out
    assert "ALLOWED_GROUP_IDS=\n" in out


def test_render_servers_with_instances(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.instances = [
        InstanceDraft(name="sandbox", description="Local sandbox",
                      host="local", workdir="/home/u/projects/sandbox"),
        InstanceDraft(name="gpu-box", description="GPU dev",
                      host="claude@gpu-box.internal",
                      workdir="/home/claude/projects/work",
                      ssh_key="/home/u/.ssh/mlc_ed25519"),
    ]
    out = render_servers(state)
    parsed = yaml.safe_load(out)
    assert "instances" in parsed
    assert parsed["instances"]["sandbox"]["host"] == "local"
    assert parsed["instances"]["sandbox"]["workdir"] == "/home/u/projects/sandbox"
    assert "ssh_key" not in parsed["instances"]["sandbox"]
    assert parsed["instances"]["gpu-box"]["ssh_key"] == "/home/u/.ssh/mlc_ed25519"


def test_render_servers_empty(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    out = render_servers(state)
    parsed = yaml.safe_load(out)
    assert parsed == {"instances": {}}


def test_atomic_write_sets_600(tmp_path):
    target = tmp_path / "file.env"
    atomic_write(target, "hello\n")
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600
    assert target.read_text() == "hello\n"


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "file.env"
    target.write_text("old")
    target.chmod(0o644)
    atomic_write(target, "new\n")
    assert target.read_text() == "new\n"
    assert target.stat().st_mode & 0o777 == 0o600
    # No leftover .tmp
    assert not (tmp_path / "file.env.tmp").exists()


def test_backup_existing_creates_timestamped_copy(tmp_path):
    target = tmp_path / "file.env"
    target.write_text("original")
    target.chmod(0o600)
    bak = backup_existing(target)
    assert bak is not None
    assert bak.exists()
    assert bak.name.startswith("file.env.backup.")
    assert bak.read_text() == "original"
    assert bak.stat().st_mode & 0o777 == 0o600


def test_backup_existing_prunes_to_keep_5(tmp_path):
    target = tmp_path / "file.env"
    # Seed seven fake backups with distinct timestamps.
    for i in range(7):
        (tmp_path / f"file.env.backup.2024010{i}-000000").write_text(f"b{i}")
    target.write_text("current")
    backup_existing(target, keep=5)
    backups = sorted(p.name for p in tmp_path.glob("file.env.backup.*"))
    # 5 originals retained + 1 freshly made = 6 total when keep=5 means we
    # delete the OLDEST until ≤5. So we should have 5 left including the new one.
    # We seeded 7 + 1 new = 8 before prune; keep=5 → 5 remain.
    assert len(backups) == 5


def test_write_all_writes_both_with_correct_perms(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.bot_token = "111:" + "A" * 40
    state.allowed_user_ids = [11111111]
    state.instances = [InstanceDraft(
        name="sandbox", description="x", host="local",
        workdir="/home/u/projects/sandbox",
    )]
    result = write_all(state)
    assert result.env_written.exists()
    assert result.servers_written.exists()
    assert result.env_written.stat().st_mode & 0o777 == 0o600
    assert result.servers_written.stat().st_mode & 0o777 == 0o600
    assert result.backups == []


def test_write_all_backs_up_existing(tmp_path):
    paths = _paths(tmp_path)
    paths.env_file.write_text("OLD=1")
    paths.servers_file.write_text("instances: {}\n")
    state = WizardState(paths=paths)
    state.bot_token = "111:" + "A" * 40
    state.allowed_user_ids = [11111111]
    result = write_all(state)
    # One backup per existing file.
    assert len(result.backups) == 2
    assert any(b.name.startswith(".env.backup.") for b in result.backups)
    assert any(b.name.startswith("servers.yaml.backup.") for b in result.backups)
