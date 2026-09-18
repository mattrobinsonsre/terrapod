"""A Pulumi program's dependencies are installed before it runs (#1566).

Only a YAML program worked before this: every other runtime needs a toolchain
the image does not carry and packages nobody installs. The reader is the part
worth the most attention — it is deliberately not a YAML parser, so the shapes
Pulumi actually writes are pinned here rather than assumed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner.phases import pulumi_deps


def _program(tmp_path: Path, pulumi_yaml: str | None = None, **files: str) -> Path:
    if pulumi_yaml is not None:
        (tmp_path / "Pulumi.yaml").write_text(pulumi_yaml)
    for name, body in files.items():
        (tmp_path / name.replace("__", ".")).write_text(body)
    return tmp_path


class TestReadingTheRuntime:
    """The two shapes Pulumi writes, and the many it does not."""

    def test_the_inline_form(self, tmp_path):
        d = _program(tmp_path, "name: p\nruntime: nodejs\n")
        assert pulumi_deps.read_runtime(d) == "nodejs"

    def test_the_block_form(self, tmp_path):
        d = _program(
            tmp_path,
            "name: p\nruntime:\n  name: nodejs\n  options:\n    typescript: true\n",
        )
        assert pulumi_deps.read_runtime(d) == "nodejs"

    def test_a_quoted_value(self, tmp_path):
        d = _program(tmp_path, 'name: p\nruntime: "nodejs"\n')
        assert pulumi_deps.read_runtime(d) == "nodejs"

    def test_a_trailing_comment(self, tmp_path):
        d = _program(tmp_path, "runtime: nodejs  # the language host\n")
        assert pulumi_deps.read_runtime(d) == "nodejs"

    def test_case_is_normalised(self, tmp_path):
        d = _program(tmp_path, "runtime: NodeJS\n")
        assert pulumi_deps.read_runtime(d) == "nodejs"

    def test_yaml_reads_as_yaml(self, tmp_path):
        d = _program(tmp_path, "name: p\nruntime: yaml\nresources: {}\n")
        assert pulumi_deps.read_runtime(d) == "yaml"

    def test_a_block_that_ends_before_a_name_is_not_read_from_the_next_key(self, tmp_path):
        # `description` is at column zero, so the runtime block is over. Reading
        # on would pick up an unrelated `name:` and install the wrong toolchain.
        d = _program(tmp_path, "runtime:\ndescription: d\nname: python\n")
        assert pulumi_deps.read_runtime(d) == ""

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert pulumi_deps.read_runtime(tmp_path) == ""

    def test_an_absent_runtime_key_is_not_an_error(self, tmp_path):
        d = _program(tmp_path, "name: p\ndescription: no runtime here\n")
        assert pulumi_deps.read_runtime(d) == ""

    def test_a_key_that_merely_starts_with_runtime_is_not_it(self, tmp_path):
        d = _program(tmp_path, "runtime-options: nodejs\nruntime: python\n")
        assert pulumi_deps.read_runtime(d) == "python"


class TestClassifying:
    def test_nodejs_is_supported(self, tmp_path):
        got = pulumi_deps.classify(_program(tmp_path, "runtime: nodejs\n"))
        assert got == pulumi_deps.Runtime(name="nodejs", supported=True)

    def test_yaml_needs_nothing(self, tmp_path):
        got = pulumi_deps.classify(_program(tmp_path, "runtime: yaml\n"))
        assert got.supported is True

    @pytest.mark.parametrize("runtime", ["go", "dotnet"])
    def test_the_others_are_not_supported_yet(self, tmp_path, runtime):
        got = pulumi_deps.classify(_program(tmp_path, f"runtime: {runtime}\n"))
        assert got == pulumi_deps.Runtime(name=runtime, supported=False)


class TestTheNpmrc:
    """The credential goes in a file, never on a command line."""

    def _cfg(self):
        return SimpleNamespace(api_url="https://terrapod.test", auth_token="runtok:abc")

    def test_it_points_at_terrapods_proxy(self, tmp_path):
        pulumi_deps.write_npmrc(tmp_path, "https://terrapod.test", "tok")
        body = (tmp_path / ".npmrc").read_text()
        assert "registry=https://terrapod.test/api/terrapod/v1/package-cache/npm/" in body

    def test_the_token_is_keyed_to_that_registry_path(self, tmp_path):
        # npm matches `_authToken` by path, so the key has to be the registry
        # URL without its scheme or it is silently never sent.
        pulumi_deps.write_npmrc(tmp_path, "https://terrapod.test", "tok")
        body = (tmp_path / ".npmrc").read_text()
        assert "//terrapod.test/api/terrapod/v1/package-cache/npm/:_authToken=tok" in body

    def test_it_is_not_world_readable(self, tmp_path):
        path = pulumi_deps.write_npmrc(tmp_path, "https://terrapod.test", "tok")
        assert path.stat().st_mode & 0o077 == 0

    def test_it_addresses_the_alias_a_lagging_runner_can_reach(self, tmp_path):
        # The runner image lags the API by design, and only the legacy alias is
        # served by both — the same reasoning as the plugin override.
        assert "/api/terrapod/v1/" in pulumi_deps.npm_registry_url("https://x")

    def test_a_trailing_slash_on_the_api_url_does_not_double(self, tmp_path):
        assert "//package-cache" not in pulumi_deps.npm_registry_url("https://x/")


class TestTheCacheRedirect:
    """Only /workspace, /tmp and $HOME are writable."""

    def test_npm_is_told_where_it_may_write(self):
        assert pulumi_deps.npm_env()["npm_config_cache"].startswith("/tmp/")

    def test_the_update_check_is_off(self):
        # It writes under $HOME and reaches upstream, which a sealed deployment
        # cannot do.
        assert pulumi_deps.npm_env()["npm_config_update_notifier"] == "false"


class TestInstalling:
    #: Distinctive on purpose: "tok" occurs inside pytest's own tmp-path name
    #: for the test below, which made the assertion pass on the path rather than
    #: on a credential.
    SECRET = "runtok-s3cr3t-9f21"

    def _cfg(self):
        return SimpleNamespace(api_url="https://terrapod.test", auth_token=self.SECRET)

    def test_a_yaml_program_installs_nothing(self, tmp_path):
        d = _program(tmp_path, "runtime: yaml\n")
        with patch.object(pulumi_deps, "_install_nodejs") as node:
            pulumi_deps.install(self._cfg(), d, child_grace=5, log_file="/dev/null")
        assert node.call_count == 0

    def test_an_unsupported_runtime_is_refused_by_name(self, tmp_path):
        d = _program(tmp_path, "runtime: go\n")
        with pytest.raises(pulumi_deps.DependencyError, match="'go'"):
            pulumi_deps.install(self._cfg(), d, child_grace=5, log_file="/dev/null")

    def test_the_refusal_says_what_does_work(self, tmp_path):
        d = _program(tmp_path, "runtime: dotnet\n")
        with pytest.raises(pulumi_deps.DependencyError, match="nodejs"):
            pulumi_deps.install(self._cfg(), d, child_grace=5, log_file="/dev/null")

    def _run_nodejs(self, tmp_path, exit_code=0, lockfile=False):
        d = _program(tmp_path, "runtime: nodejs\n", package__json="{}")
        if lockfile:
            (d / "package-lock.json").write_text("{}")
        node_dir = tmp_path / "node" / "bin"
        node_dir.mkdir(parents=True)
        (node_dir / "node").write_text("#!/node")
        cli = tmp_path / "node" / "lib" / "node_modules" / "npm" / "bin"
        cli.mkdir(parents=True)
        (cli / "npm-cli.js").write_text("//npm")
        result = MagicMock(exit_code=exit_code)
        with (
            patch.object(pulumi_deps, "_node_bin", return_value=node_dir),
            patch.object(pulumi_deps.exec_subprocess, "run", return_value=result) as run,
        ):
            pulumi_deps.install(self._cfg(), d, child_grace=5, log_file="/dev/null")
        return run

    def test_a_lockfile_means_ci(self, tmp_path):
        run = self._run_nodejs(tmp_path, lockfile=True)
        assert run.call_args.args[0][-1] == "ci"

    def test_without_one_it_installs(self, tmp_path):
        run = self._run_nodejs(tmp_path, lockfile=False)
        assert run.call_args.args[0][-1] == "install"

    def test_npm_is_invoked_through_the_node_we_fetched(self, tmp_path):
        # Not the `npm` shell wrapper: it does its own PATH lookup and could
        # find a different node, and the symlink may not survive extraction.
        run = self._run_nodejs(tmp_path)
        argv = run.call_args.args[0]
        assert argv[0].endswith("/node")
        assert argv[1].endswith("npm-cli.js")

    def test_the_token_never_reaches_the_command_line(self, tmp_path):
        # The runner streams its logs to the API and the UI.
        run = self._run_nodejs(tmp_path)
        assert not any(self.SECRET in str(a) for a in run.call_args.args[0])

    def test_a_failed_install_carries_its_exit_code(self, tmp_path):
        with pytest.raises(pulumi_deps.DependencyError) as e:
            self._run_nodejs(tmp_path, exit_code=7)
        assert e.value.exit_code == 7


class TestTheProgramsOwnNodeRange:
    def test_it_is_reported_not_honoured(self, tmp_path):
        # Honouring it would make the runtime a property of the repository
        # rather than of the platform.
        d = _program(tmp_path, "runtime: nodejs\n", package__json='{"engines":{"node":">=20"}}')
        assert pulumi_deps.package_json_engines(d) == ">=20"

    def test_absent_is_empty(self, tmp_path):
        d = _program(tmp_path, "runtime: nodejs\n", package__json="{}")
        assert pulumi_deps.package_json_engines(d) == ""

    def test_unparseable_is_empty_not_an_error(self, tmp_path):
        d = _program(tmp_path, "runtime: nodejs\n", package__json="{not json")
        assert pulumi_deps.package_json_engines(d) == ""


class TestPython:
    """A venv, because there is no ambient option (#1566).

    The root filesystem is read-only so `site-packages` cannot be written, and
    pip was removed from the image deliberately. `python -m venv` restores a
    working pip from the untouched stdlib `ensurepip`.
    """

    SECRET = "runtok-py-4c8e"

    def _cfg(self):
        return SimpleNamespace(api_url="https://terrapod.test", auth_token=self.SECRET)

    def test_python_is_supported(self, tmp_path):
        got = pulumi_deps.classify(_program(tmp_path, "runtime: python\n"))
        assert got.supported is True

    def test_a_declared_virtualenv_is_read(self, tmp_path):
        d = _program(
            tmp_path,
            "runtime:\n  name: python\n  options:\n    virtualenv: venv\n",
        )
        assert pulumi_deps.read_virtualenv(d) == "venv"

    def test_no_declaration_reads_empty(self, tmp_path):
        assert pulumi_deps.read_virtualenv(_program(tmp_path, "runtime: python\n")) == ""

    def test_the_index_url_carries_no_credential(self, tmp_path):
        # pip prints its index URL, and the runner streams its logs.
        assert "@" not in pulumi_deps.pip_index_url("https://terrapod.test")

    def test_the_credential_goes_in_a_netrc(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        path = pulumi_deps.write_netrc("https://terrapod.test", self.SECRET)
        body = path.read_text()
        assert "machine terrapod.test" in body
        assert self.SECRET in body
        assert path.stat().st_mode & 0o077 == 0

    def test_pip_writes_only_where_it_may(self, tmp_path):
        assert pulumi_deps.pip_env("https://x")["PIP_CACHE_DIR"].startswith("/tmp/")

    def test_a_plain_http_index_host_is_named_as_trusted(self):
        # pip does not fail on an untrusted HTTP index -- it silently IGNORES
        # it, and the install then dies with "No matching distribution found"
        # for a package the proxy was serving perfectly well. The runner reaches
        # the API on the in-cluster URL, which is http by default.
        assert pulumi_deps.pip_env("http://terrapod-api:8000")["PIP_TRUSTED_HOST"] == (
            "terrapod-api"
        )

    def test_an_https_api_is_left_strict(self):
        assert "PIP_TRUSTED_HOST" not in pulumi_deps.pip_env("https://terrapod.example.com")

    def _run_python(self, tmp_path, monkeypatch, *, declared=False, reqs=True, rc=(0, 0)):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        yaml = (
            "runtime:\n  name: python\n  options:\n    virtualenv: venv\n"
            if declared
            else "runtime: python\n"
        )
        d = _program(tmp_path, yaml)
        if reqs:
            (d / "requirements.txt").write_text("pulumi>=3\n")
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return MagicMock(exit_code=rc[len(calls) - 1] if len(calls) <= len(rc) else 0)

        monkeypatch.setattr(pulumi_deps.exec_subprocess, "run", fake_run)
        pulumi_deps.install(self._cfg(), d, child_grace=5, log_file="/dev/null")
        return calls

    def test_it_creates_a_venv_then_installs(self, tmp_path, monkeypatch):
        calls = self._run_python(tmp_path, monkeypatch)
        assert calls[0][1:3] == ["-m", "venv"]
        assert calls[1][1:5] == ["-m", "pip", "install", "-r"]

    def test_a_declared_virtualenv_is_built_where_the_program_says(self, tmp_path, monkeypatch):
        # Pulumi runs that interpreter and ignores PULUMI_PYTHON_CMD, so putting
        # the venv somewhere convenient would leave the program without one.
        calls = self._run_python(tmp_path, monkeypatch, declared=True)
        assert calls[0][3] == str(tmp_path / "venv")

    def test_without_one_pulumi_is_pointed_at_ours(self, tmp_path, monkeypatch):
        self._run_python(tmp_path, monkeypatch)
        import os

        assert os.environ["PULUMI_PYTHON_CMD"].endswith("/bin/python")

    def test_a_declared_virtualenv_is_not_overridden(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PULUMI_PYTHON_CMD", raising=False)
        self._run_python(tmp_path, monkeypatch, declared=True)
        import os

        assert "PULUMI_PYTHON_CMD" not in os.environ

    def test_no_requirements_is_not_an_error(self, tmp_path, monkeypatch):
        calls = self._run_python(tmp_path, monkeypatch, reqs=False)
        assert len(calls) == 1  # the venv, and nothing else

    def test_a_failed_venv_stops_before_installing(self, tmp_path, monkeypatch):
        with pytest.raises(pulumi_deps.DependencyError, match="virtualenv"):
            self._run_python(tmp_path, monkeypatch, rc=(3,))

    def test_a_failed_install_carries_its_exit_code(self, tmp_path, monkeypatch):
        with pytest.raises(pulumi_deps.DependencyError) as e:
            self._run_python(tmp_path, monkeypatch, rc=(0, 5))
        assert e.value.exit_code == 5
