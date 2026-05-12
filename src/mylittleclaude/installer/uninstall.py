"""Uninstall flow per spec §2.7.

Four separate confirmations so the operator can keep data/config/install dir
independently. Stop + disable + remove systemd unit + remove setup symlink
always happen if the operator confirms step 1.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .paths import discover
from .tui import confirm, err, good, info, say, warn

SETUP_SYMLINK = Path.home() / ".local" / "bin" / "mylittleclaude-setup"


def run() -> int:
    paths = discover()

    if not confirm(
        "Stop the bot, remove the systemd unit, and remove "
        "~/.local/bin/mylittleclaude-setup?",
        default=False,
    ):
        return 0

    delete_data = confirm(
        f"Also delete the data directory at {paths.data_dir} "
        "(run history, archived outputs, logs)?",
        default=False,
    )
    delete_config = confirm(
        f"Also delete .env and servers.yaml at {paths.install_dir} "
        "(will permanently delete your bot token and instance configs)?",
        default=False,
    )
    delete_dir = confirm(
        f"Also delete the install directory at {paths.install_dir} "
        "(the cloned repo and venv)?",
        default=False,
    )

    info("Stopping and disabling service...")
    _sudo(["systemctl", "stop", "mylittleclaude"])
    _sudo(["systemctl", "disable", "mylittleclaude"])

    unit = Path("/etc/systemd/system/mylittleclaude.service")
    if unit.exists():
        info(f"Removing {unit}...")
        _sudo(["rm", "-f", str(unit)])
        _sudo(["systemctl", "daemon-reload"])

    if SETUP_SYMLINK.exists() or SETUP_SYMLINK.is_symlink():
        info(f"Removing {SETUP_SYMLINK}...")
        try:
            SETUP_SYMLINK.unlink()
        except OSError as e:
            warn(f"could not remove symlink: {e}")

    kept: list[str] = []
    if delete_data and paths.data_dir.exists():
        info(f"Removing data dir {paths.data_dir}...")
        shutil.rmtree(paths.data_dir, ignore_errors=True)
    else:
        if paths.data_dir.exists():
            kept.append(f"data dir: {paths.data_dir}")

    if delete_config:
        for f in (paths.env_file, paths.servers_file):
            if f.exists():
                info(f"Removing {f}...")
                try:
                    f.unlink()
                except OSError as e:
                    warn(f"could not remove {f}: {e}")
    else:
        for f in (paths.env_file, paths.servers_file):
            if f.exists():
                kept.append(f"config: {f}")

    if delete_dir:
        # If we're running from inside the install dir, deleting it would
        # pull the rug out from under the running Python. Warn and skip.
        if _running_from(paths.install_dir):
            warn(
                f"refusing to delete {paths.install_dir} while it's our cwd; "
                "rm -rf it manually after this command exits."
            )
        else:
            info(f"Removing install dir {paths.install_dir}...")
            shutil.rmtree(paths.install_dir, ignore_errors=True)
    else:
        if paths.install_dir.exists():
            kept.append(f"install dir: {paths.install_dir}")

    say()
    if kept:
        info("Kept:")
        for k in kept:
            say(f"  - {k}")
    else:
        good("Full uninstall complete.")
    return 0


def _running_from(install_dir: Path) -> bool:
    try:
        cwd = Path.cwd().resolve()
        target = install_dir.resolve()
    except OSError:
        return False
    try:
        cwd.relative_to(target)
        return True
    except ValueError:
        return False


def _sudo(args: list[str]) -> int:
    try:
        return subprocess.run(["sudo", *args], timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        err(f"sudo {args}: {e}")
        return 1
