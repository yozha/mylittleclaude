"""Path discovery for the installer.

The install dir is `$MYLITTLECLAUDE_DIR` (default `$HOME/mylittleclaude`).
We never assume the installer is running *inside* the install dir — the symlink
in ~/.local/bin can be invoked from anywhere — so callers go through here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallPaths:
    install_dir: Path
    env_file: Path
    servers_file: Path
    data_dir: Path
    venv_dir: Path
    backup_dir: Path
    systemd_unit_src: Path
    systemd_unit_dst: Path
    pyproject: Path

    @property
    def exists(self) -> bool:
        return self.install_dir.exists()

    @property
    def configured(self) -> bool:
        return self.env_file.exists() or self.servers_file.exists()


def discover(install_dir: Path | None = None) -> InstallPaths:
    """Resolve install paths. `MYLITTLECLAUDE_DIR` env var wins over the default."""
    if install_dir is None:
        env = os.environ.get("MYLITTLECLAUDE_DIR", "").strip()
        if env:
            install_dir = Path(env).expanduser().resolve()
        else:
            install_dir = (Path.home() / "mylittleclaude").resolve()

    return InstallPaths(
        install_dir=install_dir,
        env_file=install_dir / ".env",
        servers_file=install_dir / "servers.yaml",
        data_dir=_data_dir(install_dir),
        venv_dir=install_dir / ".venv",
        backup_dir=install_dir / ".backup",
        systemd_unit_src=install_dir / "systemd" / "mylittleclaude.service",
        systemd_unit_dst=Path("/etc/systemd/system/mylittleclaude.service"),
        pyproject=install_dir / "pyproject.toml",
    )


def _data_dir(install_dir: Path) -> Path:
    """Honor a DATA_DIR override only if it's set in the current process env.

    The installer doesn't parse .env itself (the bot does); for status display
    we use the default unless something else has already exported DATA_DIR.
    """
    override = os.environ.get("DATA_DIR", "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            p = (install_dir / p).resolve()
        return p
    return install_dir / "data"
