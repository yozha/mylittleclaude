"""`mylittleclaude-setup` console script entrypoint.

Subcommand dispatch only — actual logic lives in the per-command modules. The
top-level error handler writes a traceback log to /tmp/ and prints the path,
per spec §7's "no bare traceback to the operator" rule.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import traceback
from pathlib import Path

from . import status, uninstall, update, version
from .paths import discover
from .state import load_existing
from .steps import all_steps
from .tui import err, good, info, say, warn
from .wizard import WizardAborted, run as run_wizard


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mylittleclaude-setup",
        description="Configure, update, or uninstall mylittleclaude.",
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("reconfigure", help="Re-run the configuration wizard.")
    sub.add_parser("status", help="Print install + service status.")

    up = sub.add_parser("update", help="Update to a new version.")
    up.add_argument("--tag", help="Update to a specific tag (e.g. v0.3.0).")
    up.add_argument("--branch", help="Track a branch instead of tags (dev).")

    sub.add_parser("rollback", help="Restore a previous version from backup.")
    sub.add_parser("uninstall", help="Remove the bot and its systemd unit.")
    sub.add_parser("logs", help="Tail the service journal (journalctl -f).")
    sub.add_parser("version", help="Print installer + bot + spec version.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Logging at WARNING by default — we use stdout for the TUI, not log lines.
    logging.basicConfig(
        level=os.environ.get("MYLITTLECLAUDE_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        say()
        warn("Interrupted.")
        return 130
    except WizardAborted:
        warn("Aborted by operator.")
        return 1
    except Exception:  # noqa: BLE001 — top-level catchall
        log_path = _dump_traceback()
        err(f"unexpected error — full traceback at {log_path}")
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.cmd or "reconfigure"

    if cmd in ("reconfigure",):
        return _cmd_reconfigure()
    if cmd == "status":
        return status.run()
    if cmd == "update":
        return update.run(tag=args.tag, branch=args.branch)
    if cmd == "rollback":
        from . import rollback
        return rollback.run()
    if cmd == "uninstall":
        return uninstall.run()
    if cmd == "logs":
        return _cmd_logs()
    if cmd == "version":
        return _cmd_version()

    err(f"unknown command: {cmd}")
    return 2


def _cmd_reconfigure() -> int:
    paths = discover()
    if not paths.install_dir.exists():
        err(f"install dir not found: {paths.install_dir}")
        say("Set $MYLITTLECLAUDE_DIR or re-run the installer.")
        return 1

    state = load_existing(paths)
    info(f"Install dir: {paths.install_dir}")

    completed = run_wizard(all_steps(), state)
    if not completed:
        warn("No changes written.")
        return 0

    # Validate the freshly-written config by running --check-config.
    rc = _run_check_config(paths)
    if rc != 0:
        err("Config validation failed. Inspect the backup files alongside .env/servers.yaml.")
        return rc

    good("Configuration written and validated.")
    _print_post_install_summary(state, paths)
    return 0


def _run_check_config(paths) -> int:
    import subprocess
    py = paths.venv_dir / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    try:
        proc = subprocess.run(
            [str(py), "-m", "mylittleclaude", "--check-config"],
            cwd=paths.install_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        err(f"could not run --check-config: {e}")
        return 1
    if proc.returncode != 0:
        err(proc.stderr.strip() or proc.stdout.strip())
        return proc.returncode
    say(proc.stdout.strip())
    return 0


def _print_post_install_summary(state, paths) -> None:
    say()
    info("Next steps:")
    if state.deferred:
        for fld in sorted(state.deferred):
            say(f"  • {fld}: re-run `mylittleclaude-setup` once you have the value.")
    else:
        say("  • All required fields are set.")
    say(f"  • Tail logs: journalctl -u mylittleclaude -f")
    say(f"  • Service state: systemctl is-active mylittleclaude")
    say(f"  • Config files: {paths.env_file}, {paths.servers_file}")


def _cmd_logs() -> int:
    import subprocess
    try:
        return subprocess.call(["journalctl", "-u", "mylittleclaude", "-f"])
    except FileNotFoundError:
        err("journalctl not found (is this a systemd system?)")
        return 1


def _cmd_version() -> int:
    say(f"mylittleclaude {version.installed_version()}")
    say("installer: bundled with the bot")
    say("spec: INSTALLERS_SPEC.md (priv, not redistributed)")
    return 0


def _dump_traceback() -> Path:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path(f"/tmp/mylittleclaude-install-{ts}.log")
    try:
        with path.open("w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
    except OSError:
        # If /tmp is unwritable, dump to stderr and return a sentinel path.
        traceback.print_exc()
        return Path("/dev/stderr")
    return path
