"""Tests for the Ansible execution-environment matrix derivation (#2154).

The derivation decides which images exist, and its failure mode is quiet: a
truncated matrix publishes fewer EEs than operators can select, and a staleness
check that answers "current" when it is not means a CVE fix is never rebuilt.
Neither crashes, so both are tests rather than something CI would notice.

Network is stubbed throughout -- these pin the logic, not PyPI.
"""

from __future__ import annotations

import pytest
from conftest import ee_matrix, ee_matrix_outputs


def _index(versions):
    return {"releases": {v: [] for v in versions}}


@pytest.fixture
def pypi(monkeypatch):
    """Stub PyPI with a controllable release set and classifier map."""
    state = {"versions": [], "classifiers": {}}

    def fake_get(url):
        # The index is `.../ansible-core/json`; a release is
        # `.../ansible-core/<version>/json`. Discriminate on the shape rather
        # than on a slash count, which is off-by-one in the obvious direction.
        if url.endswith("/ansible-core/json"):
            return _index(state["versions"])
        version = url.rsplit("/", 2)[-2]
        return {
            "info": {
                "classifiers": [
                    f"Programming Language :: Python :: {p}"
                    for p in state["classifiers"].get(version, [])
                ]
            }
        }

    monkeypatch.setattr(ee_matrix, "_get_json", fake_get)
    return state


# ── the range ────────────────────────────────────────────────────────


def test_lines_below_the_floor_are_excluded(pypi):
    """2.11 and earlier need a py2 CONTROLLER, which does not build."""
    pypi["versions"] = ["2.9.27", "2.11.12", "2.12.10", "2.13.13"]
    lines = ee_matrix.latest_patch_per_line()
    assert sorted(lines) == [(2, 12), (2, 13)]


def test_only_the_newest_patch_of_each_line(pypi):
    pypi["versions"] = ["2.16.1", "2.16.19", "2.16.4"]
    assert ee_matrix.latest_patch_per_line()[(2, 16)] == "2.16.19"


def test_pre_releases_are_not_candidates(pypi):
    """A workspace may pin an rc; we publish no EE for one."""
    pypi["versions"] = ["2.21.5", "2.21.6rc1", "2.22.0b1"]
    assert ee_matrix.latest_patch_per_line() == {(2, 21): "2.21.5"}


def test_declared_pythons_reads_the_classifiers(pypi):
    pypi["classifiers"] = {"2.16.19": ["3.10", "3.11", "3.12"]}
    assert ee_matrix.declared_pythons("2.16.19") == [(3, 10), (3, 11), (3, 12)]


def test_the_build_picks_the_NEWEST_declared_python(one_line, registry, pypi):
    """The interpreter is the one dial we control on a line that is unpatchable
    by construction, so it is turned the right way. Choosing the oldest in range
    would 'work' and silently age every image.

    Asserted through `build_matrix`, not through `declared_pythons`: the choice
    is made by the caller, so testing the helper's sort order would pass with
    the selection reversed."""
    pypi["classifiers"]["2.16.0"] = ["3.9", "3.10", "3.11"]
    entry = ee_matrix.build_matrix("repo", force=True, only="2.16")[0]
    assert entry["python"] == "3.11"
    assert entry["base"] == "python:3.11-slim"


# ── staleness, which is read from the image ──────────────────────────


@pytest.fixture
def registry(monkeypatch):
    state = {"provenance": {}, "digests": {}}
    monkeypatch.setattr(
        ee_matrix, "published_provenance", lambda repo, tag: state["provenance"].get(tag)
    )
    monkeypatch.setattr(
        ee_matrix, "base_digest", lambda base: state["digests"].get(base, "sha256:d")
    )
    return state


@pytest.fixture
def one_line(pypi):
    pypi["versions"] = [f"2.{n}.0" for n in range(12, 22)]
    pypi["classifiers"] = {f"2.{n}.0": ["3.12", "3.13", "3.14"] for n in range(12, 22)}
    return pypi


def test_absent_provenance_means_rebuild(one_line, registry):
    """The conservative default, and what makes the first run correct with
    nothing seeded."""
    entries = ee_matrix.build_matrix("repo", force=False, only="2.16")
    assert [e["line"] for e in entries] == ["2.16"]
    assert "no provenance" in entries[0]["reason"]


