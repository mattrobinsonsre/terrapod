"""The two dynamic-inventory switches, API side (#2033).

A workspace may opt into having ansible's `script` and `auto` inventory plugins
resolved **on the runner** -- an ephemeral Job with no database URL, no KEK and
an identity scoped to one run, which already executes the operator's playbook
and is therefore strictly more powerful than the plugin it is being trusted
with. #1970's objection was always about WHERE the code runs, and the API pod's
environment holds the database URL, the token signing key and the KEK. So the
API refuses both, permanently and whatever the switches say; what a switch
changes is that the source is EXCLUDED and the resolution says so.

Three properties are the whole feature and each is driven here:

* a skipped source is **reported, per source**, because an unlabelled skip is
  the short-host-set failure the inventory surface exists to prevent, arriving
  by a new door;
* the executable screening is **scoped to the sources ansible will read**, so an
  ordinary repository's `library/` module or helper script is untouched;
* the run's artifact carries **what the runner needs to resolve them**, and only
  when the resolution is partial.

The exec-bit measurements this rests on are recorded on
`_screen_inventory_sources`; `test_inventory_resolve.py` drives the plugin half.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import inventory_resolve as ir


def _sources(*paths: Path, root: Path, config_directory: str = "ansible") -> ir._InventorySources:
    return ir._InventorySources(
        paths=list(paths),
        tier="declared",
        root=root,
        config_directory=config_directory,
    )


def _executable(path: Path, body: str = "#!/bin/sh\necho '{}'\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class TestTheScriptScreening:
    """`inventory-scripts-enabled`, both directions."""

    def test_an_executable_source_is_refused_by_name(self, tmp_path):
        script = _executable(tmp_path / "hosts.sh")
        with pytest.raises(ir.InventorySourceRefused) as exc:
            ir._screen_inventory_sources(
                _sources(script, root=tmp_path), {script}, allow_scripts=False
            )
        message = str(exc.value)
        assert "hosts.sh" in message, "the refusal must name the file"
        assert "inventory-scripts-enabled" in message, "and how to opt in"
        assert "the API never will" in message, "and that the API is never the place"

    def test_with_the_switch_on_it_is_skipped_and_removed(self, tmp_path):
        """Skipped means EXCLUDED FROM THE TREE, not "parsed anyway".

        Measured against ansible-core 2.18.3: with the bit stripped, resolution
        is rc=1 under `yaml,ini` AND under `script,yaml,ini`, because the `yaml`
        plugin claims the file by extension and fails on its contents while
        `any_unparsed_is_failed` promotes that to a failure. Only removing it
        resolves. So the switch cannot mean "stop refusing".
        """
        script = _executable(tmp_path / "hosts.sh")
        skipped = ir._screen_inventory_sources(
            _sources(script, root=tmp_path), {script}, allow_scripts=True
        )
        assert not script.exists()
        assert [(s.path, s.kind, s.plugin) for s in skipped] == [("hosts.sh", "script", None)]
        assert skipped[0].reason, "a skip with no reason is an unlabelled short host set"

    def test_an_executable_OUTSIDE_the_source_set_is_untouched(self, tmp_path):
        """The whole reason this is a second, scoped pass.

        A config root legitimately holds executables -- a custom module under
        `library/`, a filter plugin, a helper beside a playbook -- and only a
        file ansible reads AS INVENTORY is a dynamic inventory source. Refusing
        every executable in the tree would break ordinary repositories, and that
        is not a hypothetical: the fixture below is the shape of every ansible
        repository with a custom module in it.
        """
        inventory = tmp_path / "hosts.yml"
        inventory.write_text("all:\n  hosts:\n    web1:\n")
        module = _executable(tmp_path / "library" / "mymodule.py")

        skipped = ir._screen_inventory_sources(
            _sources(inventory, root=tmp_path), {module}, allow_scripts=False
        )

        assert skipped == []
        assert module.exists(), "an executable outside the inventory was removed"

    def test_a_directory_source_is_walked_but_ignored_extensions_are_not_sources(self, tmp_path):
        """Ansible's own `INVENTORY_IGNORE_EXTS` decides what is a source.

        An executable `hosts.d/10_real.sh` is one; an executable
        `hosts.d/notes.bak` is not, because ansible does not read it. Refusing
        the second would refuse a repository for a file ansible never opens.
        """
        directory = tmp_path / "hosts.d"
        real = _executable(directory / "10_real.sh")
        junk = _executable(directory / "notes.bak")

        skipped = ir._screen_inventory_sources(
            _sources(directory, root=tmp_path), {real, junk}, allow_scripts=True
        )

        assert [s.path for s in skipped] == ["hosts.d/10_real.sh"]
        assert junk.exists(), "an ignored-extension file is not an inventory source"

    def test_nothing_executable_is_a_fast_no_op(self, tmp_path):
        inventory = tmp_path / "hosts.yml"
        inventory.write_text("all:\n  hosts: {}\n")
        assert (
            ir._screen_inventory_sources(
                _sources(inventory, root=tmp_path), set(), allow_scripts=False
            )
            == []
        )


class TestThePartialFlag:
    """`partial` is DERIVED from the skip list, never stored.

    Two fields that can disagree is how a stale one survives, and a reader
    asking "can I trust this host set" should not have to know that a non-empty
    list implies it.
    """

    def test_no_skips_is_not_partial(self):
        out = ir._with_skipped({"hosts": {}}, [])
        assert out["partial"] is False
        assert out[ir.SKIPPED_KEY] == []

    def test_one_skip_makes_it_partial_and_carries_the_record(self):
        skip = ir.SkippedSource(path="hosts.d/aws.yml", kind="plugin", plugin="aws_ec2", reason="r")
        out = ir._with_skipped({"hosts": {"a": {}}}, [skip])
        assert out["partial"] is True
        assert out[ir.SKIPPED_KEY] == [
            {"path": "hosts.d/aws.yml", "kind": "plugin", "plugin": "aws_ec2", "reason": "r"}
        ]
        assert out["hosts"] == {"a": {}}, "the envelope must not disturb the document"


class TestTheHandoffPath:
    """A source named to the RUNNER has to be a path it can resolve."""

    def test_it_is_repo_relative_and_includes_the_config_directory(self, tmp_path):
        source = tmp_path / "hosts.d" / "10_static.yml"
        source.parent.mkdir(parents=True)
        source.touch()
        prov = _sources(source, root=tmp_path, config_directory="ansible")
        assert ir._handoff_path(source, prov) == "ansible/hosts.d/10_static.yml"

    def test_an_empty_config_directory_means_the_repository_root(self, tmp_path):
        source = tmp_path / "hosts.yml"
        source.touch()
        prov = _sources(source, root=tmp_path, config_directory="")
        assert ir._handoff_path(source, prov) == "hosts.yml"

    def test_a_source_outside_the_root_RAISES_rather_than_naming_a_tempdir(self, tmp_path):
        """`_repo_relative` answers the absolute path here, which is right for a
        human and is a deleted scratch directory to the runner -- so ansible
        would resolve one source fewer than the operator declared, which is a
        short host set reported as success."""
        outside = tmp_path.parent / "elsewhere.yml"
        prov = _sources(root=tmp_path / "inside")
        (tmp_path / "inside").mkdir()
        with pytest.raises(ir.InventoryResolutionFailed):
            ir._handoff_path(outside, prov)

    def test_the_root_itself_RAISES(self, tmp_path):
        prov = _sources(root=tmp_path, config_directory="")
        with pytest.raises(ir.InventoryResolutionFailed):
            ir._handoff_path(tmp_path, prov)


class TestTheHandoffEnvelope:
    def test_it_carries_the_sources_and_the_declared_rows(self):
        out = ir._with_handoff(
            {"hosts": {"a": {}}, "partial": True},
            sources=["ansible/hosts.d/10_static.yml", "ansible/hosts.d/30_aws.yml"],
            declared={"all": {"hosts": {"b": {}}}},
        )
        assert out[ir.HANDOFF_SOURCES_KEY] == [
            "ansible/hosts.d/10_static.yml",
            "ansible/hosts.d/30_aws.yml",
        ]
        assert out[ir.HANDOFF_DECLARED_KEY] == {"all": {"hosts": {"b": {}}}}
        assert out["hosts"] == {"a": {}}


@pytest.mark.asyncio
class TestTheRunPathAsksForTheHandoffAndTheReadDoesNot:
    """A read has nobody to hand it to, and its document is Redis-cached.

    Driven through `resolve_for_run` and `resolve` rather than asserted on
    `_resolve_uncached`'s signature: an argument that exists and is never passed
    is the shape this project keeps finding, so what matters is which value the
    real caller supplies.
    """

    async def _seen(self, call) -> dict:
        seen: dict = {}

        async def _fake(db, workspace_id, **kwargs):
            seen.update(kwargs)
            return {"hosts": {}, "groups": {}, "children": {}}

        ws = SimpleNamespace(
            configure_enabled=True,
            config_directory="ansible",
            vcs_connection_id=uuid.uuid4(),
            vcs_repo_url="https://example.invalid/r",
        )
        with (
            patch.object(ir, "_resolve_uncached", _fake),
            patch(
                "terrapod.services.inventory_service.get_inventory_workspace",
                AsyncMock(return_value=ws),
            ),
            patch.object(ir, "_resolve_git_sha", AsyncMock(return_value="deadbeef")),
            patch.object(ir, "platform_rev", AsyncMock(return_value="rev")),
            patch(
                "terrapod.redis.client.get_redis_client",
                # A SYNC factory returning an async client, which is what it is.
                # `AsyncMock` here makes the factory itself awaitable and the
                # failure reads as the client having no `get`.
                return_value=SimpleNamespace(
                    get=AsyncMock(return_value=None), setex=AsyncMock(return_value=None)
                ),
            ),
        ):
            await call(ws)
        return seen

    async def test_the_run_path_asks_for_it(self):
        run = SimpleNamespace(
            workspace_id=uuid.uuid4(), id=uuid.uuid4(), configuration_version_id=uuid.uuid4()
        )
        seen = await self._seen(lambda ws: ir.resolve_for_run(None, run))
        assert seen.get("handoff") is True

    async def test_the_read_path_does_not(self):
        """Not an aesthetic point: the read's document is cached in Redis and
        served to the view, so a tree layout there would be published to every
        reader for no consumer's benefit."""
        seen = await self._seen(lambda ws: ir.resolve(None, uuid.uuid4()))
        assert not seen.get("handoff")


