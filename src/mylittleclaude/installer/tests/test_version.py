from __future__ import annotations

import pytest

from mylittleclaude.installer.version import Version, compare, pyproject_version


def test_parse_basic():
    v = Version.parse("0.2.0")
    assert (v.major, v.minor, v.patch) == (0, 2, 0)
    assert v.pre == ""
    assert v.pre_rank == 1


def test_parse_with_v_prefix():
    v = Version.parse("v1.2.3")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)


def test_parse_with_prerelease_dash():
    v = Version.parse("v0.2.0-rc1")
    assert v.pre == "rc1"
    assert v.pre_rank == 0


def test_parse_with_prerelease_dot():
    v = Version.parse("0.2.0.rc1")
    assert v.pre == "rc1"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        Version.parse("not-a-version")
    with pytest.raises(ValueError):
        Version.parse("1.2")


def test_compare_basic():
    assert compare("0.1.0", "0.2.0") == -1
    assert compare("0.2.0", "0.2.0") == 0
    assert compare("1.0.0", "0.99.0") == 1


def test_compare_prerelease_before_release():
    # A pre-release sorts *before* the matching release version.
    assert compare("0.2.0-rc1", "0.2.0") == -1
    assert compare("0.2.0", "0.2.0-rc1") == 1


def test_version_tag_roundtrip():
    v = Version.parse("v0.2.0")
    assert v.tag() == "v0.2.0"
    assert str(v) == "0.2.0"


def test_pyproject_version_reads_real_file(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    assert pyproject_version(p) == "1.2.3"


def test_pyproject_version_missing_returns_zero(tmp_path):
    assert pyproject_version(tmp_path / "nope.toml") == "0.0.0"


def test_pyproject_version_no_version_field(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[project]\nname = "x"\n')
    assert pyproject_version(p) == "0.0.0"
