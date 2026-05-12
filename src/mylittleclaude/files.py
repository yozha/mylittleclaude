from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from pathlib import Path

from .models import InstanceConfig

log = logging.getLogger(__name__)

MAX_FILENAME = 100
SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_TG_BYTES = 50 * 1024 * 1024  # 50 MB Telegram bot upload cap


class FileError(Exception):
    pass


def sanitize_filename(name: str) -> str:
    base = os.path.basename(name) or "file"
    cleaned = SAFE_CHAR_RE.sub("_", base)
    cleaned = cleaned.lstrip(".") or "file"
    return cleaned[:MAX_FILENAME]


def inbox_name(original: str) -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{ts}_{sanitize_filename(original)}"


async def save_local_inbox(
    instance: InstanceConfig, src: Path, original_name: str
) -> str:
    """Move `src` into <workdir>/_inbox/<ts>_<name>. Returns final filename."""
    inbox = Path(instance.workdir) / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(inbox, 0o750)
    except OSError:
        pass
    final = inbox_name(original_name)
    dest = inbox / final
    await asyncio.to_thread(_move_with_fallback, src, dest)
    try:
        os.chmod(dest, 0o640)
    except OSError:
        pass
    return final


def _move_with_fallback(src: Path, dest: Path) -> None:
    try:
        os.replace(src, dest)
    except OSError:
        # Cross-device — copy then unlink.
        import shutil
        shutil.copy2(src, dest)
        try:
            os.unlink(src)
        except OSError:
            pass


async def scp_to_remote(
    instance: InstanceConfig, src: Path, original_name: str
) -> str:
    """scp `src` to <host>:<workdir>/_inbox/<ts>_<name>. Returns final filename."""
    assert not instance.is_local
    assert instance.ssh_key
    final = inbox_name(original_name)
    remote_dir = f"{instance.workdir}/_inbox"
    # Ensure the directory exists via ssh, then scp the file.
    mkdir_argv = [
        "ssh",
        "-i", instance.ssh_key,
        "-o", "StrictHostKeyChecking=accept-new",
        instance.host,
        f"mkdir -p {shlex.quote(remote_dir)} && chmod 750 {shlex.quote(remote_dir)}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *mkdir_argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise FileError(f"remote mkdir failed: {err.decode(errors='replace')[:200]}")
    scp_argv = [
        "scp",
        "-i", instance.ssh_key,
        "-o", "StrictHostKeyChecking=accept-new",
        "-q",
        str(src),
        f"{instance.host}:{remote_dir}/{final}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *scp_argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise FileError(f"scp failed: {err.decode(errors='replace')[:200]}")
    return final


def resolve_get_path(instance: InstanceConfig, rel_path: str) -> Path:
    """Resolve <workdir>/<rel_path>, rejecting traversal. Local only.

    For remote instances, the caller must scp the file down first; this
    function assumes a local filesystem path.
    """
    if rel_path.startswith("/"):
        raise FileError("absolute paths not allowed")
    if ".." in rel_path.split("/"):
        raise FileError("path traversal not allowed")
    workdir = Path(instance.workdir).resolve()
    candidate = (workdir / rel_path).resolve()
    try:
        candidate.relative_to(workdir)
    except ValueError as e:
        raise FileError("path escapes workdir") from e
    if not candidate.exists():
        raise FileError(f"file not found: {rel_path}")
    if not candidate.is_file():
        raise FileError(f"not a regular file: {rel_path}")
    return candidate


async def scp_from_remote(
    instance: InstanceConfig, rel_path: str, dest: Path
) -> None:
    """scp <host>:<workdir>/<rel_path> down to `dest`."""
    assert not instance.is_local
    assert instance.ssh_key
    if rel_path.startswith("/") or ".." in rel_path.split("/"):
        raise FileError("invalid path")
    remote_path = f"{instance.workdir}/{rel_path}"
    argv = [
        "scp",
        "-i", instance.ssh_key,
        "-o", "StrictHostKeyChecking=accept-new",
        "-q",
        f"{instance.host}:{remote_path}",
        str(dest),
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise FileError(f"scp from remote failed: {err.decode(errors='replace')[:200]}")
