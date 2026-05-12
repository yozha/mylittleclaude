from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from mylittleclaude.installer.paths import InstallPaths
from mylittleclaude.installer.update import plan


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


def test_plan_upgrade(tmp_path):
    p = _paths(tmp_path)
    when = datetime.datetime(2026, 5, 12, 12, 0, 0)
    pln = plan(p, current="0.1.0", target="0.2.0", ref="v0.2.0",
               is_branch=False, now=when)
    assert pln.is_downgrade is False
    assert pln.is_noop is False
    assert pln.backup_dir.name == "v0.2.0-20260512-120000"
    # Expected actions in order
    titles = " | ".join(pln.actions)
    assert "git checkout v0.2.0" in titles
    assert "stop systemd service" in titles
    assert "run DB migrations" in titles
    assert "start systemd service" in titles
    assert pln.warnings == []  # no branch, no downgrade


def test_plan_downgrade_warns(tmp_path):
    p = _paths(tmp_path)
    pln = plan(p, current="0.3.0", target="0.2.0", ref="v0.2.0",
               is_branch=False, now=datetime.datetime(2026, 5, 12))
    assert pln.is_downgrade is True
    assert any("older than current" in w for w in pln.warnings)


def test_plan_noop(tmp_path):
    p = _paths(tmp_path)
    pln = plan(p, current="0.2.0", target="0.2.0", ref="v0.2.0",
               is_branch=False, now=datetime.datetime(2026, 5, 12))
    assert pln.is_noop is True


def test_plan_branch_warns(tmp_path):
    p = _paths(tmp_path)
    pln = plan(p, current="0.2.0", target="main", ref="main",
               is_branch=True, now=datetime.datetime(2026, 5, 12))
    assert any("dev mode" in w for w in pln.warnings)
