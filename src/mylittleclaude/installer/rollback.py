"""Rollback flow per spec §2.6.

Lists backups under `.backup/`, lets the operator pick one, restores it.
Auto-rollback during a failed update calls `restore()` directly with the
backup dir built earlier.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .paths import InstallPaths, discover
from .tui import confirm, err, good, info, say, warn


def run() -> int:
    paths = discover()
    backups = _list_backups(paths.backup_dir)
    if not backups:
        warn("No backups found.")
        return 1

    say("Available backups (newest first):")
    for i, b in enumerate(backups, 1):
        say(f"  {i}. {b.name}")
    say()

    choice = input("Pick a number (or 'quit'): ").strip()
    if choice.lower() in ("q", "quit", ""):
        return 0
    try:
        idx = int(choice) - 1
        target = backups[idx]
    except (ValueError, IndexError):
        err("invalid choice")
        return 1

    warn("Rollback will stop the bot, restore files, and restart it.")
    warn("In-flight prompts will be killed. DB migrations are forward-only:")
    warn("if the older version expects an older schema, the bot may fail to start.")
    if not confirm("Proceed?", default=False):
        return 0

    return restore(paths, target)


def restore(paths: InstallPaths, backup_dir: Path) -> int:
    """Restore from `backup_dir`. Used both by the manual `rollback` command
    and by the update flow's auto-rollback."""
    if not backup_dir.exists():
        err(f"backup dir not found: {backup_dir}")
        return 1

    info(f"Restoring from {backup_dir}...")

    # 1. stop service
    _systemctl(["stop", "mylittleclaude"])

    # 2. rsync code back
    try:
        if shutil.which("rsync"):
            subprocess.run(
                [
                    "rsync", "-a", "--delete",
                    "--exclude=.venv", "--exclude=data", "--exclude=.git",
                    "--exclude=.backup",
                    f"{backup_dir}/", f"{paths.install_dir}/",
                ],
                check=True, timeout=300,
            )
        else:
            # Crude fallback
            for entry in backup_dir.iterdir():
                if entry.name in (".venv", "data", ".git", ".backup"):
                    continue
                if entry.is_file():
                    shutil.copy2(entry, paths.install_dir / entry.name)
                elif entry.is_dir():
                    dst = paths.install_dir / entry.name
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(entry, dst)
    except Exception as e:  # noqa: BLE001
        err(f"file restore failed: {e}")
        return 1

    # 3. restore config files
    for name in (".env", "servers.yaml"):
        src = backup_dir / name
        if src.exists():
            shutil.copy2(src, paths.install_dir / name)

    # 4. restore git HEAD if recorded
    head_file = backup_dir / "HEAD"
    if head_file.exists():
        try:
            head = head_file.read_text().strip()
            subprocess.run(
                ["git", "reset", "--hard", head],
                cwd=paths.install_dir, check=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            warn(f"git reset failed (continuing anyway): {e}")

    # 5. reinstall venv
    info("Reinstalling Python deps...")
    pip = paths.venv_dir / "bin" / "pip"
    if pip.exists():
        try:
            subprocess.run(
                [str(pip), "install", "-e", ".", "--force-reinstall"],
                cwd=paths.install_dir, timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            warn(f"pip force-reinstall failed: {e}")

    # 6. validate + start
    py = paths.venv_dir / "bin" / "python"
    rc = subprocess.run(
        [str(py), "-m", "mylittleclaude", "--check-config"],
        cwd=paths.install_dir, capture_output=True, text=True, timeout=15,
    ).returncode
    if rc != 0:
        warn("config validation failed after rollback; service not restarted.")
        return 1

    _systemctl(["start", "mylittleclaude"])
    good("Rollback complete.")
    return 0


def _list_backups(backup_root: Path) -> list[Path]:
    if not backup_root.exists():
        return []
    entries = [d for d in backup_root.iterdir() if d.is_dir()]
    entries.sort(key=lambda p: p.name, reverse=True)
    return entries


def _systemctl(args: list[str]) -> int:
    try:
        return subprocess.run(["sudo", "systemctl", *args], timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1
