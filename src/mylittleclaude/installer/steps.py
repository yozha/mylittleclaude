"""The wizard's step functions. One per §2.3 step.

Each step accepts a WizardState, mutates it, and may raise GoBack / Skip /
Quit to signal control flow. Quit propagates up to `cli.main`.

Network calls (Telegram getMe) are optional and best-effort: a 5-second
timeout, no retries; on any failure the operator continues unchallenged.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from . import validate
from .render import coerce_instance_to_dict, render_env, render_servers, write_all
from .state import InstanceDraft, WizardState, default_claude_bin
from .tui import (
    AskOptions,
    GoBack,
    Quit,
    Skip,
    ask,
    block,
    choose,
    confirm,
    err,
    good,
    header,
    info,
    say,
    warn,
)
from .wizard import ReviewAborted

if TYPE_CHECKING:
    pass


STEP_TOTAL = 9  # spec §4 uses "Step N of 9" — keep in sync


# --- step 1: welcome ---------------------------------------------------------

def step_welcome(state: WizardState) -> None:
    header(1, STEP_TOTAL, "Welcome")
    if state.is_reconfigure:
        info("Existing install detected.")
        say(_current_state_summary(state))
    else:
        info("Fresh install — let's get you set up.")
        say("This wizard will collect the bot's config in 8 short steps.")
        say("You can type 'skip' on most steps, 'back' to revisit a step,")
        say("or 'quit' to abort cleanly.")
    say()


def _current_state_summary(state: WizardState) -> str:
    rows = [
        f"  Token configured:   {'yes' if state.has_token else 'no'}",
        f"  Allowed users:      "
        f"{', '.join(str(i) for i in state.allowed_user_ids) or '(none)'}",
        f"  Allowed groups:     "
        f"{', '.join(str(i) for i in state.allowed_group_ids) or '(none — bootstrap mode)'}",
        f"  Instances:          {len(state.instances)}",
        f"  Data dir:           {state.paths.data_dir}",
    ]
    return "\n".join(rows)


# --- step 2: install paths ---------------------------------------------------

def step_paths(state: WizardState) -> None:
    header(2, STEP_TOTAL, "Install paths")
    block(f"""
        Install dir: {state.paths.install_dir}
        Data dir:    {state.paths.data_dir}

        These are read from the environment ($MYLITTLECLAUDE_DIR / $DATA_DIR)
        and from the existing install layout. The wizard does not change them
        — to relocate, exit, set the env vars, and re-run the installer.
    """)
    if not state.paths.install_dir.exists():
        err(f"install dir does not exist: {state.paths.install_dir}")
        raise Quit()
    if not os.access(state.paths.install_dir, os.W_OK):
        err(f"install dir not writable by current user: {state.paths.install_dir}")
        raise Quit()
    state.data_dir = str(state.paths.data_dir)


# --- step 3: bot token -------------------------------------------------------

def step_bot_token(state: WizardState) -> None:
    header(3, STEP_TOTAL, "Telegram bot token")
    block("""
        To create a Telegram bot:

          1. Open Telegram on your phone or desktop.
          2. Search for the user "@BotFather" (the verified one with a
             blue check mark) and start a chat.
          3. Send /newbot
          4. Choose a display name (any string, can be changed later).
          5. Choose a username — must be unique on Telegram and must end
             in "bot". Example: mylittleclaude_jane_bot
          6. BotFather replies with a token. It looks like:
                123456789:AAEhBP3pV7_xK0pZqL3mN8oQrStUvWxYz...
          7. Send /setprivacy to BotFather, pick your new bot, choose
             "Disable" (so the bot can read commands in groups).
          8. Paste the token below.
    """)
    opts = AskOptions(default=state.bot_token if state.has_token else None)
    try:
        token = ask("Bot token", options=opts, validate=validate.token)
    except Skip:
        warn("Skipping token — the bot will refuse to start until you set it.")
        state.bot_token = None
        state.mark_deferred("TELEGRAM_BOT_TOKEN")
        raise

    state.bot_token = token
    state.unmark_deferred("TELEGRAM_BOT_TOKEN")

    # Best-effort getMe to confirm the token works and show the bot's name.
    name = _telegram_get_me(token)
    if name is not None:
        good(f"  ✓ Verified — bot username is @{name}")
    else:
        info("  (could not verify with Telegram API; that's ok)")


def _telegram_get_me(token: str) -> str | None:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not data.get("ok"):
        return None
    return data.get("result", {}).get("username")


# --- step 4: user IDs --------------------------------------------------------

def step_user_ids(state: WizardState) -> None:
    header(4, STEP_TOTAL, "Your Telegram user ID")
    block("""
        The bot needs to know your Telegram user ID so it can ignore
        messages from anyone else.

        To find your ID:

          1. Open Telegram.
          2. Search for "@userinfobot" and start a chat.
          3. Send /start (or any message).
          4. It replies with a block of info. The first line is your
             ID — a number, usually 9 or 10 digits.
          5. Paste it below.

        Multiple IDs (advanced): separate with commas, e.g. 11111111,22222222
    """)
    default = (
        ",".join(str(i) for i in state.allowed_user_ids)
        if state.has_users else None
    )
    try:
        raw = ask(
            "Your Telegram user ID",
            options=AskOptions(default=default),
            validate=validate.user_id_csv,
        )
    except Skip:
        warn("Skipping — the bot will not respond to anyone until you set this.")
        state.allowed_user_ids = []
        state.mark_deferred("ALLOWED_USER_IDS")
        raise

    state.allowed_user_ids = validate.parse_user_id_csv(raw)
    state.unmark_deferred("ALLOWED_USER_IDS")


# --- step 5+6: instances -----------------------------------------------------

def step_first_instance(state: WizardState) -> None:
    header(5, STEP_TOTAL, "First instance")
    if state.is_reconfigure and state.has_instances:
        info(f"Current instances: {', '.join(i.name for i in state.instances)}")
        if confirm("Keep existing instances and skip this step?", default=True):
            return

    block("""
        An "instance" is a (host, workdir) pair where Claude Code will
        be run. The most common setup is a single instance on the same
        machine (host: local). You can add more later by editing
        servers.yaml directly or re-running this wizard.
    """)

    inst = _collect_instance(state, default_name="sandbox")
    if state.is_reconfigure and state.has_instances:
        state.instances = [inst] + [i for i in state.instances if i.name != inst.name]
    else:
        state.instances = [inst]


def step_more_instances(state: WizardState) -> None:
    header(6, STEP_TOTAL, "Additional instances")
    if not state.has_instances:
        # Operator skipped step 5; allow them to add now.
        info("No instances configured yet.")
    while confirm("Add another instance?", default=False):
        existing_names = {i.name for i in state.instances}
        inst = _collect_instance(state, default_name=None)
        if inst.name in existing_names:
            warn(f"Replacing existing instance {inst.name!r}.")
            state.instances = [i for i in state.instances if i.name != inst.name]
        state.instances.append(inst)


def _collect_instance(state: WizardState, *, default_name: str | None) -> InstanceDraft:
    name = ask(
        "Instance name (e.g. sandbox)",
        options=AskOptions(default=default_name, allow_skip=False),
        validate=validate.instance_name,
    )
    desc = ask(
        "Description (one line)",
        options=AskOptions(default=f"Claude on {name}", allow_skip=False),
        validate=None,
    )
    kind = choose(
        "Where does Claude run for this instance?",
        choices=[("local", "Local subprocess on this machine"),
                 ("remote", "Remote machine via SSH")],
        default="local",
    )
    if kind == "local":
        host = "local"
        ssh_key: str | None = None
    else:
        host = ask(
            "Remote host (user@host[:port])",
            options=AskOptions(allow_skip=False),
            validate=validate.remote_host,
        )
        ssh_key = _collect_ssh_key()

    workdir = ask(
        "Workdir (absolute path on the target host)",
        options=AskOptions(
            default=f"/home/{os.environ.get('USER', 'claude')}/projects/{name}",
            allow_skip=False,
        ),
        validate=validate.absolute_path,
    )
    if kind == "local":
        _maybe_mkdir(Path(workdir))

    return InstanceDraft(
        name=name,
        description=desc,
        host=host,
        workdir=workdir,
        ssh_key=ssh_key,
    )


def _collect_ssh_key() -> str:
    while True:
        path = ask(
            "SSH key path (absolute, on this controller)",
            options=AskOptions(allow_skip=False),
            validate=validate.ssh_key_path,
        )
        mode = Path(path).stat().st_mode & 0o777
        if mode in (0o600, 0o400):
            return path
        warn(f"  key {path} has mode {oct(mode)}; expected 600 or 400.")
        if confirm("Fix it now (chmod 600)?", default=True):
            try:
                os.chmod(path, 0o600)
                good("  ✓ chmod 600 applied.")
                return path
            except OSError as e:
                err(f"  ✗ chmod failed: {e}")
                continue
        warn("  Continuing with current permissions; SSH may reject this key.")
        return path


def _maybe_mkdir(p: Path) -> None:
    if p.exists():
        return
    if not confirm(f"Workdir {p} doesn't exist. Create it (mkdir -p, mode 750)?", default=True):
        warn("  Skipping mkdir; create it before using this instance.")
        return
    try:
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o750)
        good(f"  ✓ created {p}")
    except OSError as e:
        err(f"  ✗ mkdir failed: {e}")


# --- step 7: group ID --------------------------------------------------------

def step_group_id(state: WizardState) -> None:
    header(7, STEP_TOTAL, "Telegram group ID (optional)")
    block("""
        The bot lives in a Telegram supergroup with topics enabled.
        Each topic is one Claude conversation.

        If you haven't created the group yet, you can skip this step
        and the bot will install in "bootstrap mode" — only the
        /chatid command works, and only from your user ID. After
        installing:

          1. Create a new Telegram group.
          2. Add your bot (search for the username you chose with
             BotFather) and promote it to admin with the "Manage
             Topics" permission enabled.
          3. Open the group's info, edit it, and enable Topics. Confirm
             the supergroup conversion dialog.
          4. In the group's General topic, send /chatid
          5. The bot replies with the group ID (a large negative
             number, starts with -100).
          6. Run "mylittleclaude-setup" again and paste it here.
    """)
    default = (
        ",".join(str(i) for i in state.allowed_group_ids)
        if state.has_groups else None
    )
    try:
        raw = ask(
            "Group ID",
            options=AskOptions(default=default),
            validate=_group_id_csv,
        )
    except Skip:
        info("Skipping — bot will start in bootstrap mode.")
        state.allowed_group_ids = []
        state.mark_deferred("ALLOWED_GROUP_IDS")
        raise

    state.allowed_group_ids = [
        int(p.strip()) for p in raw.split(",") if p.strip()
    ]
    state.unmark_deferred("ALLOWED_GROUP_IDS")


def _group_id_csv(s: str) -> str | None:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return "at least one group ID is required"
    for p in parts:
        e = validate.group_id(p)
        if e is not None:
            return f"in {p!r}: {e}"
    return None


# --- step 8: advanced --------------------------------------------------------

def step_advanced(state: WizardState) -> None:
    header(8, STEP_TOTAL, "Advanced options")
    if not confirm("Configure CLAUDE_BIN / LOG_LEVEL overrides?", default=False):
        return

    auto_bin = default_claude_bin()
    cb = ask(
        "Path to the claude binary",
        options=AskOptions(default=state.claude_bin or auto_bin),
        validate=None,
    )
    state.claude_bin = cb if cb != auto_bin else None

    ll = ask(
        "Log level",
        options=AskOptions(default=state.log_level or "INFO"),
        validate=validate.log_level,
    )
    state.log_level = ll.upper()


# --- step 9: review + write --------------------------------------------------

def step_review(state: WizardState) -> None:
    header(9, STEP_TOTAL, "Review")
    block(_format_review(state))
    if not confirm("Proceed?", default=True):
        raise ReviewAborted()
    info("Writing config files...")
    result = write_all(state)
    for b in result.backups:
        info(f"  backed up: {b}")
    good(f"  ✓ wrote {result.env_written}")
    good(f"  ✓ wrote {result.servers_written}")


def _format_review(state: WizardState) -> str:
    def mark(v: object, *, ok: bool, hint: str = "") -> str:
        if ok:
            return f"✓ {v}"
        return f"✗ deferred {hint}".rstrip()

    token_disp = "set (" + (state.bot_token[:13] + "…") + ")" if state.has_token else "(deferred)"
    users_disp = ", ".join(str(i) for i in state.allowed_user_ids) or "(deferred)"
    groups_disp = (
        ", ".join(str(i) for i in state.allowed_group_ids)
        if state.has_groups else "(deferred — bootstrap mode)"
    )

    lines = [
        ".env:",
        f"  TELEGRAM_BOT_TOKEN     {mark(token_disp, ok=state.has_token)}",
        f"  ALLOWED_USER_IDS       {mark(users_disp, ok=state.has_users)}",
        f"  ALLOWED_GROUP_IDS      {mark(groups_disp, ok=state.has_groups, hint='(bootstrap mode)')}",
        f"  CLAUDE_BIN             {state.claude_bin or '(default)'}",
        f"  DATA_DIR               {state.data_dir or state.paths.data_dir}",
        f"  LOG_LEVEL              {state.log_level}",
        "",
        "servers.yaml:",
    ]
    if not state.instances:
        lines.append("  (no instances configured)")
    else:
        lines.append("  instances:")
        for inst in state.instances:
            host_part = inst.host if inst.host != "local" else "local"
            lines.append(f"    {inst.name}: {host_part} {inst.workdir}")
    if state.deferred:
        lines.append("")
        lines.append("Deferred fields (you'll need to set these later via mylittleclaude-setup):")
        for f in sorted(state.deferred):
            lines.append(f"  - {f}")
        # Required fields trigger an explicit "bot will refuse to start"
        # warning so the operator isn't surprised when systemctl reports the
        # service as inactive after install. Group ID + instances are not
        # required (bootstrap mode handles those), so they don't trigger this.
        required_missing = state.deferred & {"TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"}
        if required_missing:
            lines.append("")
            if required_missing == {"TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"}:
                lines.append(
                    "⚠ Bot will not start until TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS are set."
                )
            elif "TELEGRAM_BOT_TOKEN" in required_missing:
                lines.append("⚠ Bot will not start until TELEGRAM_BOT_TOKEN is set.")
            else:
                lines.append(
                    "⚠ Bot will not start until at least one ALLOWED_USER_IDS entry is set."
                )
    return "\n".join(lines)


# --- bundle ------------------------------------------------------------------

def all_steps() -> list:
    """Return the Step list for the fresh-install / full-reconfigure flow."""
    from .wizard import Step
    return [
        Step("Welcome", step_welcome, skippable=False),
        Step("Install paths", step_paths, skippable=False),
        Step("Bot token", step_bot_token,
             skippable=True, deferred_field="TELEGRAM_BOT_TOKEN"),
        Step("User IDs", step_user_ids,
             skippable=True, deferred_field="ALLOWED_USER_IDS"),
        Step("First instance", step_first_instance,
             skippable=True, deferred_field="instances"),
        Step("More instances", step_more_instances, skippable=False),
        Step("Group ID", step_group_id,
             skippable=True, deferred_field="ALLOWED_GROUP_IDS"),
        Step("Advanced options", step_advanced, skippable=False),
        Step("Review", step_review, skippable=False),
    ]
