"""Installing a run's collections through Terrapod's Galaxy proxy (#1931).

`ansible-core` ships no collections, so a playbook using anything beyond the
builtins needs them fetched before it runs — and in a restricted network the only
place they can come from is Terrapod.

One class here **drives real `ansible-galaxy`**. The rest of this module's output
is configuration an external tool consumes, and a test that asserts on the shape
of an environment variable proves nothing about whether the tool honours it: the
value can be perfectly well-formed, agree with every neighbouring setting, and
still configure something that cannot work. That is how a release shipped a
Pulumi plugin downloader pointed at a cache it never sent a credential to
(#1906), with every shape assertion green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from terrapod.runner.phases import ansible_collections as ac

API = "https://terrapod.test"
TOKEN = "runtok:run-1:apply:3600:0:deadbeef"


@pytest.fixture(autouse=True)
def _ee_install_dirs(tmp_path_factory, monkeypatch):
    """Point the install targets somewhere that exists in a test.

    They default to `/tp/...`, the volume shared with the execution-environment
    container, which exists only inside a runner Job -- so without this every
    test that reaches `install` fails on `mkdir` rather than on the thing it is
    about. Setting the environment rather than patching the functions is
    deliberate: it is the same channel the Job uses, so these tests exercise how
    the paths are actually resolved.
    """
    base = tmp_path_factory.mktemp("ee-dirs")
    monkeypatch.setenv("TP_EE_COLLECTIONS_DIR", str(base / "collections"))
    monkeypatch.setenv("TP_EE_ROLES_DIR", str(base / "roles"))


def _cfg(api_url: str = API, token: str = TOKEN):
    return SimpleNamespace(api_url=api_url, auth_token=token)


class TestTheServerURL:
    def test_it_is_the_galaxy_proxy_with_a_trailing_slash(self) -> None:
        """The slash is not cosmetic: the client requests this URL as given for
        version discovery and only falls back to appending `/api/` when that
        call fails."""
        assert ac.galaxy_server_url(API) == (
            "https://terrapod.test/api/terrapod/v1/package-cache/galaxy/"
        )

    def test_a_trailing_slash_on_the_api_url_is_not_doubled(self) -> None:
        assert ac.galaxy_server_url("https://terrapod.test/") == ac.galaxy_server_url(API)

    def test_the_credential_is_not_in_it(self) -> None:
        """The runner tees its output to the API and the UI, and ansible-galaxy
        prints the server URL it is talking to."""
        assert TOKEN not in ac.galaxy_server_url(API)


class TestTheEnvironment:
    def test_the_server_is_named_and_pointed_at_us(self) -> None:
        env = ac.galaxy_env(API, TOKEN, base={})
        assert env["ANSIBLE_GALAXY_SERVER_LIST"] == "terrapod"
        assert env["ANSIBLE_GALAXY_SERVER_TERRAPOD_URL"] == ac.galaxy_server_url(API)
        assert env["ANSIBLE_GALAXY_SERVER_TERRAPOD_TOKEN"] == TOKEN

    def test_only_one_server_is_offered(self) -> None:
        """A runner is the case that must work with no route out, so a miss is an
        error rather than a quiet upstream fetch."""
        assert "," not in ac.galaxy_env(API, TOKEN, base={})["ANSIBLE_GALAXY_SERVER_LIST"]

    def test_an_inherited_galaxy_server_cannot_redirect_the_supply_chain(self) -> None:
        base = {
            "ANSIBLE_GALAXY_SERVER_LIST": "evil,terrapod",
            "ANSIBLE_GALAXY_SERVER_EVIL_URL": "https://evil.example/",
            "ANSIBLE_GALAXY_SERVER_EVIL_TOKEN": "x",
        }
        env = ac.galaxy_env(API, TOKEN, base=base)
        assert env["ANSIBLE_GALAXY_SERVER_LIST"] == "terrapod"
        assert "ANSIBLE_GALAXY_SERVER_EVIL_URL" not in env
        assert "ANSIBLE_GALAXY_SERVER_EVIL_TOKEN" not in env

    def test_the_operators_other_ansible_settings_SURVIVE(self) -> None:
        """The inventory resolver strips the whole `ANSIBLE_*` prefix, and that is
        right in the API pod where no operator-supplied environment exists. On a
        runner it is where a workspace's own environment variables arrive, so
        blanket-stripping would silently discard what the operator set."""
        env = ac.galaxy_env(API, TOKEN, base={"ANSIBLE_FORKS": "25", "ANSIBLE_STRATEGY": "free"})
        assert env["ANSIBLE_FORKS"] == "25"
        assert env["ANSIBLE_STRATEGY"] == "free"

    def test_a_vendored_collections_path_is_kept_after_ours(self) -> None:
        """A repository that vendors collections sets this itself; discarding it
        would stop ansible finding them. Ours has to be on the path too, or `-p`
        installs successfully into somewhere nothing reads."""
        env = ac.galaxy_env(API, TOKEN, base={"ANSIBLE_COLLECTIONS_PATH": "/repo/collections"})
        assert env["ANSIBLE_COLLECTIONS_PATH"] == f"{ac.collections_dir()}:/repo/collections"

    def test_an_operators_ansible_home_wins(self) -> None:
        env = ac.galaxy_env(API, TOKEN, base={"ANSIBLE_HOME": "/repo/.ansible"})
        assert env["ANSIBLE_HOME"] == "/repo/.ansible"

    def test_a_vendored_roles_path_is_kept_after_ours(self) -> None:
        env = ac.galaxy_env(API, TOKEN, base={"ANSIBLE_ROLES_PATH": "/repo/roles"})
        assert env["ANSIBLE_ROLES_PATH"] == f"{ac.roles_dir()}:/repo/roles"

    def test_roles_and_collections_paths_are_distinct(self) -> None:
        env = ac.galaxy_env(API, TOKEN, base={})
        assert env["ANSIBLE_ROLES_PATH"] != env["ANSIBLE_COLLECTIONS_PATH"]

    def test_a_writable_home_is_supplied_when_they_set_none(self) -> None:
        """The root filesystem is read-only."""
        assert ac.galaxy_env(API, TOKEN, base={})["ANSIBLE_HOME"] == "/tmp/ansible-home"


class TestWhichRequirementsAreInstalled:
    def test_nothing_declared_is_not_an_error(self, tmp_path: Path) -> None:
        """The common case for a playbook using only `ansible.builtin`."""
        assert ac.requirements_files(tmp_path) == []

    def test_the_directory_form_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "collections").mkdir()
        f = tmp_path / "collections" / "requirements.yml"
        f.write_text("collections: []\n")
        assert ac.requirements_files(tmp_path) == [f]

    def test_the_flat_form_is_found(self, tmp_path: Path) -> None:
        f = tmp_path / "requirements.yml"
        f.write_text("collections: []\n")
        assert ac.requirements_files(tmp_path) == [f]

    def test_both_are_installed_in_a_fixed_order(self, tmp_path: Path) -> None:
        """Neither shadows the other: a repository holding both means both, and
        the order is the declared precedence rather than whatever the filesystem
        happens to return."""
        (tmp_path / "collections").mkdir()
        nested = tmp_path / "collections" / "requirements.yml"
        flat = tmp_path / "requirements.yml"
        for p in (flat, nested):
            p.write_text("collections: []\n")
        assert ac.requirements_files(tmp_path) == [nested, flat]

    def test_a_directory_named_like_the_file_is_not_a_requirements_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "requirements.yml").mkdir()
        assert ac.requirements_files(tmp_path) == []


class TestRolesAndCollectionsAreTwoMechanisms:
    """Collections come through Terrapod; roles come from git.

    Measured against ansible-core 2.21.5: `role install -r` and
    `collection install -r` each ignore the other's section, and against a file
    carrying only the other's they print "Skipping install, no requirements
    found" and exit 0. So both run unconditionally, which is what lets this
    phase support both without a YAML parser the runner image does not carry.

    A `roles:` section is NOT refused. An earlier draft of this phase refused
    one on the grounds that a git-sourced role needed credentials the phase did
    not have. That was wrong: `git_auth` materialises `git_http_auth` /
    `git_ssh_auth` into git's own config with `insteadOf` rewrites before
    anything fetches, so a role clone inherits all of it. Fetching from private
    git is a solved problem here.
    """

    def _req(self, tmp_path: Path, body: str) -> Path:
        f = tmp_path / "requirements.yml"
        f.write_text(body)
        return f

    def _argvs(self, run) -> list[list[str]]:
        # The full argv, binary first. `ee_client.run` takes the entry point and
        # its arguments as two parameters rather than one list -- the agent
        # resolves the NAME inside the execution environment, where a path from
        # here would mean nothing -- so this rejoins them, and every index below
        # keeps meaning what it meant when the runner exec'd it directly.
        return [[c.args[0], *c.args[1]] for c in run.call_args_list]

    def test_both_subcommands_run_for_one_requirements_file(self, tmp_path: Path) -> None:
        self._req(
            tmp_path, "roles:\n  - src: git+ssh://git@host/r.git\ncollections:\n  - name: a.b\n"
        )
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(_cfg(), tmp_path)
        argvs = self._argvs(run)
        assert len(argvs) == 2, argvs
        assert argvs[0][1:3] == ["role", "install"], argvs[0]
        assert argvs[1][1:3] == ["collection", "install"], argvs[1]

    def test_roles_are_installed_before_collections(self, tmp_path: Path) -> None:
        """Not cosmetic: a collection may be a dependency of nothing, but a role
        a playbook needs should be on disk before anything that could fail."""
        self._req(tmp_path, "roles:\n  - src: git+ssh://git@host/r.git\n")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(_cfg(), tmp_path)
        assert self._argvs(run)[0][1] == "role"

    def test_no_cache_is_only_on_the_collection_command(self, tmp_path: Path) -> None:
        """`ansible-galaxy install`/`role install` do not accept `--no-cache`;
        passing it would fail the whole invocation with an argparse error."""
        self._req(tmp_path, "collections:\n  - name: a.b\n")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(_cfg(), tmp_path)
        role, coll = self._argvs(run)
        assert "--no-cache" not in role, role
        assert "--no-cache" in coll, coll

    def test_roles_and_collections_land_in_separate_trees(self, tmp_path: Path) -> None:
        """ansible keeps the two search paths separate; a role installed into the
        collections tree is not found."""
        self._req(tmp_path, "collections:\n  - name: a.b\n")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(_cfg(), tmp_path)
        role, coll = self._argvs(run)
        assert str(ac.roles_dir()) in role and str(ac.collections_dir()) not in role
        assert str(ac.collections_dir()) in coll and str(ac.roles_dir()) not in coll

    def test_a_failing_role_install_names_the_roles(self, tmp_path: Path) -> None:
        f = self._req(tmp_path, "roles:\n  - src: git+ssh://git@host/r.git\n")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=3, error="")
            with pytest.raises(ac.CollectionInstallError) as e:
                ac.install(_cfg(), tmp_path)
        assert "roles" in str(e.value) and str(f) in str(e.value)
        assert e.value.exit_code == 3


class TestTheWorkspacesOwnCollections:
    """The platform's list, installed last so it overrides."""

    def _argvs(self, run) -> list[list[str]]:
        # The full argv, binary first. `ee_client.run` takes the entry point and
        # its arguments as two parameters rather than one list -- the agent
        # resolves the NAME inside the execution environment, where a path from
        # here would mean nothing -- so this rejoins them, and every index below
        # keeps meaning what it meant when the runner exec'd it directly.
        return [[c.args[0], *c.args[1]] for c in run.call_args_list]

    def _cfg_with(self, declared: list[str]):
        return SimpleNamespace(api_url=API, auth_token=TOKEN, ansible_collections=declared)

    def test_they_are_installed_with_force_and_last(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.yml").write_text("collections:\n  - name: a.b\n")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(self._cfg_with(["a.b:2.0.0"]), tmp_path)
        argvs = self._argvs(run)
        assert len(argvs) == 3, argvs
        assert "--force" not in argvs[1], "the configuration's own install must not force"
        assert "--force" in argvs[2] and "a.b:2.0.0" in argvs[2], argvs[2]

    def test_they_install_with_no_requirements_file_at_all(self, tmp_path: Path) -> None:
        """A workspace may declare collections for a configuration that declares
        none of its own."""
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(self._cfg_with(["a.b"]), tmp_path)
        argvs = self._argvs(run)
        assert len(argvs) == 1 and "a.b" in argvs[0], argvs

    def test_nothing_runs_when_nothing_is_declared(self, tmp_path: Path) -> None:
        with patch.object(ac.ee_client, "run") as run:
            ac.install(self._cfg_with([]), tmp_path)
        run.assert_not_called()

    def test_two_requirements_files_are_refused_before_anything_runs(self, tmp_path: Path) -> None:
        (tmp_path / "collections").mkdir()
        (tmp_path / "collections" / "requirements.yml").write_text("collections: []\n")
        (tmp_path / "requirements.yml").write_text("collections: []\n")
        with patch.object(ac.ee_client, "run") as run:
            with pytest.raises(ac.CollectionInstallError) as e:
                ac.install(_cfg(), tmp_path)
        run.assert_not_called()
        assert "more than one place" in str(e.value)


needs_ansible = pytest.mark.skipif(
    shutil.which("ansible-galaxy") is None,
    reason="needs a real ansible-galaxy to drive; the point of this class is the tool",
)


@needs_ansible
class TestRealAnsibleGalaxyHonoursTheEnvironment:
    """The claim is that this environment configures `ansible-galaxy`. Only the
    tool can say so.

    These assert against a server that is *not there*: the proof is which URL the
    client reports trying, which is the one thing a shape assertion cannot give.
    """

    def _run(self, env_extra: dict[str, str], cwd: Path) -> str:
        env = ac.galaxy_env("http://127.0.0.1:9", TOKEN, base={})
        env.update(env_extra)
        env.setdefault("PATH", os.environ.get("PATH", ""))
        proc = subprocess.run(
            [
                shutil.which("ansible-galaxy") or "ansible-galaxy",
                "collection",
                "install",
                "terrapodtest.nonexistent",
                "-p",
                str(cwd / "c"),
                "--no-cache",
            ],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.stdout + proc.stderr

    def test_it_resolves_against_terrapod_and_not_upstream(self, tmp_path: Path) -> None:
        out = self._run({}, tmp_path)
        assert "127.0.0.1:9" in out, out
        assert "galaxy.ansible.com" not in out, out

    def test_the_configured_path_is_the_one_it_calls_first(self, tmp_path: Path) -> None:
        """Our discovery route is `/galaxy/`, and the client requests the URL as
        given before trying anything else. The `/galaxy/api` that also appears is
        its post-failure fallback, which is why a real error from this client
        names a path Terrapod never served."""
        out = self._run({}, tmp_path)
        assert "/package-cache/galaxy/" in out, out

    def test_an_inherited_server_list_does_not_survive(self, tmp_path: Path) -> None:
        """The supply-chain property, driven rather than asserted: ansible is
        handed an environment that also carried a hostile server, and must not
        contact it."""
        env = ac.galaxy_env(
            "http://127.0.0.1:9",
            TOKEN,
            base={
                "ANSIBLE_GALAXY_SERVER_LIST": "evil",
                "ANSIBLE_GALAXY_SERVER_EVIL_URL": "http://127.0.0.2:9/evil/",
            },
        )
        env.setdefault("PATH", os.environ.get("PATH", ""))
        proc = subprocess.run(
            [
                shutil.which("ansible-galaxy") or "ansible-galaxy",
                "collection",
                "install",
                "terrapodtest.nonexistent",
                "-p",
                str(tmp_path / "c"),
                "--no-cache",
            ],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = proc.stdout + proc.stderr
        assert "127.0.0.2" not in out, out
        assert "127.0.0.1:9" in out, out
