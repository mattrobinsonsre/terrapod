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
    # And the EE's HOME, for the same reason: the Python-requirements install
    # writes pip's credential there, so without this it fails on a read-only
    # `/tp` rather than on whatever the test is about (#2305).
    monkeypatch.setenv("TP_EE_HOME", str(base / "home"))


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


#: Exactly what `galaxy_env` may hand the execution environment (#2305). Named
#: here rather than derived from the function, so a key added there fails a test
#: rather than being blessed by the gate meant to bound it.
_ALLOWED_KEYS = {
    "ANSIBLE_GALAXY_SERVER_LIST",
    f"ANSIBLE_GALAXY_SERVER_{ac.SERVER_NAME.upper()}_URL",
    f"ANSIBLE_GALAXY_SERVER_{ac.SERVER_NAME.upper()}_TOKEN",
    "ANSIBLE_COLLECTIONS_PATH",
    "ANSIBLE_ROLES_PATH",
}


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

    def test_nothing_else_from_this_process_crosses_into_the_EE(self) -> None:
        """Supersedes `test_the_operators_other_ansible_settings_SURVIVE` (#2305).

        **Half of that test's reason still holds and half of it was wrong.** It
        argued that blanket-stripping would discard what the operator set, which
        is true of the API pod's resolver and false here: a workspace's own
        environment variables arrive in the ORCHESTRATOR, and this env is handed
        to the EE, a different container with a different filesystem. So
        forwarding them was not preserving the operator's settings -- for the
        galaxy install it was shipping one container's paths into another's, and
        `configure._forced_env` strips every inherited `ANSIBLE_*` before the
        playbook runs anyway, so these never reached ansible by this route.

        What it cost: `HOME=/home/runner` overrode the EE's own `HOME=/tp/home`,
        and `ansible-galaxy` exited 1 with ENOENT because the EE has no such
        directory (its passwd home is `/home/ansible`).
        """
        leaky = {
            "ANSIBLE_FORKS": "25",
            "ANSIBLE_STRATEGY": "free",
            "HOME": "/home/runner",
            "TMPDIR": "/tmp",
            "ANSIBLE_LOCAL_TEMP": "/tmp/ansible-local",
        }
        assert set(ac.galaxy_env(API, TOKEN, base=leaky)) == _ALLOWED_KEYS

    def test_the_allow_list_is_exactly_these_keys(self) -> None:
        """A floor AND a membership assertion, because a set-iterating gate also
        passes when the set merely SHRINKS. The two search paths are the only
        values read off the caller's environment; everything else is ours."""
        env = ac.galaxy_env(API, TOKEN, base={})
        assert set(env) == _ALLOWED_KEYS
        assert len(_ALLOWED_KEYS) == 5

    def test_a_vendored_collections_path_is_kept_after_ours(self) -> None:
        """A repository that vendors collections sets this itself; discarding it
        would stop ansible finding them. Ours has to be on the path too, or `-p`
        installs successfully into somewhere nothing reads."""
        env = ac.galaxy_env(API, TOKEN, base={"ANSIBLE_COLLECTIONS_PATH": "/repo/collections"})
        assert env["ANSIBLE_COLLECTIONS_PATH"] == f"{ac.collections_dir()}:/repo/collections"

    def test_no_ansible_home_is_set_at_all(self) -> None:
        """Supersedes `test_a_writable_home_is_supplied_when_they_set_none` and
        `test_an_operators_ansible_home_wins` (#2305).

        The old default was `/tmp/ansible-home`: the orchestrator's writable
        `tmp` emptyDir, and the EE's **read-only root**. Measured against the
        published image with a read-only root and the Job's own env:

            ANSIBLE_HOME=/tmp/ansible-home   rc=1  `[Errno 30] Read-only file
                                                    system: /tmp/ansible-home`
            ANSIBLE_HOME unset               rc=0  cache at
                                                    /tp/home/.ansible/galaxy_cache

        So no collection could be installed at all. `ANSIBLE_LOCAL_TEMP` does
        not rescue it -- the Job already points that at the shared volume and
        galaxy still fails, because what it needs is `ANSIBLE_HOME/galaxy_cache`.

        Setting nothing is the fix: ansible derives it from `~`, and the EE's own
        `HOME` is on the shared volume. The operator-wins half of the superseded
        test described a capability that did not exist end to end either, since
        `configure._forced_env` strips an inherited `ANSIBLE_HOME` regardless.
        """
        assert "ANSIBLE_HOME" not in ac.galaxy_env(API, TOKEN, base={})
        assert "ANSIBLE_HOME" not in ac.galaxy_env(
            API, TOKEN, base={"ANSIBLE_HOME": "/repo/.ansible"}
        )

    def test_a_vendored_roles_path_is_kept_after_ours(self) -> None:
        env = ac.galaxy_env(API, TOKEN, base={"ANSIBLE_ROLES_PATH": "/repo/roles"})
        assert env["ANSIBLE_ROLES_PATH"] == f"{ac.roles_dir()}:/repo/roles"

    def test_roles_and_collections_paths_are_distinct(self) -> None:
        env = ac.galaxy_env(API, TOKEN, base={})
        assert env["ANSIBLE_ROLES_PATH"] != env["ANSIBLE_COLLECTIONS_PATH"]

    def test_no_value_names_a_path_outside_the_shared_volume(self) -> None:
        """The property both defects violated, stated once.

        Every path this env names has to exist in the EE, and the only paths that
        do are on the shared volume -- or the operator's own, which they chose
        and which is theirs to get right.
        """
        env = ac.galaxy_env(API, TOKEN, base={})
        for key, value in env.items():
            if value.startswith("/"):
                assert value.startswith(str(ac.collections_dir().parent)), (
                    f"{key}={value} names a path outside the shared volume, so it is "
                    "this process's filesystem rather than the execution environment's"
                )


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


