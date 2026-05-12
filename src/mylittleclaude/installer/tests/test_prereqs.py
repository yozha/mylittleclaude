from __future__ import annotations

from pathlib import Path

import pytest

from mylittleclaude.installer.prereqs import (
    PREREQS,
    PrereqReport,
    audit,
    detect_distro,
    install_commands,
    python_minor_version,
    venv_apt_package_name,
)


def _write_os_release(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "os-release"
    p.write_text(content)
    return p


def test_detect_ubuntu(tmp_path):
    p = _write_os_release(tmp_path, """\
NAME="Ubuntu"
ID=ubuntu
ID_LIKE=debian
VERSION_ID="24.04"
PRETTY_NAME="Ubuntu 24.04 LTS"
""")
    d = detect_distro(p)
    assert d.family == "debian"
    assert d.package_manager == "apt-get"
    assert d.pretty_name == "Ubuntu 24.04 LTS"


def test_detect_debian(tmp_path):
    p = _write_os_release(tmp_path, """\
ID=debian
VERSION_ID="12"
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
""")
    d = detect_distro(p)
    assert d.family == "debian"


def test_detect_rocky(tmp_path):
    p = _write_os_release(tmp_path, """\
ID="rocky"
ID_LIKE="rhel centos fedora"
VERSION_ID="9.3"
PRETTY_NAME="Rocky Linux 9.3 (Blue Onyx)"
""")
    d = detect_distro(p)
    assert d.family == "rhel"
    # package manager preference depends on host; just assert it's set
    assert d.package_manager in ("dnf", "yum")


def test_detect_alpine_unknown(tmp_path):
    p = _write_os_release(tmp_path, """\
ID=alpine
PRETTY_NAME="Alpine Linux v3.19"
""")
    d = detect_distro(p)
    assert d.family == "unknown"
    assert d.package_manager is None


def test_detect_missing_file_is_unknown(tmp_path):
    d = detect_distro(tmp_path / "no-such-file")
    assert d.family == "unknown"


def test_audit_pretends_everything_missing(monkeypatch, tmp_path):
    # Force each Prereq.is_installed() to False.
    for req in PREREQS:
        monkeypatch.setattr(type(req), "is_installed", lambda self: False, raising=False)
    p = _write_os_release(tmp_path, "ID=ubuntu\nID_LIKE=debian\n")
    d = detect_distro(p)
    report = audit(d)
    assert not report.all_satisfied
    # systemctl has no package on either family — it's "unsupported" if missing,
    # but only because it's expected to ship preinstalled.
    missing_names = {r.name for r in report.missing}
    assert "git" in missing_names
    assert "nodejs" in missing_names


def test_install_commands_debian(tmp_path):
    p = _write_os_release(tmp_path, "ID=ubuntu\nID_LIKE=debian\n")
    d = detect_distro(p)
    # Build a report with one fake missing entry.
    fake = [r for r in PREREQS if r.name == "git"]
    report = PrereqReport(distro=d, missing=fake, present=[], unsupported=[])
    cmds = install_commands(report)
    assert cmds == [
        ["apt-get", "update", "-y"],
        ["apt-get", "install", "-y", "git"],
    ]


def test_install_commands_rhel(tmp_path):
    p = _write_os_release(tmp_path, "ID=rocky\nID_LIKE=rhel\n")
    d = detect_distro(p)
    fake = [r for r in PREREQS if r.name == "git"]
    report = PrereqReport(distro=d, missing=fake, present=[], unsupported=[])
    cmds = install_commands(report)
    assert len(cmds) == 1
    assert cmds[0][0] in ("dnf", "yum")
    assert cmds[0][1:3] == ["install", "-y"]
    assert "git" in cmds[0]


def test_install_commands_empty_when_nothing_missing(tmp_path):
    p = _write_os_release(tmp_path, "ID=ubuntu\n")
    d = detect_distro(p)
    report = PrereqReport(distro=d, missing=[], present=list(PREREQS), unsupported=[])
    assert install_commands(report) == []


def test_install_commands_unsupported_distro(tmp_path):
    p = _write_os_release(tmp_path, "ID=alpine\n")
    d = detect_distro(p)
    report = PrereqReport(distro=d, missing=list(PREREQS), present=[], unsupported=[])
    assert install_commands(report) == []


# --- v0.2.1: functional checks + dynamic venv package name -------------------


def test_python_minor_version_works():
    # The test runner's Python is what we read; should be 3.<something>.
    v = python_minor_version()
    assert v is not None
    major, minor = v.split(".")
    assert major == "3"
    assert int(minor) >= 11


def test_python_minor_version_returns_none_for_missing_binary():
    assert python_minor_version("/this/path/does/not/exist") is None


def test_venv_apt_package_name_matches_running_python():
    pkg = venv_apt_package_name()
    # On any real Python 3.11+ host, this should be "python3.<minor>-venv".
    assert pkg.startswith("python3.")
    assert pkg.endswith("-venv")
    # And it should agree with python_minor_version.
    minor = python_minor_version()
    assert minor is not None
    assert pkg == f"python{minor}-venv"


def test_venv_apt_package_name_falls_back_when_probe_fails():
    pkg = venv_apt_package_name("/this/path/does/not/exist")
    # Falls back to the generic name. NOT just "python3-venv" — the function
    # might return that, that's actually what we want as fallback.
    assert pkg == "python3-venv"


def test_install_commands_debian_uses_dynamic_venv_pkg(tmp_path):
    """v0.2.0 hardcoded 'python3-venv' which silently no-ops on Ubuntu 26.04.
    The Debian package is named after the running Python's minor version."""
    p = _write_os_release(tmp_path, "ID=ubuntu\nID_LIKE=debian\n")
    d = detect_distro(p)
    venv_req = [r for r in PREREQS if r.name == "python3-venv"]
    assert venv_req, "PREREQS must contain a python3-venv entry"
    report = PrereqReport(distro=d, missing=venv_req, present=[], unsupported=[])
    cmds = install_commands(report)
    assert len(cmds) == 2
    assert cmds[0] == ["apt-get", "update", "-y"]
    # The install line must mention python<X.Y>-venv, NOT the generic name.
    install_line = cmds[1]
    assert install_line[:3] == ["apt-get", "install", "-y"]
    assert len(install_line) == 4
    assert install_line[3].startswith("python3.")
    assert install_line[3].endswith("-venv")


def test_install_commands_rhel_skips_venv_pkg(tmp_path):
    """On RHEL family, venv ships with python — no separate package."""
    p = _write_os_release(tmp_path, "ID=rocky\nID_LIKE=rhel\n")
    d = detect_distro(p)
    venv_req = [r for r in PREREQS if r.name == "python3-venv"]
    report = PrereqReport(distro=d, missing=venv_req, present=[], unsupported=[])
    cmds = install_commands(report)
    # No package to install; commands list is empty.
    assert cmds == []


def test_venv_prereq_uses_functional_check():
    """v0.2.0 used `python3 -c 'import venv'` which passes even when ensurepip
    is missing. The fix is to use `python3 -m venv --help` which fails iff
    the subsystem is actually broken."""
    venv_req = next(r for r in PREREQS if r.name == "python3-venv")
    assert venv_req.check_cmd == ["python3", "-m", "venv", "--help"]
    # Sanity: the check passes on this venv.
    assert venv_req.is_installed() is True
