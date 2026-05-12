"""Update flow per spec §2.5.

Stages:
  1. Plan (decide target version, build action list).
  2. Confirm with operator.
  3. Pre-flight (clean git, disk space).
  4. Backup.
  5. Stop service, mark in-flight runs errored.
  6. Checkout, reinstall venv, migrate DB.
  7. Start service, verify.
  8. Auto-rollback if any of 6/7 fail.

The `plan()` function is intentionally pure — it inspects the world and emits
an action list — so update_planner unit tests can hammer it without touching
the filesystem or systemd.
"""

from __future__ import annotations

import datetime
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import version as ver
from .paths import InstallPaths, discover
from .tui import confirm, err, good, info, say, warn

log = logging.getLogger(__name__)


# --- pure planning -----------------------------------------------------------

@dataclass
class UpdatePlan:
    paths: InstallPaths
    current: str
    target: str
    ref: str                  # "v0.2.0" (tag) or branch name
    is_branch: bool
    backup_dir: Path
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_downgrade(self) -> bool:
        try:
            return ver.compare(self.current, self.target) > 0
        except ValueError:
            return False

    @property
    def is_noop(self) -> bool:
        try:
            return ver.compare(self.current, self.target) == 0
        except ValueError:
            return False


def plan(
    paths: InstallPaths,
    *,
    current: str,
    target: str,
    ref: str,
    is_branch: bool,
    now: datetime.datetime | None = None,
) -> UpdatePlan:
    """Build the action list for an update. Side-effect-free."""
    ts = (now or datetime.datetime.utcnow()).strftime("%Y%m%d-%H%M%S")
    backup_dir = paths.backup_dir / f"v{target}-{ts}"

    p = UpdatePlan(
        paths=paths,
        current=current,
        target=target,
        ref=ref,
        is_branch=is_branch,
        backup_dir=backup_dir,
    )

    if p.is_branch:
        p.warnings.append(f"tracking branch {ref!r} — dev mode, not a release.")
    if p.is_downgrade:
        p.warnings.append(
            f"target {target} is older than current {current}; "
            "DB migrations are forward-only."
        )

    p.actions.extend([
        "pre-flight: git status must be clean",
        f"backup install dir → {backup_dir}",
        "stop systemd service",
        "mark in-flight runs as error in DB",
        f"git checkout {ref}",
        "pip install -e . --upgrade",
        "validate config (--check-config)",
        "run DB migrations (--migrate-db)",
        "start systemd service",
        "verify service is active",
    ])
    return p


# --- live runner -------------------------------------------------------------

def run(*, tag: str | None = None, branch: str | None = None) -> int:
    paths = discover()
    if not paths.install_dir.exists():
        err(f"install dir not found: {paths.install_dir}")
        return 1
    if branch:
        ref = branch
        target = branch
        is_branch = True
    elif tag:
        ref = tag
        target = tag.lstrip("v")
        is_branch = False
    else:
        ref, target = _latest_tag(paths.install_dir) or (None, None)
        if ref is None:
            err("no release tags found; pass --tag or --branch.")
            return 1
        is_branch = False

    current = _current_version(paths)
    p = plan(paths, current=current, target=target, ref=ref, is_branch=is_branch)

    info(f"Current version: {p.current}")
    info(f"Target version:  {p.target} ({'branch' if is_branch else 'tag'} {ref})")
    if p.is_noop:
        good("Already up to date.")
        return 0
    if p.is_downgrade and not confirm(
        "This is a downgrade. Continue?", default=False,
    ):
        return 0

    _show_changelog(paths, current=p.current, target_ref=p.ref)
    say()
    say("Planned actions:")
    for a in p.actions:
        say(f"  - {a}")
    for w in p.warnings:
        warn(f"  ! {w}")
    if not confirm("Proceed?", default=True):
        return 0

    return _execute(p)


