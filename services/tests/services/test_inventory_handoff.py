"""The configure inventory handoff: fetch on change, never on run (#2021).

The property under test is an ABSENCE -- that launching a configure fetches
nothing from a VCS provider -- so every test here either booby-traps the fetch
path and proves resolution still works, or reads the module's own call graph.
A test that merely asserts the right document comes out would pass just as well
with a fetch in the middle, which is the thing this exists to prevent.

The git half comes from the run's configuration version, which the poller
already collected when the code changed (`vcs_poller` passes `configure_enabled`
and `config_directory` into the fetch, and `git_fetch.workspace_fetch_paths`
puts the config root in the tarball, #2117). That is also the only shape that
keeps the inventory and the playbook on one commit: `_fetch_vcs_source` resolves
"the commit the bound branch currently points at", so a push between a run's CV
being built and its configure launching would split them silently.

**Patched at the SOURCE modules, not on `inventory_resolve`.** `get_storage` and
`materialize_archive` are imported inside the functions that use them, so
`monkeypatch.setattr(inventory_resolve, "get_storage", ...)` raises
`AttributeError` and a patch that appears to work would be patching nothing.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import pathlib
import shutil
import subprocess
import tarfile
import uuid

import pytest

from terrapod.services import inventory_resolve


def _working_ansible() -> str | None:
    """A usable `ansible-inventory`, or None. Probed, not merely located.

    Same guard as `test_inventory_resolve.py`, and for the same reason: a stale
    shim on a developer's machine must not fail the suite, while the test image
    gates its own install at BUILD time so CI genuinely runs these.
    """
    found = shutil.which("ansible-inventory")
    if found is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 -- the path came from `which`
            [found, "--version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.returncode == 0 else None


_ANSIBLE = _working_ansible()
needs_ansible = pytest.mark.skipif(
    _ANSIBLE is None,
    reason="no WORKING ansible-inventory on PATH; the test image installs one",
)


def _archive(files: dict[str, str], *, wrapper: str = "owner-repo-abc123") -> bytes:
    """A provider-shaped archive: every path under one commit-named directory.

    That wrapper is what `_extract` unwraps, and the CV carries it because the
    poller copies the archive-cache tarball verbatim rather than re-taring --
    `put_stream(cv_key, _file_chunks(path))` after `verify_archive`. Building
    the fixture the same way is what makes this test about the real bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"{wrapper}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Store:
    """The objects a test planted, and every key anyone asked for."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.written: dict[str, bytes] = {}
        self.requested: list[str] = []

    async def put(self, key, data, content_type="application/octet-stream", metadata=None):
        self.written[key] = data
        return None

    async def get_stream(self, key):
        self.requested.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        yield self.objects[key]


class _Workspace:
    def __init__(self, *, configure_enabled=True, config_directory="ansible"):
        self.id = uuid.uuid4()
        self.configure_enabled = configure_enabled
        self.config_directory = config_directory
        self.inventory_include_platform = True
        self.inventory_ignore_paths: list[str] = []
        self.inventory_paths: list[str] = ["hosts.yml"]
        self.ansible_version = ""
        self.vcs_connection_id = uuid.uuid4()
        self.vcs_repo_url = "https://github.com/owner/repo"


class _Run:
    def __init__(self, ws, *, cv_id=None):
        self.id = uuid.uuid4()
        self.workspace_id = ws.id
        self.configuration_version_id = cv_id


@contextlib.contextmanager
def _trapped(monkeypatch, store):
    """Storage serving `store`, and every VCS fetch route made fatal."""
    import terrapod.services.vcs_archive_cache as vac
    import terrapod.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_storage", lambda: store)

    @contextlib.asynccontextmanager
    async def _materialize(key):
        chunks = [c async for c in store.get_stream(key)]
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".tar.gz")
        with os.fdopen(fd, "wb") as fh:
            for c in chunks:
                fh.write(c)
        try:
            yield path
        finally:
            os.unlink(path)

    monkeypatch.setattr(vac, "materialize_archive", _materialize)

    async def _never_fetch(*a, **kw):
        raise AssertionError(
            "the run path fetched from a VCS provider -- #2021 requires the git "
            "half to come from the run's configuration version, which the poller "
            "already collected"
        )

    monkeypatch.setattr(inventory_resolve, "_fetch_vcs_source", _never_fetch)
    monkeypatch.setattr(inventory_resolve, "_resolve_git_sha", _never_fetch)

    # A known binary rather than whatever this machine's PATH holds: what is
    # under test is where the INVENTORY comes from, not which ansible runs.
    # Synchronous and argument-free, matching the real lookup -- the API's
    # ansible is baked into its image, so there is nothing to await and no
    # version to pass.
    monkeypatch.setattr(inventory_resolve, "ansible_inventory_binary", lambda: _ANSIBLE)
    yield


class TestTheRunPathNeverFetches:
    """The goal in one class: on a run, nothing reaches a VCS provider."""

    def test_the_run_path_hands_the_resolver_no_sha_to_fetch(self):
        """Structural, and narrower than it first looks -- deliberately.

        The obvious gate, "the call graph from `resolve_for_run` cannot reach
        `_fetch_vcs_source`", is UNSATISFIABLE and I wrote it before checking:
        both paths share `_resolve_uncached`, so the fetch is statically
        reachable from either and only the DATA decides. The property is
        therefore about the argument, not the graph: the run path passes an
        empty `git_sha`, so the branch that fetches cannot be taken.

        Asserted on the keyword's VALUE rather than its presence, because
        `git_sha=something` would satisfy a presence check while fetching.
        """
        src = pathlib.Path(inventory_resolve.__file__).read_text()
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "resolve_for_run"
        )
        calls = [
            c
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and getattr(c.func, "id", getattr(c.func, "attr", None)) == "_resolve_uncached"
        ]
        assert len(calls) == 1, f"expected one resolver call, found {len(calls)}"
        kwargs = {k.arg: k.value for k in calls[0].keywords}
        assert "git_sha" in kwargs, "the run path stopped passing git_sha explicitly"
        assert isinstance(kwargs["git_sha"], ast.Constant) and kwargs["git_sha"].value == "", (
            "resolve_for_run must pass an EMPTY git_sha: a non-empty one takes the "
            "branch that fetches from the VCS provider at run time"
        )
        # Its VALUE, not its presence: `config_version_id=None` keeps the
        # keyword and takes the fetch branch, which is precisely the mutation a
        # presence check waves through.
        assert "config_version_id" in kwargs, "the run path stopped passing the CV"
        assert isinstance(kwargs["config_version_id"], ast.Name), (
            "config_version_id must be a variable the function computed, not a "
            "constant: a literal None means the run path resolves no CV at all"
        )

    def test_the_fetch_path_is_still_reachable_for_the_READ_endpoint(self):
        """The other half, so the gate above cannot be satisfied by deleting the
        fetch outright. A workspace with no runs still has an inventory to show,
        and `GET .../inventory/resolved` answers it as of the branch head.
        """
        src = pathlib.Path(inventory_resolve.__file__).read_text()
        tree = ast.parse(src)
        fns = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "resolve" in fns, "the read entry point moved or was renamed"

        reached: set[str] = set()
        queue = ["resolve"]
        while queue:
            name = queue.pop()
            if name in reached or name not in fns:
                continue
            reached.add(name)
            for call in ast.walk(fns[name]):
                if isinstance(call, ast.Call):
                    f = call.func
                    target = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                    if target in fns:
                        queue.append(target)

        assert {"_fetch_vcs_source", "_resolve_git_sha"} <= reached, (
            "the READ path no longer reaches a fetch: a workspace with no runs "
            "still has an inventory to show, as of the branch head"
        )
        assert "_resolve_uncached" in reached, reached

    @needs_ansible
    def test_it_resolves_from_the_configuration_version(self, monkeypatch):
        """The whole mechanism, with every fetch route fatal."""
        from terrapod.storage.keys import config_version_key

        ws = _Workspace()
        cv_id = uuid.uuid4()
        run = _Run(ws, cv_id=cv_id)
        store = _Store(
            {
                config_version_key(str(ws.id), str(cv_id)): _archive(
                    {
                        "ansible/hosts.yml": "all:\n  hosts:\n    web1:\n      ansible_host: 10.0.0.1\n"
                    }
                )
            }
        )

        async def _ws(db, workspace_id):
            return ws

        async def _declared(db, workspace_id):
            return "all:\n  hosts: {}\n"

        monkeypatch.setattr(inventory_resolve, "_declared_sources", _declared, raising=True)
        import terrapod.services.inventory_service as inv_service

        monkeypatch.setattr(inv_service, "get_inventory_workspace", _ws, raising=True)

        with _trapped(monkeypatch, store):
            document = asyncio.run(inventory_resolve.resolve_for_run(object(), run))

        assert "web1" in document["hosts"], document
        assert any(str(cv_id) in k for k in store.requested), (
            f"it did not read the CV: {store.requested}"
        )

    @needs_ansible
    def test_a_run_with_no_configuration_version_uses_the_declared_rows_alone(self, monkeypatch):
        """The ordinary shape for a Terraform-declared inventory: no git half to
        be missing, and no fetch to stand in for it."""
        ws = _Workspace()
        run = _Run(ws, cv_id=None)
        store = _Store()

        async def _ws(db, workspace_id):
            return ws

        async def _declared(db, workspace_id):
            return "all:\n  hosts:\n    declared1:\n      ansible_host: 10.0.0.9\n"

        monkeypatch.setattr(inventory_resolve, "_declared_sources", _declared)
        import terrapod.services.inventory_service as inv_service

        monkeypatch.setattr(inv_service, "get_inventory_workspace", _ws)

        with _trapped(monkeypatch, store):
            document = asyncio.run(inventory_resolve.resolve_for_run(object(), run))

        assert "declared1" in document["hosts"], document
        assert store.requested == [], "nothing should have been read from storage"

    @needs_ansible
    def test_configure_disabled_ignores_the_cv(self, monkeypatch):
        """`workspace_fetch_paths` only puts the config root in the tarball when
        `configure_enabled` (#1966), so extracting one from a workspace that has
        not declared a configure would find the Terraform tree and report an
        empty inventory rather than a misconfiguration."""
        ws = _Workspace(configure_enabled=False)
        run = _Run(ws, cv_id=uuid.uuid4())
        store = _Store()

        async def _ws(db, workspace_id):
            return ws

        async def _declared(db, workspace_id):
            return "all:\n  hosts:\n    only_declared:\n"

        monkeypatch.setattr(inventory_resolve, "_declared_sources", _declared)
        import terrapod.services.inventory_service as inv_service

        monkeypatch.setattr(inv_service, "get_inventory_workspace", _ws)

        with _trapped(monkeypatch, store):
            document = asyncio.run(inventory_resolve.resolve_for_run(object(), run))

        assert "only_declared" in document["hosts"]
        assert store.requested == [], "a configure-disabled workspace read a CV"


class TestTheArtifactIsStored:
    def test_the_document_lands_under_the_runs_own_key(self, monkeypatch):
        from terrapod.storage.keys import resolved_inventory_key

        ws = _Workspace()
        run = _Run(ws, cv_id=None)
        store = _Store()

        async def _resolved(db, r, *, limit=None):
            return {"hosts": {"web1": {}}, "groups": {}, "children": {}}

        monkeypatch.setattr(inventory_resolve, "resolve_for_run", _resolved)
        import terrapod.storage as storage_mod

        monkeypatch.setattr(storage_mod, "get_storage", lambda: store)

        key, _doc = asyncio.run(inventory_resolve.write_run_artifact(object(), run))

        assert key == resolved_inventory_key(str(ws.id), str(run.id))
        assert json.loads(store.written[key])["hosts"] == {"web1": {}}

    def test_an_empty_inventory_is_STORED_rather_than_skipped(self, monkeypatch):
        """So the phase can tell "nothing to target" from "could not read it".
        Writing nothing would make an empty inventory and a failed resolution
        the same observation, which is the one thing #2021 asked this to get
        right."""
        ws = _Workspace()
        run = _Run(ws, cv_id=None)
        store = _Store()

        async def _empty(db, r, *, limit=None):
            return {"hosts": {}, "groups": {}, "children": {}}

        monkeypatch.setattr(inventory_resolve, "resolve_for_run", _empty)
        import terrapod.storage as storage_mod

        monkeypatch.setattr(storage_mod, "get_storage", lambda: store)

        key, _doc = asyncio.run(inventory_resolve.write_run_artifact(object(), run))
        assert key in store.written
        assert json.loads(store.written[key]) == {"hosts": {}, "groups": {}, "children": {}}

    def test_a_resolution_failure_RAISES_rather_than_storing_nothing(self, monkeypatch):
        """Run creation refuses, so a configure that could not be resolved is
        never dispatched to a runner that would fail inside the phase."""
        ws = _Workspace()
        run = _Run(ws, cv_id=None)
        store = _Store()

        async def _boom(db, r, *, limit=None):
            raise inventory_resolve.InventoryResolutionFailed("no")

        monkeypatch.setattr(inventory_resolve, "resolve_for_run", _boom)
        import terrapod.storage as storage_mod

        monkeypatch.setattr(storage_mod, "get_storage", lambda: store)

        with pytest.raises(inventory_resolve.InventoryResolutionFailed):
            asyncio.run(inventory_resolve.write_run_artifact(object(), run))
        assert store.written == {}, "it stored something despite failing"
