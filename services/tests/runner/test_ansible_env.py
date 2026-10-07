"""Obtaining ansible-core on the runner (#2010).

The properties worth pinning are the ones that are invisible when they regress:
that the install goes through Terrapod's own proxy rather than upstream, that
pip is never allowed to build from source, that a half-finished install is not
mistaken for a complete one, and that a failure fails the Job rather than
letting a configure proceed without ansible.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from terrapod.runner.phases import ansible_env


def _cfg(**over):
    from terrapod.runner.runner_config import RunnerConfig as RC

    base = {
        "TP_API_URL": "http://terrapod-api:8000",
        "TP_AUTH_TOKEN": "runtok:abc",
        "TP_RUN_ID": "11111111-1111-1111-1111-111111111111",
        "TP_PHASE": "plan",
        **over,
    }
    for k, v in base.items():
        os.environ[k] = v
    return RC.from_env()


class _Recorder:
    """Stands in for `exec_subprocess.run`, recording every argv."""

    def __init__(self, *, codes=None):
        self.calls: list[list[str]] = []
        self.codes = list(codes or [])

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        code = self.codes.pop(0) if self.codes else 0

        # Behave like the tool being replaced: a successful `python -m venv`
        # creates the directory and a `bin/python` inside it. Without this the
        # fake is not standing in for venv, it is standing in for a venv that
        # silently did nothing — and the code is right to object to that.
        if "venv" in argv and code == 0:
            target = Path(argv[-1])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("#!/bin/sh\n")

        class R:
            exit_code = code

        return R()


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch, tmp_path):
    """Keep the real `$HOME` and `os.environ` out of it.

    `write_netrc` writes a credential to `$HOME`, and `pip_env` mutates
    `os.environ` -- both would otherwise escape the test.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


class TestItGoesThroughTerrapodsOwnProxy:
    """Never upstream. The proxy is the only thing on the runner's side with
    upstream reach, so a direct fetch breaks every sealed deployment."""

    def test_pip_is_pointed_at_the_package_cache(self, monkeypatch, tmp_path):
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)
        monkeypatch.delenv("PIP_INDEX_URL", raising=False)

        ansible_env.ensure(_cfg(), version="2.21.5", venv=tmp_path / "v")

        assert "/package-cache/pypi/simple" in os.environ["PIP_INDEX_URL"], (
            "the install would reach PyPI directly, which a sealed deployment cannot do"
        )

    def test_the_token_goes_in_a_netrc_not_the_index_url(self, monkeypatch, tmp_path):
        """The runner streams its logs and pip prints its index URL."""
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        ansible_env.ensure(_cfg(), version="2.21.5", venv=tmp_path / "v")

        netrc = Path(os.environ["HOME"]) / ".netrc"
        assert "runtok:abc" in netrc.read_text()
        assert "runtok:abc" not in os.environ["PIP_INDEX_URL"]
        # And not on any argv either, for the same reason.
        assert not any("runtok:abc" in a for call in rec.calls for a in call)

    def test_an_http_api_is_named_as_a_trusted_host(self, monkeypatch, tmp_path):
        """Without this pip IGNORES an http index -- it does not fail -- and the
        install dies with "No matching distribution found" for a package the
        proxy was serving perfectly well."""
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        ansible_env.ensure(_cfg(), version="2.21.5", venv=tmp_path / "v")

        assert os.environ.get("PIP_TRUSTED_HOST") == "terrapod-api"


class TestPipMayNotBuildFromSource:
    def test_only_binary_is_passed(self, monkeypatch, tmp_path):
        """A security property, not a speed one: building from source executes a
        package's setup.py, which is the boundary #1970 was closed to hold."""
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        ansible_env.ensure(_cfg(), version="2.21.5", venv=tmp_path / "v")

        install = [c for c in rec.calls if "install" in c]
        assert len(install) == 1
        assert "--only-binary=:all:" in install[0]

    def test_the_exact_version_is_pinned(self, monkeypatch, tmp_path):
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        ansible_env.ensure(_cfg(), version="2.18.3", venv=tmp_path / "v")

        install = [c for c in rec.calls if "install" in c][0]
        assert "ansible-core==2.18.3" in install


class TestAHalfFinishedInstallIsNotReused:
    """The marker is written last, deliberately.

    Keying on the directory existing would let a Job reuse a venv whose pip
    install had failed -- running an ansible nobody chose, or none at all.
    """

    def test_the_marker_is_written_only_after_a_successful_install(self, monkeypatch, tmp_path):
        venv = tmp_path / "v"
        # venv creation succeeds, pip install fails.
        rec = _Recorder(codes=[0, 1])
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        with pytest.raises(ansible_env.AnsibleUnavailable):
            ansible_env.ensure(_cfg(), version="2.21.5", venv=venv)

        assert not ansible_env.is_installed("2.21.5", venv), (
            "a failed install left a marker, so the next Job would reuse it"
        )

    def test_a_second_call_with_the_same_version_is_a_no_op(self, monkeypatch, tmp_path):
        venv = tmp_path / "v"
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        ansible_env.ensure(_cfg(), version="2.21.5", venv=venv)
        first = len(rec.calls)
        ansible_env.ensure(_cfg(), version="2.21.5", venv=venv)

        assert len(rec.calls) == first, "the second call re-installed"

    def test_a_different_version_is_installed_over_the_top(self, monkeypatch, tmp_path):
        """An operator bumping the Helm value must actually get the new version."""
        venv = tmp_path / "v"
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        ansible_env.ensure(_cfg(), version="2.21.5", venv=venv)
        before = len(rec.calls)
        ansible_env.ensure(_cfg(), version="2.18.3", venv=venv)

        assert len(rec.calls) > before
        assert ansible_env.is_installed("2.18.3", venv)


class TestItFailsClosed:
    """A configure with no ansible has no weaker thing it could do instead."""

    def test_a_failed_venv_creation_raises(self, monkeypatch, tmp_path):
        rec = _Recorder(codes=[1])
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        with pytest.raises(ansible_env.AnsibleUnavailable):
            ansible_env.ensure(_cfg(), version="2.21.5", venv=tmp_path / "v")

    def test_a_failed_install_raises_and_says_what_a_sealed_deployment_means(
        self, monkeypatch, tmp_path
    ):
        rec = _Recorder(codes=[0, 1])
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        with pytest.raises(ansible_env.AnsibleUnavailable) as err:
            ansible_env.ensure(_cfg(), version="2.21.5", venv=tmp_path / "v")

        # The most likely cause on a sealed install is a proxy that has never
        # seen the version, which reads as "does not exist" without this.
        assert "pull-through" in str(err.value)

    def test_no_configured_version_raises_rather_than_guessing(self, monkeypatch, tmp_path):
        rec = _Recorder()
        monkeypatch.setattr(ansible_env.exec_subprocess, "run", rec)

        with pytest.raises(ansible_env.AnsibleUnavailable):
            ansible_env.ensure(_cfg(), version="", venv=tmp_path / "v")
        assert rec.calls == [], "it tried to install something with no version"


def test_bin_dir_is_where_the_executables_land(tmp_path):
    assert ansible_env.bin_dir(tmp_path / "v") == tmp_path / "v" / "bin"
