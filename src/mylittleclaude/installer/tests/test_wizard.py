from __future__ import annotations

import builtins
from pathlib import Path
from typing import Iterator

import pytest

from mylittleclaude.installer import steps, validate
from mylittleclaude.installer.paths import InstallPaths
from mylittleclaude.installer.state import InstanceDraft, WizardState
from mylittleclaude.installer.tui import GoBack, Quit, Skip, ask, confirm
from mylittleclaude.installer.wizard import (
    ReviewAborted,
    Step,
    WizardAborted,
    run as run_wizard,
)


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


def _feed(monkeypatch, lines: list[str]) -> None:
    it: Iterator[str] = iter(lines)
    monkeypatch.setattr(builtins, "input", lambda *a, **kw: next(it))


# --- validators --------------------------------------------------------------


def test_token_validator():
    assert validate.token("123456789:" + "A" * 35) is None
    assert validate.token("nope") is not None
    assert validate.token("123:short") is not None


def test_user_id_csv_validator():
    assert validate.user_id_csv("11111111") is None
    assert validate.user_id_csv("11111111,22222222") is None
    assert validate.user_id_csv("") is not None
    assert validate.user_id_csv("foo,11111111") is not None
    assert validate.user_id_csv("-5") is not None


def test_group_id_validator():
    assert validate.group_id("-1001234567890") is None
    assert validate.group_id("0") is None
    assert validate.group_id("not-a-number") is not None


def test_instance_name_validator():
    assert validate.instance_name("sandbox") is None
    assert validate.instance_name("dev-1") is None
    assert validate.instance_name("Cap") is not None
    assert validate.instance_name("-leading-dash") is not None
    assert validate.instance_name("a" * 32) is not None  # too long


def test_remote_host_validator():
    assert validate.remote_host("user@host") is None
    assert validate.remote_host("user@host:2222") is None
    assert validate.remote_host("user@host:notaport") is not None
    assert validate.remote_host("host-only") is not None
    # defense-in-depth: weird chars get rejected even if they match REMOTE_RE
    assert validate.remote_host("user;rm@host") is not None


def test_absolute_path_validator():
    assert validate.absolute_path("/etc/hosts") is None
    assert validate.absolute_path("relative") is not None


def test_log_level_validator():
    assert validate.log_level("INFO") is None
    assert validate.log_level("debug") is None
    assert validate.log_level("yelling") is not None


# --- ask() / control flow ----------------------------------------------------


def test_ask_accepts_value(monkeypatch):
    _feed(monkeypatch, ["hello"])
    assert ask("prompt") == "hello"


def test_ask_back_raises(monkeypatch):
    _feed(monkeypatch, ["back"])
    with pytest.raises(GoBack):
        ask("prompt")


def test_ask_skip_raises(monkeypatch):
    _feed(monkeypatch, ["skip"])
    with pytest.raises(Skip):
        ask("prompt")


def test_ask_quit_raises(monkeypatch):
    _feed(monkeypatch, ["quit"])
    with pytest.raises(Quit):
        ask("prompt")


def test_ask_default_on_enter(monkeypatch):
    from mylittleclaude.installer.tui import AskOptions
    _feed(monkeypatch, [""])
    assert ask("prompt", options=AskOptions(default="d")) == "d"


def test_confirm_yes(monkeypatch):
    _feed(monkeypatch, ["y"])
    assert confirm("ok?") is True


def test_confirm_default(monkeypatch):
    _feed(monkeypatch, [""])
    assert confirm("ok?", default=False) is False


# --- state machine -----------------------------------------------------------


def test_state_machine_runs_in_order(tmp_path):
    seen: list[str] = []

    def make_step(name: str, *, raise_=None):
        def fn(state):
            seen.append(name)
            if raise_:
                raise raise_
        return fn

    state = WizardState(paths=_paths(tmp_path))
    steps_ = [
        Step("a", make_step("a"), skippable=False),
        Step("b", make_step("b"), skippable=False),
        Step("c", make_step("c"), skippable=False),
    ]
    assert run_wizard(steps_, state) is True
    assert seen == ["a", "b", "c"]


def test_state_machine_back_navigation(tmp_path):
    seen: list[str] = []
    calls = {"b": 0}

    def step_a(state):
        seen.append("a")

    def step_b(state):
        calls["b"] += 1
        seen.append(f"b{calls['b']}")
        if calls["b"] == 1:
            raise GoBack()  # bounces back to a

    def step_c(state):
        seen.append("c")

    state = WizardState(paths=_paths(tmp_path))
    steps_ = [
        Step("a", step_a, skippable=False),
        Step("b", step_b, skippable=False),
        Step("c", step_c, skippable=False),
    ]
    assert run_wizard(steps_, state) is True
    # We saw a, b1 (which bounced), then back to a, b2, c.
    assert seen == ["a", "b1", "a", "b2", "c"]


def test_state_machine_skip_marks_deferred(tmp_path):
    def step_token(state):
        raise Skip()

    def step_users(state):
        pass

    state = WizardState(paths=_paths(tmp_path))
    steps_ = [
        Step("token", step_token, skippable=True, deferred_field="TELEGRAM_BOT_TOKEN"),
        Step("users", step_users, skippable=False),
    ]
    assert run_wizard(steps_, state) is True
    assert "TELEGRAM_BOT_TOKEN" in state.deferred


def test_state_machine_quit_propagates(tmp_path):
    def step(state):
        raise Quit()

    state = WizardState(paths=_paths(tmp_path))
    with pytest.raises(WizardAborted):
        run_wizard([Step("q", step, skippable=False)], state)


def test_state_machine_review_aborted_returns_false(tmp_path):
    def step(state):
        raise ReviewAborted()

    state = WizardState(paths=_paths(tmp_path))
    assert run_wizard([Step("r", step, skippable=False)], state) is False


# --- step functions (smoke-style with mocked input) --------------------------


def test_step_user_ids_accepts_csv(monkeypatch, tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    _feed(monkeypatch, ["11111111,22222222"])
    steps.step_user_ids(state)
    assert state.allowed_user_ids == [11111111, 22222222]
    assert "ALLOWED_USER_IDS" not in state.deferred


def test_step_user_ids_skip_marks_deferred(monkeypatch, tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    _feed(monkeypatch, ["skip"])
    with pytest.raises(Skip):
        steps.step_user_ids(state)
    assert state.allowed_user_ids == []
    assert "ALLOWED_USER_IDS" in state.deferred


def test_step_group_id_skip_marks_bootstrap(monkeypatch, tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    _feed(monkeypatch, ["skip"])
    with pytest.raises(Skip):
        steps.step_group_id(state)
    assert state.allowed_group_ids == []
    assert "ALLOWED_GROUP_IDS" in state.deferred


def test_step_review_format(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.bot_token = "123456789:" + "A" * 35
    state.allowed_user_ids = [11111111]
    state.allowed_group_ids = [-1001234567890]
    state.instances = [InstanceDraft(
        name="sandbox", description="x", host="local", workdir="/home/u/x",
    )]
    out = steps._format_review(state)
    assert "TELEGRAM_BOT_TOKEN" in out
    assert "sandbox: local /home/u/x" in out


def test_step_review_format_with_deferred(tmp_path):
    state = WizardState(paths=_paths(tmp_path))
    state.mark_deferred("ALLOWED_GROUP_IDS")
    out = steps._format_review(state)
    assert "(bootstrap mode)" in out
    assert "deferred" in out
    assert "Deferred fields" in out
    assert "ALLOWED_GROUP_IDS" in out