def _execute(p: UpdatePlan) -> int:
    paths = p.paths

    # 1. pre-flight: git clean
    if not _git_clean(paths.install_dir):
        err("Uncommitted changes in install dir; commit, stash, or discard before updating.")
        return 1

    # 2. backup
    info(f"Creating backup at {p.backup_dir}...")
    try:
        _backup(paths, p.backup_dir)
    except Exception as e:
        err(f"backup failed: {e}")
        return 1

    # 3. stop service + mark in-flight runs
    info("Stopping service...")
    _systemctl(["stop", "mylittleclaude"])
    _mark_runs_errored(paths)

    # 4. checkout
    info(f"Checking out {p.ref}...")
    try:
        _git_checkout(paths.install_dir, p.ref, is_branch=p.is_branch)
    except subprocess.CalledProcessError as e:
        err(f"git checkout failed: {e}")
        return _rollback(paths, p.backup_dir, "checkout failed")

    # 5. pip install
    info("Updating Python deps...")
    rc = _pip_install(paths)
    if rc != 0:
        return _rollback(paths, p.backup_dir, "pip install failed")

    # 6. validate
    info("Validating config...")
    rc = _check_config(paths)
    if rc != 0:
        return _rollback(paths, p.backup_dir, "config validation failed")

    # 7. migrate DB
    info("Running DB migrations...")
    rc = _migrate_db(paths)
    if rc != 0:
        return _rollback(paths, p.backup_dir, "DB migration failed")

    # 8. start service + verify
    info("Starting service...")
    if _systemctl(["start", "mylittleclaude"]) != 0:
        return _rollback(paths, p.backup_dir, "systemctl start failed")
    if not _verify_active(timeout_s=10):
        _show_recent_logs()
        return _rollback(paths, p.backup_dir, "service did not become active")

    good(f"Updated to {p.target}.")
    info(f"Backup retained at {p.backup_dir}.")
    _prune_backups(paths.backup_dir, keep=5)
    return 0


def _rollback(paths: InstallPaths, backup_dir: Path, reason: str) -> int:
    from . import rollback as rb
    err(f"Update failed: {reason}. Rolling back...")
    return rb.restore(paths, backup_dir)


# --- helpers -----------------------------------------------------------------

def _current_version(paths: InstallPaths) -> str:
    return ver.pyproject_version(paths.pyproject)