@pytest.mark.asyncio
class TestTheDenominator:
    """What `outstanding_from` subtracts from, when the API's half is short.

    The artifact lists the sources the API could read, so for a dynamic
    workspace it is a SHORT denominator -- and a short denominator under-reports
    which hosts are still outstanding, which is the one thing this number exists
    to get right.
    """

    def _stream(self, records: list[dict]):
        body = "".join(json.dumps(r) + "\n" for r in records).encode()

        async def _lines(key):  # noqa: ARG001
            for line in body.split(b"\n"):
                yield line

        return _lines

    def _run(self):
        return SimpleNamespace(id=uuid.uuid4(), workspace_id=uuid.uuid4())

    async def test_the_reported_set_is_read_from_the_head_of_the_stream(self):
        from terrapod.services import host_results_service as hrs

        run = self._run()
        with patch.object(
            hrs,
            "_lines",
            self._stream(
                [
                    {"kind": "playbook_start"},
                    {"kind": "play_start", "pattern": "all"},
                    {"kind": "inventory", "hosts": ["w1", "w2", "discovered-1"]},
                ]
            ),
        ):
            assert await hrs.reported_inventory_hosts(run) == {"w1", "w2", "discovered-1"}

    async def test_no_record_is_an_empty_set_rather_than_an_error(self):
        """Every ordinary reason: an older runner image, a Job that died before
        uploading, a callback that could not reach the inventory."""
        from terrapod.services import host_results_service as hrs

        with patch.object(hrs, "_lines", self._stream([{"kind": "stats", "recap": {}}])):
            assert await hrs.reported_inventory_hosts(self._run()) == set()

    async def test_it_stops_scanning_rather_than_reading_a_whole_fleets_results(self):
        from terrapod.services import host_results_service as hrs

        noise = [{"kind": "ok", "host": f"h{i}"} for i in range(500)]
        late = noise + [{"kind": "inventory", "hosts": ["never-seen"]}]
        with patch.object(hrs, "_lines", self._stream(late)):
            assert await hrs.reported_inventory_hosts(self._run()) == set()

    async def _targeted(self, document: dict, reported: set[str]) -> set[str]:
        from terrapod.services import configure_recovery as cr

        storage = AsyncMock()
        storage.get = AsyncMock(return_value=json.dumps(document).encode())
        with (
            patch("terrapod.storage.get_storage", return_value=storage),
            patch(
                "terrapod.services.host_results_service.reported_inventory_hosts",
                AsyncMock(return_value=reported),
            ),
        ):
            return await cr.targeted_hosts(self._run())

    async def test_a_partial_artifact_is_UNIONED_with_what_the_runner_reported(self):
        assert await self._targeted(
            {"hosts": {"declared-1": {}}, "partial": True}, {"discovered-1", "discovered-2"}
        ) == {"declared-1", "discovered-1", "discovered-2"}

    async def test_a_complete_artifact_does_not_read_the_stream_at_all(self):
        """Where the API resolved everything the report agrees, so reading it
        buys nothing but a storage call on every per-host view."""
        from terrapod.services import configure_recovery as cr

        storage = AsyncMock()
        storage.get = AsyncMock(return_value=json.dumps({"hosts": {"a": {}}}).encode())
        reported = AsyncMock(return_value={"should-not-appear"})
        with (
            patch("terrapod.storage.get_storage", return_value=storage),
            patch("terrapod.services.host_results_service.reported_inventory_hosts", reported),
        ):
            assert await cr.targeted_hosts(self._run()) == {"a"}
        reported.assert_not_awaited()

    async def test_the_union_cannot_SHRINK_the_denominator_when_the_report_is_absent(self):
        """Unioned rather than preferred, and this is why.

        Either side can be the smaller one -- the artifact when the API skipped
        a source, the report on an older runner image. A preference would
        silently take the short side on exactly the runs that need the long one.
        """
        assert await self._targeted({"hosts": {"declared-1": {}}, "partial": True}, set()) == {
            "declared-1"
        }