def test_current_provenance_is_skipped(one_line, registry):
    registry["provenance"]["2.16"] = {
        "io.terrapod.ee.ansible-core": "2.16.0",
        "io.terrapod.ee.base-digest": "sha256:d",
    }
    assert ee_matrix.build_matrix("repo", force=False, only="2.16") == []


def test_a_moved_ansible_patch_rebuilds(one_line, registry):
    registry["provenance"]["2.16"] = {
        "io.terrapod.ee.ansible-core": "2.16.0-old",
        "io.terrapod.ee.base-digest": "sha256:d",
    }
    entries = ee_matrix.build_matrix("repo", force=False, only="2.16")
    assert "ansible-core moved" in entries[0]["reason"]


def test_a_moved_base_digest_rebuilds(one_line, registry):
    """A Debian refresh is a CVE fix for every EE on that base, and the only
    signal is the digest -- the tag has not changed."""
    registry["provenance"]["2.16"] = {
        "io.terrapod.ee.ansible-core": "2.16.0",
        "io.terrapod.ee.base-digest": "sha256:OLD",
    }
    entries = ee_matrix.build_matrix("repo", force=False, only="2.16")
    assert entries[0]["reason"] == "base image digest moved"


# ── the stops ────────────────────────────────────────────────────────


def test_a_python_with_no_base_image_is_a_stop(one_line, registry, monkeypatch):
    """Never a silent fall back to an older interpreter: that would quietly
    contradict the rule the whole matrix is built on."""
    monkeypatch.setattr(ee_matrix, "base_digest", lambda base: None)
    with pytest.raises(SystemExit, match="does not resolve"):
        ee_matrix.build_matrix("repo", force=True, only="2.16")


def test_a_truncated_pypi_answer_is_a_stop(pypi, registry):
    """Publishing a short matrix would silently remove EEs operators select."""
    pypi["versions"] = ["2.20.0", "2.21.0"]
    with pytest.raises(SystemExit, match="Refusing to publish a truncated matrix"):
        ee_matrix.build_matrix("repo", force=True, only=None)


def test_a_line_declaring_no_python_is_a_stop(one_line, registry):
    one_line["classifiers"]["2.16.0"] = []
    with pytest.raises(SystemExit, match="declares no python classifiers"):
        ee_matrix.build_matrix("repo", force=True, only="2.16")


# ── the Actions matrix shape ─────────────────────────────────────────


def test_the_build_set_is_a_true_cross_product(one_line, registry, capsys):
    """A GitHub matrix `include` EXTENDS combinations rather than crossing with
    another dimension, so pairing it with `arch:` yields N+2 entries and not
    N*2. The cross product is therefore built here, where it can be asserted."""
    import json

    ee_matrix.main.__globals__["sys"].argv = [
        "ee_matrix",
        "--repository",
        "repo",
        "--all",
    ]
    ee_matrix.main()
    emitted = json.loads(capsys.readouterr().out)
    assert len(emitted["build"]) == len(emitted["lines"]) * len(ee_matrix.ARCHES)
    assert sorted({e["arch"] for e in emitted["build"]}) == ["amd64", "arm64"]
    for entry in emitted["build"]:
        assert {"line", "version", "base", "base_digest", "arch"} <= set(entry)


def test_outputs_render_both_shapes_under_include(tmp_path, capsys):
    import json

    matrix = tmp_path / "m.json"
    matrix.write_text(
        json.dumps(
            {
                "build": [{"line": "2.16", "arch": "amd64"}, {"line": "2.16", "arch": "arm64"}],
                "lines": [{"line": "2.16"}],
            }
        )
    )
    ee_matrix_outputs.main.__globals__["sys"].argv = ["x", str(matrix)]
    ee_matrix_outputs.main()
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().split("\n"))
    assert out["any"] == "true"
    assert json.loads(out["build"])["include"][0]["arch"] == "amd64"
    assert len(json.loads(out["lines"])["include"]) == 1


def test_an_empty_matrix_reports_any_false(tmp_path, capsys):
    """`any` gates the build job; reporting true on an empty set would start a
    matrix job with no entries."""
    import json

    matrix = tmp_path / "m.json"
    matrix.write_text(json.dumps({"build": [], "lines": []}))
    ee_matrix_outputs.main.__globals__["sys"].argv = ["x", str(matrix)]
    ee_matrix_outputs.main()
    assert "any=false" in capsys.readouterr().out
