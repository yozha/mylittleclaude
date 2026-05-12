from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
from dataclasses import dataclass
from typing import AsyncIterator

from .models import InstanceConfig

log = logging.getLogger(__name__)


CLAUDE_FLAGS = [
    "-p",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
    "--dangerously-skip-permissions",
]


@dataclass
class RunnerProcess:
    proc: asyncio.subprocess.Process

    async def stdin_write_and_close(self, prompt: str) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(prompt.encode("utf-8"))
            await self.proc.stdin.drain()
        finally:
            try:
                self.proc.stdin.close()
            except Exception:
                pass

    async def stdout_lines(self) -> AsyncIterator[bytes]:
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            yield line

    async def stderr_bytes(self) -> bytes:
        assert self.proc.stderr is not None
        return await self.proc.stderr.read()

    async def wait(self) -> int:
        return await self.proc.wait()

    def terminate_group(self) -> None:
        """SIGTERM the process group. Caller is responsible for SIGKILL fallback."""
        try:
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            log.warning("terminate_group failed: %s", e)

    def kill_group(self) -> None:
        try:
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as e:
            log.warning("kill_group failed: %s", e)


def _local_argv(claude_bin: str, session_id: str | None) -> list[str]:
    argv = [claude_bin, *CLAUDE_FLAGS]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def _remote_argv(
    instance: InstanceConfig, claude_bin: str, session_id: str | None
) -> list[str]:
    quoted_wd = shlex.quote(instance.workdir)
    quoted_bin = shlex.quote(claude_bin)
    remote_cmd_parts = [
        quoted_bin,
        "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if session_id:
        remote_cmd_parts += ["--resume", shlex.quote(session_id)]
    remote_cmd = f"cd {quoted_wd} && exec {' '.join(remote_cmd_parts)}"
    assert instance.ssh_key
    argv = [
        "ssh",
        "-i", instance.ssh_key,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
        "-T",
        instance.host,
        "bash", "-lc", remote_cmd,
    ]
    return argv


async def spawn(
    instance: InstanceConfig,
    *,
    claude_bin: str,
    session_id: str | None,
) -> RunnerProcess:
    if instance.is_local:
        argv = _local_argv(claude_bin, session_id)
        cwd = instance.workdir
    else:
        argv = _remote_argv(instance, claude_bin, session_id)
        cwd = None

    log.info(
        "spawn instance=%s host=%s resume=%s",
        instance.workdir, instance.host, "yes" if session_id else "no",
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        # New process group so we can SIGTERM the whole tree.
        start_new_session=True,
    )
    return RunnerProcess(proc=proc)