class TestThePythonRequirementsOfInstalledCollections:
    """A collection's Python dependencies, installed through the cache (#2305).

    `ansible-galaxy` installs a collection's CONTENT and not its Python
    requirements, so `amazon.aws` installs cleanly and then fails at the first
    task -- or, for a dynamic inventory, at plugin load -- with an import error
    naming boto3. Nothing in the tree installed them, and nothing pointed the
    execution environment's pip at Terrapod's pull-through cache, so a sealed
    deployment could not have satisfied one even by hand.

    These drive `install_python_requirements` through `ee_client.run`, which is
    the boundary: the interpreter that must import the package is the EE's, so
    an install in the orchestrator would succeed and change nothing.
    """

    def _collection(self, ns: str, name: str, body: str = "boto3>=1.38\n") -> Path:
        d = ac.collections_dir() / "ansible_collections" / ns / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / ac.PYTHON_REQUIREMENTS
        f.write_text(body)
        return f

    def _pip_argvs(self, run) -> list[list[str]]:
        return [[c.args[0], *c.args[1]] for c in run.call_args_list if c.args[0] == "pip"]

    # -- which files are installed, and in what order ----------------------

    def test_each_installed_collection_contributes_its_own(self, tmp_path: Path) -> None:
        a = self._collection("amazon", "aws")
        b = self._collection("community", "general", "netaddr\n")
        assert ac.python_requirements_files(tmp_path) == sorted([a, b])

    def test_the_operators_own_file_is_installed_last(self, tmp_path: Path) -> None:
        """Last because pip's later install wins a conflicting pin, which is the
        same relationship `--force` expresses for the workspace's collections."""
        self._collection("amazon", "aws")
        declared = tmp_path / ac.PYTHON_REQUIREMENTS
        declared.write_text("requests\n")
        assert ac.python_requirements_files(tmp_path)[-1] == declared

    def test_the_collection_pass_is_sorted(self, tmp_path: Path) -> None:
        """Two collections pinning one package differently is the operator's to
        resolve; an order that varied per run would make it intermittent."""
        self._collection("zz", "last")
        self._collection("aa", "first")
        found = ac.python_requirements_files(tmp_path)
        assert [p.parent.parent.name for p in found] == ["aa", "zz"]

    def test_a_requirements_txt_that_is_a_directory_is_not_a_file(self, tmp_path: Path) -> None:
        (tmp_path / ac.PYTHON_REQUIREMENTS).mkdir()
        assert ac.python_requirements_files(tmp_path) == []

    def test_nothing_declared_runs_no_pip_at_all(self, tmp_path: Path) -> None:
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install_python_requirements(_cfg(), tmp_path)
        assert self._pip_argvs(run) == []

    # -- how pip is configured ---------------------------------------------

    def test_the_cache_is_on_the_shared_volume_and_not_tmp(self) -> None:
        """`/tmp` is the ORCHESTRATOR's writable emptyDir and the EE's read-only
        root -- the same mistake this issue is about, one directory over."""
        env = ac.pip_env(API)
        assert env["PIP_CACHE_DIR"] == str(ac.ee_home() / ".cache" / "pip")
        assert "/tmp" not in env["PIP_CACHE_DIR"]

    def test_the_index_is_terrapods_pull_through_cache(self) -> None:
        from terrapod.runner.phases import pulumi_deps

        assert ac.pip_env(API)["PIP_INDEX_URL"] == pulumi_deps.pip_index_url(API)

    def test_a_plain_http_api_is_named_as_trusted(self) -> None:
        """pip does not FAIL on an untrusted plain-HTTP index, it ignores it --
        and then reports "No matching distribution found" for a package the
        proxy was serving. Inherited from the shared helper, asserted here
        because this is the caller whose API URL is http in-cluster."""
        assert ac.pip_env("http://terrapod-api:8000")["PIP_TRUSTED_HOST"] == "terrapod-api"
        assert "PIP_TRUSTED_HOST" not in ac.pip_env("https://terrapod.test")

    def test_the_install_is_user_scoped(self) -> None:
        """The EE's root filesystem is read-only, so site-packages cannot be
        written. `--user` needs no PYTHONPATH: the user site is on `sys.path`
        already and HOME is on the writable shared volume."""
        assert ac.pip_env(API)["PIP_USER"] == "1"

    def test_the_credential_goes_in_the_execution_environments_home(self, tmp_path: Path) -> None:
        """Not this process's HOME: pip runs in the other container, so a file
        in the orchestrator's home is one the EE never looks at -- and the
        failure is silent, because pip without a credential simply 401s."""
        self._collection("amazon", "aws")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install_python_requirements(_cfg(), tmp_path)
        netrc = ac.ee_home() / ".netrc"
        assert netrc.is_file()
        assert TOKEN in netrc.read_text()
        assert netrc.stat().st_mode & 0o777 == 0o600

    def test_the_token_is_not_on_the_argv(self, tmp_path: Path) -> None:
        self._collection("amazon", "aws")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install_python_requirements(_cfg(), tmp_path)
        for argv in self._pip_argvs(run):
            assert TOKEN not in " ".join(argv), argv

    # -- a missing pip is skipped; a failure is fatal ----------------------

    def test_a_missing_pip_is_skipped_rather_than_fatal(self, tmp_path: Path) -> None:
        """pip is NOT part of the custom-EE contract, so an operator-supplied
        image may legitimately not have one and requiring it at the startup
        probe would fail an image that conforms today (user's call, #2305)."""
        f = self._collection("amazon", "aws")
        calls: list[list[str]] = []

        def _run(binary, args, **kw):
            calls.append([binary, *args])
            if args[:1] == ["--version"]:
                return SimpleNamespace(rc=127, error="pip: not found")
            return SimpleNamespace(rc=0, error="")

        with patch.object(ac.ee_client, "run", side_effect=_run):
            ac.install_python_requirements(_cfg(), tmp_path)  # does not raise
        assert calls == [["pip", "--version"]], calls
        assert str(f) not in " ".join(c for argv in calls for c in argv)

    def test_a_failing_install_is_fatal_and_carries_the_exit_code(self, tmp_path: Path) -> None:
        """A missing SDK fails the run anyway, later and less legibly: a partial
        configure against real hosts is the outcome worth refusing over."""
        f = self._collection("amazon", "aws")

        def _run(binary, args, **kw):
            if args[:1] == ["--version"]:
                return SimpleNamespace(rc=0, error="")
            return SimpleNamespace(rc=2, error="")

        with patch.object(ac.ee_client, "run", side_effect=_run):
            with pytest.raises(ac.CollectionInstallError) as e:
                ac.install_python_requirements(_cfg(), tmp_path)
        assert str(f) in str(e.value)
        assert e.value.exit_code == 2

    def test_an_agent_error_is_fatal_and_names_it(self, tmp_path: Path) -> None:
        """`error` is the agent failing to run the child at all, which is a
        different thing from the child exiting non-zero."""
        self._collection("amazon", "aws")

        def _run(binary, args, **kw):
            if args[:1] == ["--version"]:
                return SimpleNamespace(rc=0, error="")
            return SimpleNamespace(rc=1, error="the sidecar is gone")

        with patch.object(ac.ee_client, "run", side_effect=_run):
            with pytest.raises(ac.CollectionInstallError) as e:
                ac.install_python_requirements(_cfg(), tmp_path)
        assert "the sidecar is gone" in str(e.value)

    # -- the call site -----------------------------------------------------

    def test_install_satisfies_them_after_the_collections_are_on_disk(self, tmp_path: Path) -> None:
        """It READS the installed tree, so it cannot be folded into either
        Galaxy pass: a collection's own `requirements.txt` exists only once the
        collection is there."""
        (tmp_path / "requirements.yml").write_text("collections:\n  - name: a.b\n")
        order: list[str] = []

        def _run(binary, args, **kw):
            order.append(f"{binary} {args[0] if args else ''}")
            if binary == "ansible-galaxy":
                # Only now does the tree a collection's file would be found in
                # exist, which is the whole reason for the ordering.
                self._collection("amazon", "aws")
            return SimpleNamespace(rc=0, error="")

        with patch.object(ac.ee_client, "run", side_effect=_run):
            ac.install(_cfg(), tmp_path)
        assert order[-1] == "pip install", order
        assert order.index("pip --version") > order.index("ansible-galaxy collection"), order

    def test_the_operators_file_is_installed_with_no_galaxy_requirements_at_all(
        self, tmp_path: Path
    ) -> None:
        """An operator may declare a `requirements.txt` and no Galaxy
        requirements -- a `uri` task needing a client, a filter plugin of their
        own -- and an early return would install nothing while the file sat
        there looking honoured."""
        (tmp_path / ac.PYTHON_REQUIREMENTS).write_text("requests\n")
        with patch.object(ac.ee_client, "run") as run:
            run.return_value = SimpleNamespace(rc=0, error="")
            ac.install(_cfg(), tmp_path)
        argvs = self._pip_argvs(run)
        assert ["pip", "install"] == argvs[-1][:2], argvs
        assert str(tmp_path / ac.PYTHON_REQUIREMENTS) in argvs[-1]
