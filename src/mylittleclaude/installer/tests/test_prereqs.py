from __future__ import annotations

from pathlib import Path

import pytest

from mylittleclaude.installer.prereqs import (
    PREREQS,
    PrereqReport,
    audit,
    detect_distro,
    install_commands,
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
