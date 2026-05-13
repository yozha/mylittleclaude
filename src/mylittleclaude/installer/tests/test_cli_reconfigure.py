"""Tests for cli._classify_check_config_failure (v0.2.3).

v0.2.2 surfaced a bug where `_cmd_reconfigure` aborted on any non-zero exit
from `--check-config`. But the bot's --check-config refuses *by contract*
when required fields are missing (spec §2.8) — that's not a failure, it's a
bootstrap-mode install. The classifier separates expected refusal from
unexpected validation failure.

Each test pins one branch of the four-state matrix:

| TOKEN deferred | USER_IDS deferred | classifier result          |
|---|---|---|
| no             | no                | None (unexpected → abort)  |
| yes            | no                | "until TELEGRAM_BOT_TOKEN" |
| no             | yes               | "until at least one ALLOWED_USER_IDS" |
| yes            | yes               | "both TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS" |
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mylittleclaude.installer import cli
from mylittleclaude.installer.paths import InstallPaths
from mylittleclaude.installer.state import WizardState


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


# --- four-state matrix -------------------------------------------------------


def test_classify_no_deferred_returns_none(tmp_path):
    """A check-config failure with nothing deferred is a real bug — abort."""
    state = WizardState(paths=_paths(tmp_path))
    assert cli._classify_check_config_failure(state) is None


def test_classify_token_only(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("TELEGRAM_BOT_TOKEN")
    result = cli._classify_check_config_failure(state)
    assert result is not None
    assert len(result) == 1
    assert "TELEGRAM_BOT_TOKEN" in result[0]
    # The token-only message should NOT mention ALLOWED_USER_IDS — it'd be
    # misleading.
    assert "ALLOWED_USER_IDS" not in result[0]


def test_classify_users_only(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("ALLOWED_USER_IDS")
    result = cli._classify_check_config_failure(state)
    assert result is not None
    assert len(result) == 1
    assert "ALLOWED_USER_IDS" in result[0]
    assert "at least one" in result[0]
    assert "TELEGRAM_BOT_TOKEN" not in result[0]


def test_classify_both_deferred(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("TELEGRAM_BOT_TOKEN")
    state.mark_deferred("ALLOWED_USER_IDS")
    result = cli._classify_check_config_failure(state)
    assert result is not None
    assert len(result) == 1
    # Both must be mentioned by name so the operator knows the full set.
    assert "TELEGRAM_BOT_TOKEN" in result[0]
    assert "ALLOWED_USER_IDS" in result[0]
    assert "both" in result[0].lower()


# --- non-required deferred should not produce a warning ---------------------


def test_classify_only_group_deferred_returns_none(tmp_path):
    """Group ID deferred → bootstrap mode → --check-config returns 0, so the
    classifier never runs on this state. But if it DID run (e.g., some other
    validation failed), it should return None (treat as unexpected). The
    important thing: it should NOT treat group-only as a deferred-required."""
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("ALLOWED_GROUP_IDS")
    assert cli._classify_check_config_failure(state) is None


def test_classify_only_instances_deferred_returns_none(tmp_path):
    """Instances deferred → still bootstrap-runnable. Not a required field
    from --check-config's perspective."""
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("instances")
    assert cli._classify_check_config_failure(state) is None


def test_classify_mixed_required_and_nonrequired(tmp_path):
    """Token deferred AND group deferred — classifier should still produce a
    warning, and it should be the token-only flavor (group doesn't bump it
    to 'both required missing')."""
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("TELEGRAM_BOT_TOKEN")
    state.mark_deferred("ALLOWED_GROUP_IDS")
    result = cli._classify_check_config_failure(state)
    assert result is not None
    assert "TELEGRAM_BOT_TOKEN" in result[0]
    # Token-only flavor, not the dual-required flavor.
    assert "both" not in result[0].lower()
    assert "ALLOWED_USER_IDS" not in result[0]
