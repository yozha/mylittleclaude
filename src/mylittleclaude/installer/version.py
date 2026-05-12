"""Version detection and semver comparison for the update flow.

Pulls the installed package version via importlib.metadata; falls back to
parsing pyproject.toml when running uninstalled (e.g., during tests). Supports
pre-release tags like ``v0.2.0-rc1`` for ordering only — they sort before the
clean release per PEP 440-ish semantics, with simpler rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:[-.](?P<pre>[A-Za-z0-9.]+))?$"
)


@dataclass(frozen=True, order=True)
class Version:
    """Semver-ish version. Pre-releases sort *before* the matching release."""

    # Order: (major, minor, patch, pre_rank, pre) — dataclass(order=True) uses
    # field order, so layout matters.
    major: int
    minor: int
    patch: int
    pre_rank: int  # 0 for pre-release, 1 for release — release > pre
    pre: str = ""

    @classmethod
    def parse(cls, raw: str) -> "Version":
        m = _SEMVER_RE.match(raw.strip())
        if not m:
            raise ValueError(f"not a semver string: {raw!r}")
        pre = m.group("pre") or ""
        return cls(
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            pre_rank=0 if pre else 1,
            pre=pre,
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.pre}" if self.pre else base

    def tag(self) -> str:
        return f"v{self}"


def installed_version() -> str:
    """Return the bot's version. Falls back to pyproject.toml when uninstalled."""
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
        try:
            return _v("mylittleclaude")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    return pyproject_version()


def pyproject_version(path: Path | None = None) -> str:
    """Read version from pyproject.toml. Returns '0.0.0' if absent."""
    p = path or _default_pyproject_path()
    if not p.exists():
        return "0.0.0"
    try:
        import tomllib  # py 3.11+
    except ImportError:  # pragma: no cover
        return "0.0.0"
    try:
        data = tomllib.loads(p.read_text())
    except Exception:
        return "0.0.0"
    return str(data.get("project", {}).get("version", "0.0.0"))


def _default_pyproject_path() -> Path:
    # repo root is two parents up from this file (src/mylittleclaude/installer)
    return Path(__file__).resolve().parents[3] / "pyproject.toml"


def compare(a: str, b: str) -> int:
    """Return -1/0/1 for a<b / a==b / a>b. Raises ValueError on bad input."""
    va = Version.parse(a)
    vb = Version.parse(b)
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0