def _latest_tag(install_dir: Path) -> tuple[str, str] | None:
    """Return (tag, target_version_without_v) or None."""
    try:
        subprocess.run(
            ["git", "fetch", "--tags"], cwd=install_dir,
            capture_output=True, check=False, timeout=60,
        )
        r = subprocess.run(
            ["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"],
            cwd=install_dir, capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None
    tags = [t.strip() for t in r.stdout.splitlines() if t.strip()]
    if not tags:
        return None
    parsed: list[tuple[ver.Version, str]] = []
    for t in tags:
        try:
            parsed.append((ver.Version.parse(t), t))
        except ValueError:
            continue
    if not parsed:
        return None
    parsed.sort()
    _, latest = parsed[-1]
    return latest, latest.lstrip("v")


def _show_changelog(paths: InstallPaths, *, current: str, target_ref: str) -> None:
    cl = paths.install_dir / "CHANGELOG.md"
    if cl.exists():
        say("(CHANGELOG.md present — please review before continuing)")
        return
    # Fallback: git log
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", f"v{current}..{target_ref}"],
            cwd=paths.install_dir, capture_output=True, text=True,
            check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.returncode == 0 and r.stdout.strip():
        say("Commits between current and target:")
        say(r.stdout)


def _git_clean(install_dir: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=install_dir, capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False
    return not r.stdout.strip()


def _git_checkout(install_dir: Path, ref: str, *, is_branch: bool) -> None:
    subprocess.run(
        ["git", "fetch", "--all", "--tags"], cwd=install_dir, check=True, timeout=60,
    )
    subprocess.run(
        ["git", "checkout", ref], cwd=install_dir, check=True, timeout=30,
    )
    if is_branch:
        subprocess.run(
            ["git", "pull", "--ff-only", "origin", ref],
            cwd=install_dir, check=True, timeout=60,
        )


def _backup(paths: InstallPaths, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    # rsync the install dir (excluding venv, data, git history)
    if shutil.which("rsync"):
        subprocess.run(
            [
                "rsync", "-a",
                "--exclude=.venv", "--exclude=data", "--exclude=.git",
                "--exclude=.backup",
                f"{paths.install_dir}/", f"{backup_dir}/",
            ],
            check=True, timeout=300,
        )
    else:
        # Fallback: shutil.copytree with ignore patterns
        shutil.copytree(
            paths.install_dir, backup_dir / "code",
            ignore=shutil.ignore_patterns(".venv", "data", ".git", ".backup"),
            dirs_exist_ok=True,
        )

    for f in (paths.env_file, paths.servers_file):
        if f.exists():
            shutil.copy2(f, backup_dir / f.name)

    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=paths.install_dir,
            capture_output=True, text=True, check=True, timeout=10,
        )
        (backup_dir / "HEAD").write_text(r.stdout.strip() + "\n")
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass


def _prune_backups(backup_root: Path, *, keep: int) -> None:
    if not backup_root.exists():
        return
    entries = [d for d in backup_root.iterdir() if d.is_dir()]
    entries.sort(key=lambda p: p.name, reverse=True)
    for old in entries[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def _mark_runs_errored(paths: InstallPaths) -> None:
    """Best-effort: open the DB and reconcile orphans before the bot does."""
    db_path = paths.data_dir / "mylittleclaude.db"
    if not db_path.exists():
        return
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            import time
            conn.execute(
                "UPDATE runs SET status='error', exit_code=-1, finished_at=? "
                "WHERE status='running'",
                (int(time.time()),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("could not pre-mark in-flight runs: %s", e)


def _pip_install(paths: InstallPaths) -> int:
    pip = paths.venv_dir / "bin" / "pip"
    if not pip.exists():
        err(f"pip not found at {pip}")
        return 1
    try:
        r = subprocess.run(
            [str(pip), "install", "-e", ".", "--upgrade",
             "--upgrade-strategy", "eager"],
            cwd=paths.install_dir, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        err(f"pip install failed: {e}")
        return 1
    return r.returncode


def _check_config(paths: InstallPaths) -> int:
    py = paths.venv_dir / "bin" / "python"
    try:
        r = subprocess.run(
            [str(py), "-m", "mylittleclaude", "--check-config"],
            cwd=paths.install_dir, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        err(f"check-config failed: {e}")
        return 1
    if r.returncode != 0:
        err(r.stderr.strip() or r.stdout.strip())
    return r.returncode


def _migrate_db(paths: InstallPaths) -> int:
    py = paths.venv_dir / "bin" / "python"
    try:
        r = subprocess.run(
            [str(py), "-m", "mylittleclaude", "--migrate-db"],
            cwd=paths.install_dir, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        err(f"migrate-db failed: {e}")
        return 1
    if r.returncode != 0:
        err(r.stderr.strip() or r.stdout.strip())
    return r.returncode


def _systemctl(args: list[str]) -> int:
    try:
        r = subprocess.run(["sudo", "systemctl", *args], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        err(f"systemctl {args}: {e}")
        return 1
    return r.returncode


def _verify_active(*, timeout_s: int) -> bool:
    """Tail logs for `timeout_s`, then check is-active."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "mylittleclaude"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode == 0 and r.stdout.strip() == "active":
            return True
        time.sleep(1)
    return False


def _show_recent_logs() -> None:
    try:
        r = subprocess.run(
            ["journalctl", "-u", "mylittleclaude", "-n", "30", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        if r.stdout:
            say(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
