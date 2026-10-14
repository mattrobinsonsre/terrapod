"""`api_ansible.ansible_inventory_binary` — which version, and cached how (#2035).

This module had **no tests at all** while it decided which ansible-core the API
parses every inventory with, which is why it could resolve
`api.config.default_ansible_version` and never consult the workspace without
anything noticing.

Mocked at `_install`, which is the boundary below the function under test: the
pip plumbing above it has its own reasons and its own module docstring, and a
test that patched `ansible_inventory_binary`'s own internals would assert on a
function that does not run in production's configuration.
"""

from __future__ import annotations

import pytest

from terrapod.services import api_ansible
from terrapod.services.api_ansible import AnsibleUnavailable, ansible_inventory_binary


@pytest.fixture(autouse=True)
def _clean_memo():
    """The memo is a module global, so it leaks between tests both ways."""
    api_ansible._reset_for_tests()
    yield
    api_ansible._reset_for_tests()


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """`_install` that really creates the binary, and records what it was asked for.

    It creates the file because the function checks `binary.exists()` after
    installing -- a fake that recorded the call and made nothing would exercise
    the "installed but produced no ansible-inventory" branch instead of the
    happy path, and pass for the wrong reason.
    """
    monkeypatch.setattr(api_ansible, "_tool_dir", lambda: tmp_path)
    asked: list[str] = []

    async def fake_install(version: str, venv):
        asked.append(version)
        binary = api_ansible._bin_dir(venv)
        binary.mkdir(parents=True, exist_ok=True)
        (binary / "ansible-inventory").write_text("#!/bin/sh\n")

    monkeypatch.setattr(api_ansible, "_install", fake_install)
    return asked


class TestItInstallsTheVersionItIsAskedFor:
    async def test_the_requested_version_is_what_gets_installed(self, staged, tmp_path):
        """The defect in one assertion: it used to install
        `api.config.default_ansible_version` whatever it was handed."""
        path = await ansible_inventory_binary("2.18.3")

        assert staged == ["2.18.3"]
        assert "ansible-2.18.3" in path, (
            f"installed into the wrong venv for the requested version: {path}"
        )

    async def test_the_deployment_default_is_not_consulted(self, staged, monkeypatch):
        """Handed a version, it must not reach for config at all.

        `test_ansible_version_resolution` proves it holds no config READ; this
        proves the behaviour, by setting the default to something that would be
        obvious in the result.
        """
        from terrapod.config import settings

        monkeypatch.setattr(settings, "default_ansible_version", "9.9.9")
        path = await ansible_inventory_binary("2.18.3")

        assert staged == ["2.18.3"]
        assert "9.9.9" not in path


class TestTheMemoIsPerVersion:
    """One slot was correct only while every resolution used one version.

    With a workspace pinning its own, a single slot would serve whichever
    workspace resolved FIRST to every workspace after it -- a worse skew than
    the one this issue closes, because which version you got would depend on
    arrival order.
    """

    async def test_two_versions_get_two_binaries(self, staged):
        first = await ansible_inventory_binary("2.18.3")
        second = await ansible_inventory_binary("2.21.5")

        assert first != second, "the second version was served the first one's binary"
        assert staged == ["2.18.3", "2.21.5"]

    async def test_the_same_version_twice_installs_once(self, staged):
        first = await ansible_inventory_binary("2.18.3")
        again = await ansible_inventory_binary("2.18.3")

        assert first == again
        assert staged == ["2.18.3"], "the memo did not hold, so every read reinstalls"

    async def test_an_already_present_venv_is_adopted_without_installing(
        self, tmp_path, monkeypatch
    ):
        """A restart must not reinstall what the PVC already holds."""
        monkeypatch.setattr(api_ansible, "_tool_dir", lambda: tmp_path)
        binary = api_ansible._bin_dir(tmp_path / "ansible-2.18.3")
        binary.mkdir(parents=True)
        (binary / "ansible-inventory").write_text("#!/bin/sh\n")

        async def refuse(version, venv):  # pragma: no cover -- must not run
            raise AssertionError("installed over a venv that was already there")

        monkeypatch.setattr(api_ansible, "_install", refuse)

        assert await ansible_inventory_binary("2.18.3") == str(binary / "ansible-inventory")


class TestItRefusesRatherThanGuessing:
    async def test_an_empty_version_is_refused_by_name(self, staged):
        """It fails closed, and says which two places could have supplied one.

        `effective_ansible_version` returns `""` when the workspace pins nothing
        AND the deployment default is empty, deliberately leaving the error to
        the caller -- so this is where that case has to be named.
        """
        with pytest.raises(AnsibleUnavailable) as err:
            await ansible_inventory_binary("")

        message = str(err.value)
        assert "workspace" in message and "default_ansible_version" in message, (
            f"the refusal does not say where a version should have come from: {message!r}"
        )
        assert staged == [], "it tried to install despite having no version"

    async def test_there_is_no_default_argument(self):
        """A default would let a caller omit the version and silently
        reintroduce the deployment-wide read this issue removed."""
        import inspect

        param = inspect.signature(ansible_inventory_binary).parameters["version"]
        assert param.default is inspect.Parameter.empty, (
            "`version` has a default, so a caller can omit it -- which is how the "
            "deployment-wide resolution gets back in without anyone writing it down"
        )
