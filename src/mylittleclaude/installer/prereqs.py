"""Distro detection + system-package prereq detection.

The bash entrypoint installs prereqs (it has sudo); this module owns the
decision logic so it can be unit-tested without invoking apt/dnf. Distro
detection reads /etc/os-release and slots each system into a known family or
returns "unknown".
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Family = Literal["debian", "rhel", "unknown"]


@dataclass(frozen=True)
class Distro:
    id: str             # raw ID= from os-release ("ubuntu", "debian", "fedora", …)
    id_like: tuple[str, ...]  # tuple from ID_LIKE= split on whitespace
    pretty_name: str
    family: Family
    version_id: str

    @property
    def package_manager(self) -> str | None:
        if self.family == "debian":
            return "apt-get"
        if self.family == "rhel":
            return "dnf" if shutil.which("dnf") else "yum"
        return None


@dataclass(frozen=True)
class Prereq:
    """A single required system tool or package.

    `check_cmd` is what we run to detect presence (must exit 0 if present).
    `apt_pkg` / `dnf_pkg` are the corresponding package names; if either is
    absent, the prereq can't be installed automatically on that family and the
    operator gets a manual-install note.
    """
    name: str
    check_cmd: list[str]
    apt_pkg: str | None
    dnf_pkg: str | None
    rationale: str

    def is_installed(self) -> bool:
        try:
            r = subprocess.run(
                self.check_cmd,
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


# The full list of prereqs the installer needs on the controller VPS.
PREREQS: list[Prereq] = [
    Prereq(
        name="python3.11+",
        check_cmd=["python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"],
        apt_pkg="python3",  # 24.04 has 3.12 by default
        dnf_pkg="python3.11",
        rationale="bot runs on 3.11+",
    ),
    Prereq(
        name="python3-venv",
        # `import ensurepip` is the precise probe. v0.2.0 used `import venv`
        # (false positive — venv is in stdlib). v0.2.1 used `-m venv --help`
        # (still a false positive — --help doesn't exercise the bootstrap
        # path). Ensurepip is what Debian splits into python<X.Y>-venv; if it
        # imports, `python -m venv .venv` will succeed.
        check_cmd=["python3", "-c", "import ensurepip"],
        # Placeholder. `install_commands()` resolves this dynamically because
        # the Debian package is named after the running Python's minor version
        # (e.g. python3.14-venv on Ubuntu 26.04, python3.12-venv on 24.04).
        apt_pkg="python3-venv",
        dnf_pkg=None,  # bundled with python3 on RHEL family
        rationale="ensurepip must be importable (it's the venv bootstrap backend)",
    ),
    Prereq(
        name="git",
        check_cmd=["git", "--version"],
        apt_pkg="git",
        dnf_pkg="git",
        rationale="needed for update/rollback flow",
    ),
    Prereq(
        name="curl",
        check_cmd=["curl", "--version"],
        apt_pkg="curl",
        dnf_pkg="curl",
        rationale="needed by bootstrap.sh",
    ),
    Prereq(
        name="ca-certificates",
        check_cmd=["bash", "-c", "test -e /etc/ssl/certs/ca-certificates.crt || test -d /etc/pki/tls/certs"],
        apt_pkg="ca-certificates",
        dnf_pkg="ca-certificates",
        rationale="needed for HTTPS to Telegram / GitHub",
    ),
    Prereq(
        name="nodejs",
        check_cmd=["bash", "-c", "node --version | grep -Eq '^v(2[0-9]|[3-9][0-9])\\.'"],
        apt_pkg="nodejs",
        dnf_pkg="nodejs",
        rationale="Claude Code is an npm package — needs node >= 20",
    ),
    Prereq(
        name="npm",
        check_cmd=["npm", "--version"],
        apt_pkg="npm",
        dnf_pkg="npm",
        rationale="installs @anthropic-ai/claude-code",
    ),
    Prereq(
        name="rsync",
        check_cmd=["rsync", "--version"],
        apt_pkg="rsync",
        dnf_pkg="rsync",
        rationale="used by the update/backup flow",
    ),
    Prereq(
        name="openssh-client",
        check_cmd=["ssh", "-V"],
        apt_pkg="openssh-client",
        dnf_pkg="openssh-clients",
        rationale="remote workers are reached via ssh",
    ),
    Prereq(
        name="systemctl",
        check_cmd=["systemctl", "--version"],
        apt_pkg=None,  # always present on systemd systems
        dnf_pkg=None,
        rationale="bot runs as a systemd service",
    ),
]


def detect_distro(os_release_path: Path | None = None) -> Distro:
    """Parse /etc/os-release into a Distro record. Defaults to 'unknown'."""
    p = os_release_path or Path("/etc/os-release")
    fields: dict[str, str] = {}
    if p.exists():
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            fields[k.strip()] = v

    id_ = (fields.get("ID") or "").lower()
    like_raw = (fields.get("ID_LIKE") or "").lower()
    id_like = tuple(re.split(r"\s+", like_raw)) if like_raw else ()
    pretty = fields.get("PRETTY_NAME") or id_ or "unknown"
    version_id = fields.get("VERSION_ID") or ""

    family: Family
    debian_keys = {"debian", "ubuntu", "linuxmint", "pop", "raspbian"}
    rhel_keys = {"rhel", "fedora", "rocky", "almalinux", "centos", "ol"}
    if id_ in debian_keys or any(k in debian_keys for k in id_like):
        family = "debian"
    elif id_ in rhel_keys or any(k in rhel_keys for k in id_like):
        family = "rhel"
    else:
        family = "unknown"

    return Distro(
        id=id_,
        id_like=id_like,
        pretty_name=pretty,
        family=family,
        version_id=version_id,
    )


@dataclass
class PrereqReport:
    distro: Distro
    missing: list[Prereq]
    present: list[Prereq]
    unsupported: list[Prereq]  # missing AND no package available on this family

    @property
    def all_satisfied(self) -> bool:
        return not self.missing and not self.unsupported


def audit(distro: Distro | None = None) -> PrereqReport:
    """Run each prereq's check_cmd; return what's missing vs. installed."""
    d = distro or detect_distro()
    missing: list[Prereq] = []
    present: list[Prereq] = []
    unsupported: list[Prereq] = []

    for req in PREREQS:
        if req.is_installed():
            present.append(req)
            continue
        if d.family == "debian" and req.apt_pkg:
            missing.append(req)
        elif d.family == "rhel" and req.dnf_pkg:
            missing.append(req)
        else:
            unsupported.append(req)

    return PrereqReport(
        distro=d,
        missing=missing,
        present=present,
        unsupported=unsupported,
    )


def python_minor_version(python_bin: str = "python3") -> str | None:
    """Return 'X.Y' for `python_bin` (e.g. '3.14'), or None on failure.

    Used to compute the correct apt package name for venv on Debian-family
    distros. The shipped package name is `python<X.Y>-venv`, not `python3-venv`,
    on every modern Ubuntu/Debian — installing the wrong name silently no-ops.
    """
    try:
        r = subprocess.run(
            [python_bin, "-c",
             "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def venv_apt_package_name(python_bin: str = "python3") -> str:
    """Return the Debian/Ubuntu apt package that ships venv for `python_bin`.

    Falls back to the generic 'python3-venv' only when the version probe fails;
    that's the safest default (it exists as a meta-package on most Debian-based
    distros). For specific versions we prefer the versioned name.
    """
    minor = python_minor_version(python_bin)
    if not minor:
        return "python3-venv"
    return f"python{minor}-venv"


def install_commands(report: PrereqReport) -> list[list[str]]:
    """Return the shell commands install.sh should run to fix `missing`.

    No sudo prefix — the bash side adds that. We just emit the package lists.
    Returns an empty list if nothing to do.

    The `python3-venv` entry is resolved dynamically against the running
    Python's minor version (see `venv_apt_package_name`).
    """
    if not report.missing:
        return []
    d = report.distro
    pkgs: list[str] = []
    for req in report.missing:
        if req.name == "python3-venv":
            pkg: str | None = (
                venv_apt_package_name() if d.family == "debian" else None
            )
        else:
            pkg = req.apt_pkg if d.family == "debian" else req.dnf_pkg
        if pkg:
            pkgs.append(pkg)
    if not pkgs:
        return []
    if d.family == "debian":
        return [
            ["apt-get", "update", "-y"],
            ["apt-get", "install", "-y", *pkgs],
        ]
    if d.family == "rhel":
        pm = d.package_manager or "dnf"
        return [[pm, "install", "-y", *pkgs]]
    return []
