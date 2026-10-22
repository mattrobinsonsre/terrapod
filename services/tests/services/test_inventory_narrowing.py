"""A derived configure narrows to the hosts that changed (#2229).

The user's rule, which is what every test here is derived from:

    a composite or autoconfigure run targets only changed hosts IF there is no
    change to the ansible code. If there is a change to the ansible code it
    targets all hosts. Override with a `configure_all_hosts` option

Two properties carry the whole feature and neither is visible from a single
fixture, so each has a test written as a CONTRAST:

* **Narrowing is bounded by `default_limit`.** The emitted limit is the
  intersection, so a workspace whose limit names a canary group can only ever
  reach less of that group -- never beyond it. A fixture whose changed hosts all
  happen to sit inside the limit passes whether or not the intersection is taken.
* **Every way of failing goes WIDE.** Absent baseline, moved code, changed
  nesting, nothing changed, changed hosts all outside the limit: all five return
  `""`. A test that only checked the happy path would pass with the override
  inverted.

`""` rather than `default_limit` for the wide answer is itself asserted, because
the two dispatch identically (`build_job_spec` falls back to `default-limit`) and
only one of them leaves a wide derived run recorded exactly as it is today.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import inventory_narrowing as narrow


def _ws(**kw):
    ws = MagicMock()
    ws.id = "ws-1"
    ws.default_limit = kw.pop("default_limit", "")
    ws.configure_all_hosts = kw.pop("configure_all_hosts", False)
    ws.config_directory = kw.pop("config_directory", "ansible")
    for k, v in kw.items():
        setattr(ws, k, v)
    return ws


def _run(**kw):
    run = MagicMock()
    run.id = "run-1"
    run.is_configure = kw.pop("is_configure", True)
    run.configure_after_apply = kw.pop("configure_after_apply", False)
    run.source = kw.pop("source", "auto-configure")
    run.configuration_version_id = kw.pop("configuration_version_id", "cv-1")
    for k, v in kw.items():
        setattr(run, k, v)
    return run


def _doc(hosts: dict | None = None, groups: dict | None = None, children: dict | None = None):
    return {
        "hosts": hosts if hosts is not None else {},
        "groups": groups if groups is not None else {},
        "children": children if children is not None else {},
    }


def _baseline(document: dict, cv: str | None = "cv-1"):
    return {"configuration-version-id": cv, "document": document}


class TestWhichRunsAreDerived:
    """Narrowing applies to a run the PLATFORM decided, not one somebody asked
    for. A person who queues a configure with no limit means "the workspace's
    default"; narrowing that would override a request rather than bound a side
    effect."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("auto-configure", True), ("vcs", True), ("tfe-api", False), ("ai-estimate", False)],
    )
    def test_the_source_decides(self, source, expected):
        assert narrow.is_derived_configure(_run(source=source)) is expected

    def test_a_plan_is_not_a_configure(self):
        assert narrow.is_derived_configure(_run(is_configure=False)) is False

    def test_a_composite_is_excluded_here_because_it_narrows_at_the_handover(self):
        """Not because a composite is undeserving -- it is derived by definition,
        the apply having decided it. Its inventory is only knowable once the
        apply has finished moving the estate (#2230), so its limit is computed
        there and computing one here would use a pre-apply host set."""
        assert narrow.is_derived_configure(_run(configure_after_apply=True, source="vcs")) is False


class TestChangedHosts:
    def test_a_host_whose_variables_moved_is_changed(self):
        before = _doc(hosts={"a": {"x": 1}, "b": {"x": 1}})
        after = _doc(hosts={"a": {"x": 2}, "b": {"x": 1}})
        changed, reason = narrow.changed_hosts(before, after)
        assert reason is None
        assert changed == {"a"}

    def test_a_host_whose_GROUPS_moved_is_changed_even_with_identical_variables(self):
        """The case variables alone would miss, and it is an ordinary one: moving
        a host into a group that carries no `group_vars` is how you bring it into
        scope. What reaches it is `hosts: webservers`, not a variable."""
        before = _doc(hosts={"a": {}}, groups={"web": []})
        after = _doc(hosts={"a": {}}, groups={"web": ["a"]})
        changed, reason = narrow.changed_hosts(before, after)
        assert reason is None
        assert changed == {"a"}

    def test_a_new_host_is_changed(self):
        changed, reason = narrow.changed_hosts(_doc(hosts={}), _doc(hosts={"a": {}}))
        assert reason is None
        assert changed == {"a"}

    def test_a_removed_host_is_not_in_the_set(self):
        """There is nothing to configure on a host that no longer exists, and
        naming one in a `--limit` would match nothing."""
        changed, reason = narrow.changed_hosts(
            _doc(hosts={"a": {}, "b": {}}), _doc(hosts={"a": {}})
        )
        assert reason is None
        assert changed == set()

    def test_identical_documents_change_nothing(self):
        d = _doc(hosts={"a": {"x": 1}}, groups={"web": ["a"]})
        changed, reason = narrow.changed_hosts(d, json.loads(json.dumps(d)))
        assert reason is None
        assert changed == set()

    def test_a_nesting_change_goes_wide(self):
        """A `children` edit changes what a pattern resolves to without
        necessarily changing any host's own membership, and computing the
        affected subtree is the group-DAG walk the inventory model forbids us
        from reimplementing."""
        before = _doc(hosts={"a": {}}, children={"prod": ["web"]})
        after = _doc(hosts={"a": {}}, children={"prod": ["web", "db"]})
        changed, reason = narrow.changed_hosts(before, after)
        assert reason == narrow.WIDE_NESTING
        assert changed == set()


@pytest.mark.asyncio
class TestTheLimitAndTheFiveWaysWide:
    async def _limit(self, *, ws, run, current, baseline, code_changed=False, allowed=None):
        with (
            patch.object(narrow, "load_baseline", AsyncMock(return_value=baseline)),
            patch.object(narrow, "_code_changed", AsyncMock(return_value=code_changed)),
            patch(
                "terrapod.services.inventory_resolve.resolve_for_run",
                AsyncMock(return_value=_doc(hosts={h: {} for h in (allowed or [])})),
            ),
        ):
            return await narrow.limit_for(MagicMock(), run, ws, current=current)

    async def test_it_narrows_to_the_changed_hosts(self):
        current = _doc(hosts={"a": {"x": 2}, "b": {"x": 1}, "c": {"x": 1}})
        base = _baseline(_doc(hosts={"a": {"x": 1}, "b": {"x": 1}, "c": {"x": 1}}))
        assert await self._limit(ws=_ws(), run=_run(), current=current, baseline=base) == "a"

    async def test_several_changed_hosts_are_joined_with_colons(self):
        current = _doc(hosts={"a": {"x": 2}, "b": {"x": 2}})
        base = _baseline(_doc(hosts={"a": {"x": 1}, "b": {"x": 1}}))
        assert await self._limit(ws=_ws(), run=_run(), current=current, baseline=base) == "a:b"

    async def test_the_override_goes_wide(self):
        current = _doc(hosts={"a": {"x": 2}})
        base = _baseline(_doc(hosts={"a": {"x": 1}}))
        got = await self._limit(
            ws=_ws(configure_all_hosts=True), run=_run(), current=current, baseline=base
        )
        assert got == ""

    async def test_no_baseline_goes_wide(self):
        """The first configure ever, or one whose predecessor failed. Hosts may
        be unconfigured for reasons no diff can see."""
        got = await self._limit(ws=_ws(), run=_run(), current=_doc(hosts={"a": {}}), baseline=None)
        assert got == ""

    async def test_moved_ansible_code_goes_wide(self):
        current = _doc(hosts={"a": {"x": 2}})
        base = _baseline(_doc(hosts={"a": {"x": 1}}))
        got = await self._limit(
            ws=_ws(), run=_run(), current=current, baseline=base, code_changed=True
        )
        assert got == ""

    async def test_changed_nesting_goes_wide(self):
        current = _doc(hosts={"a": {}}, children={"prod": ["web"]})
        base = _baseline(_doc(hosts={"a": {}}, children={}))
        assert await self._limit(ws=_ws(), run=_run(), current=current, baseline=base) == ""

    async def test_nothing_changed_goes_wide_rather_than_emitting_an_empty_limit(self):
        """A `--limit` matching nothing exits 1 (measured), so a run with nothing
        to do must not be handed one. Suppressing such a run entirely is #2229's
        remaining open decision; it changes the run lifecycle."""
        d = _doc(hosts={"a": {"x": 1}})
        assert await self._limit(ws=_ws(), run=_run(), current=d, baseline=_baseline(d)) == ""

    async def test_the_wide_answer_is_empty_and_NOT_the_default_limit(self):
        """The two dispatch identically -- `build_job_spec` falls back to
        `default-limit` -- but only `""` leaves a wide derived run recorded and
        served exactly as it is today, instead of starting to serialise an
        `ansible-limit` attribute that is absent now for no change in what runs.
        """
        d = _doc(hosts={"a": {"x": 1}})
        got = await self._limit(
            ws=_ws(default_limit="canary"), run=_run(), current=d, baseline=_baseline(d)
        )
        assert got == "", "a wide run must record no limit of its own"


@pytest.mark.asyncio
class TestDefaultLimitIsTheOuterBound:
    """Written as a contrast, because a fixture whose changed hosts all sit
    inside the limit passes whether or not the intersection is taken."""

    async def _limit(self, *, default_limit, changed_from, changed_to, allowed):
        with (
            patch.object(
                narrow, "load_baseline", AsyncMock(return_value=_baseline(_doc(hosts=changed_from)))
            ),
            patch.object(narrow, "_code_changed", AsyncMock(return_value=False)),
            patch(
                "terrapod.services.inventory_resolve.resolve_for_run",
                AsyncMock(return_value=_doc(hosts={h: {} for h in allowed})),
            ),
        ):
            return await narrow.limit_for(
                MagicMock(),
                _run(),
                _ws(default_limit=default_limit),
                current=_doc(hosts=changed_to),
            )

    async def test_a_changed_host_outside_the_limit_is_dropped(self):
        got = await self._limit(
            default_limit="canary",
            changed_from={"in": {"x": 1}, "out": {"x": 1}},
            changed_to={"in": {"x": 2}, "out": {"x": 2}},
            allowed=["in"],
        )
        assert got == "in", "the limit must be the intersection, not the changed set"

    async def test_changed_hosts_ALL_outside_the_limit_go_wide(self):
        """Narrowing to nothing would exit 1, so this is the same case as
        nothing having changed."""
        got = await self._limit(
            default_limit="canary",
            changed_from={"out": {"x": 1}},
            changed_to={"out": {"x": 2}},
            allowed=["in"],
        )
        assert got == ""

    async def test_with_no_default_limit_every_changed_host_is_targeted(self):
        got = await self._limit(
            default_limit="",
            changed_from={"a": {"x": 1}, "b": {"x": 1}},
            changed_to={"a": {"x": 2}, "b": {"x": 2}},
            allowed=[],
        )
        assert got == "a:b"


@pytest.mark.asyncio
class TestCodeChangedIsScopedToTheConfigDirectory:
    """This is what decides whether the feature is useful at all. A composite's
    configuration version almost always differs from the baseline's -- it came
    from a push -- so a coarse "is this a different CV" test would send every
    run wide and narrow nothing, ever."""

    async def _changed(
        self,
        *,
        files=None,
        oversized=None,
        this_cv="cv-2",
        base_cv="cv-1",
        config_directory="ansible",
        raises=None,
    ):
        diff = {"files": files or [], "oversized": oversized or []}
        mock = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=diff)
        with patch("terrapod.services.cv_diff_service.diff_tarballs", mock):
            return await narrow._code_changed(
                MagicMock(),
                _run(configuration_version_id=this_cv),
                _ws(config_directory=config_directory),
                base_cv,
            )

    async def test_a_terraform_only_push_does_not_count_as_changed(self):
        got = await self._changed(files=[{"path": "terraform/main.tf", "type": "modified"}])
        assert got is False, "a terraform-only push must still narrow"

    async def test_a_push_touching_the_config_root_counts_as_changed(self):
        got = await self._changed(files=[{"path": "ansible/site.yml", "type": "modified"}])
        assert got is True

    async def test_the_same_configuration_version_needs_no_diff_at_all(self):
        with patch("terrapod.services.cv_diff_service.diff_tarballs", AsyncMock()) as m:
            got = await narrow._code_changed(
                MagicMock(), _run(configuration_version_id="cv-1"), _ws(), "cv-1"
            )
        assert got is False
        m.assert_not_awaited()

    async def test_an_absent_baseline_version_counts_as_changed(self):
        assert await self._changed(base_cv=None) is True

    async def test_a_run_with_no_configuration_version_counts_as_changed(self):
        assert await self._changed(this_cv=None) is True

    async def test_a_failed_diff_counts_as_changed(self):
        assert await self._changed(raises=RuntimeError("boom")) is True

    async def test_a_file_too_large_to_diff_under_the_config_root_counts_as_changed(self):
        """An oversized file is EXCLUDED from `files` and listed in `oversized`,
        so reading `files` alone says "unchanged" about a large playbook or role
        that may well have moved -- the one direction this module must never fail
        in."""
        got = await self._changed(
            files=[{"path": "terraform/main.tf", "type": "modified"}],
            oversized=["ansible/roles/big/files/image.iso"],
        )
        assert got is True

    async def test_an_oversized_file_OUTSIDE_the_config_root_does_not_count(self):
        got = await self._changed(oversized=["terraform/big.zip"])
        assert got is False

    async def test_an_empty_config_directory_means_the_whole_repository(self):
        got = await self._changed(
            files=[{"path": "anything.yml", "type": "modified"}], config_directory=""
        )
        assert got is True


@pytest.mark.asyncio
class TestTheBaseline:
    async def test_a_round_trip(self):
        store: dict[str, bytes] = {}
        storage = MagicMock()
        storage.put = AsyncMock(side_effect=lambda k, v, **kw: store.__setitem__(k, v))
        storage.get = AsyncMock(side_effect=lambda k: store[k])
        with patch("terrapod.storage.get_storage", return_value=storage):
            await narrow.store_baseline(
                "ws-1", document=_doc(hosts={"a": {}}), config_version_id="cv-9"
            )
            got = await narrow.load_baseline("ws-1")
        assert got == {"configuration-version-id": "cv-9", "document": _doc(hosts={"a": {}})}

    async def test_an_absent_baseline_is_None_not_an_error(self):
        from terrapod.storage.protocol import ObjectNotFoundError

        storage = MagicMock()
        storage.get = AsyncMock(side_effect=ObjectNotFoundError("nope"))
        with patch("terrapod.storage.get_storage", return_value=storage):
            assert await narrow.load_baseline("ws-1") is None

    async def test_an_unreadable_baseline_is_None_rather_than_a_failed_configure(self):
        """A baseline we cannot read is a baseline we do not have, and that
        answer is a WIDE run. Erroring here would fail a configure over an
        optimisation."""
        storage = MagicMock()
        storage.get = AsyncMock(side_effect=RuntimeError("storage down"))
        with patch("terrapod.storage.get_storage", return_value=storage):
            assert await narrow.load_baseline("ws-1") is None

    async def test_a_baseline_that_is_not_json_is_None(self):
        storage = MagicMock()
        storage.get = AsyncMock(return_value=b"not json at all")
        with patch("terrapod.storage.get_storage", return_value=storage):
            assert await narrow.load_baseline("ws-1") is None


# --------------------------------------------------------------------------
# The call sites. A correct `limit_for` that nothing calls is the defect this
# half exists to catch -- the same shape as #2116's declaration that fifteen
# run creators never read, and as #2224 shipping a cycle that created runs no
# claim loop would ever match.
# --------------------------------------------------------------------------


def _full_ws(**kw):
    """A workspace complete enough for `create_run` to build a Run from."""
    import uuid as _uuid

    ws = MagicMock()
    ws.id = _uuid.uuid4()
    ws.name = "narrow-ws"
    ws.auto_apply = False
    ws.engine_version = "1.11"
    ws.terragrunt_enabled = False
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.ee_resource_cpu = ""
    ws.ee_resource_memory = ""
    ws.parallelism = 10
    ws.agent_pool_links = []
    ws.plan_apply_enabled = kw.get("plan_apply_enabled", True)
    ws.configure_enabled = True
    ws.configure_after_apply = kw.get("configure_after_apply", False)
    ws.auto_configure_enabled = False
    ws.ansible_version = ""
    ws.ansible_execution_environment = ""
    ws.default_limit = kw.get("default_limit", "")
    ws.configure_all_hosts = False
    ws.config_directory = "ansible"
    return ws


@pytest.mark.asyncio
class TestCreateRunNarrowsADerivedConfigure:
    async def _create(self, ws, **kw):
        """Create a run; return (the Run instance, the patched limit_for)."""
        import uuid as _uuid

        from sqlalchemy.ext.asyncio import AsyncSession

        from terrapod.services import inventory_resolve
        from terrapod.services.run_service import create_run

        db = AsyncMock(spec=AsyncSession)
        limit = AsyncMock(return_value="h1:h2")
        doc = _doc(hosts={"h1": {}, "h2": {}})
        with (
            patch("terrapod.services.run_service.Run") as MockRun,
            patch.object(
                inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", doc))
            ),
            patch.object(narrow, "limit_for", limit),
        ):
            inst = MockRun.return_value
            inst.id = _uuid.uuid4()
            inst.status = "pending"
            inst.is_configure = kw.get("is_configure", False)
            inst.configure_after_apply = kw.get("configure_after_apply", False)
            inst.source = kw.get("source", "tfe-api")
            inst.ansible_limit = kw.get("ansible_limit", "")
            await create_run(db, ws, message="m", **kw)
            return inst, limit, doc

    async def test_an_auto_configure_run_is_narrowed(self):
        inst, limit, doc = await self._create(
            _full_ws(plan_apply_enabled=False), is_configure=True, source="auto-configure"
        )
        limit.assert_awaited_once()
        assert inst.ansible_limit == "h1:h2"
        # Against the document just written, never a second resolution: the
        # limit may only name hosts the artifact contains.
        assert limit.await_args.kwargs["current"] is doc

    async def test_a_push_driven_configure_is_narrowed(self):
        inst, limit, _ = await self._create(
            _full_ws(plan_apply_enabled=False), is_configure=True, source="vcs"
        )
        limit.assert_awaited_once()
        assert inst.ansible_limit == "h1:h2"

    async def test_a_configure_somebody_ASKED_for_is_NOT_narrowed(self):
        """A person who queues a configure without a limit means "the
        workspace's default". Narrowing that overrides a request."""
        inst, limit, _ = await self._create(
            _full_ws(plan_apply_enabled=False), is_configure=True, source="tfe-api"
        )
        limit.assert_not_awaited()
        assert inst.ansible_limit == ""

    async def test_an_explicit_limit_is_never_overwritten(self):
        _inst, limit, _ = await self._create(
            _full_ws(plan_apply_enabled=False),
            is_configure=True,
            source="auto-configure",
            ansible_limit="canary",
        )
        limit.assert_not_awaited()

    async def test_a_composite_is_not_narrowed_at_CREATION(self):
        """Its inventory is only knowable once the apply has moved the estate
        (#2230), so a limit computed here would name a pre-apply host set."""
        _inst, limit, _ = await self._create(_full_ws(configure_after_apply=True))
        limit.assert_not_awaited()

    async def test_a_plain_plan_resolves_nothing_and_narrows_nothing(self):
        _inst, limit, _ = await self._create(_full_ws())
        limit.assert_not_awaited()


@pytest.mark.asyncio
class TestTheCompositeNarrowsAtTheHandOver:
    async def _complete(self, *, existing_limit=""):
        from terrapod.db.models import Workspace  # noqa: F401
        from terrapod.services import inventory_resolve, run_service

        run = MagicMock()
        run.id = "run-1"
        run.workspace_id = "ws-1"
        run.status = "applying"
        run.is_configure = True
        run.configure_after_apply = True
        run.is_destroy = False
        run.ansible_limit = existing_limit
        # Explicitly None: the hand-over's idempotency guard is
        # `apply_finished_at is not None`, and a MagicMock attribute is not
        # None -- so leaving it unset makes `complete_apply` return early as
        # though the hand-over had already happened, and every assertion below
        # would be about a function that did nothing.
        run.apply_finished_at = None

        async def _transition(_db, r, status, **_kw):
            r.status = status
            return r

        doc = _doc(hosts={"new-host": {}})
        limit = AsyncMock(return_value="new-host")
        db = AsyncMock()
        db.get = AsyncMock(return_value=_ws())
        with (
            patch.object(
                inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", doc))
            ),
            patch.object(narrow, "limit_for", limit),
            patch.object(run_service, "transition_run", AsyncMock(side_effect=_transition)),
            patch.object(run_service, "_after_apply_landed", AsyncMock()),
            patch("terrapod.redis.client.delete_job_status", new_callable=AsyncMock),
        ):
            await run_service.complete_apply(db, run)
        return run, limit, doc

    async def test_the_hand_over_narrows_against_the_RE_RESOLVED_document(self):
        """The whole reason it is computed here rather than at creation: the
        apply has only now finished changing what the inventory resolves to, so
        a host the apply just created is in this document and not in the one
        run creation saw."""
        run, limit, doc = await self._complete()
        limit.assert_awaited_once()
        assert limit.await_args.kwargs["current"] is doc
        assert run.ansible_limit == "new-host"

    async def test_a_composite_created_with_an_explicit_limit_keeps_it(self):
        run, limit, _ = await self._complete(existing_limit="canary")
        limit.assert_not_awaited()
        assert run.ansible_limit == "canary"


@pytest.mark.asyncio
class TestTheBaselineIsRecordedOnSuccessOnly:
    async def _complete_configure(self, *, store=None, resolve=None):
        from terrapod.services import inventory_resolve, run_service

        run = MagicMock()
        run.id = "run-1"
        run.workspace_id = "ws-1"
        run.status = "configuring"
        run.configuration_version_id = "cv-7"

        async def _transition(_db, r, status, **_kw):
            r.status = status
            return r

        store = store or AsyncMock()
        resolve = resolve or AsyncMock(return_value=_doc(hosts={"a": {}}))
        db = AsyncMock()
        db.get = AsyncMock(return_value=_ws())
        with (
            patch.object(narrow, "store_baseline", store),
            patch.object(inventory_resolve, "resolve_for_run", resolve),
            patch.object(run_service, "transition_run", AsyncMock(side_effect=_transition)),
            patch.object(run_service, "_after_apply_landed", AsyncMock()),
        ):
            out = await run_service.complete_configure(db, run)
        return out, store

    async def test_a_successful_configure_records_what_it_targeted(self):
        out, store = await self._complete_configure()
        assert out.status == "applied"
        store.assert_awaited_once()
        assert store.await_args.kwargs["document"] == _doc(hosts={"a": {}})
        assert store.await_args.kwargs["config_version_id"] == "cv-7"

    async def test_a_run_that_is_not_configuring_records_nothing(self):
        """`complete_configure` returns early, so a failed configure never
        leaves a baseline -- which is what makes the next derived run go wide
        rather than diffing against a half-applied estate."""
        from terrapod.services import run_service

        run = MagicMock()
        run.status = "errored"
        store = AsyncMock()
        with patch.object(narrow, "store_baseline", store):
            await run_service.complete_configure(AsyncMock(), run)
        store.assert_not_awaited()

    async def test_a_failure_to_store_does_not_fail_the_configure(self):
        """A baseline we could not store is a baseline the next run does not
        have, and the consequence of that is a WIDER run. Failing the configure
        over it would turn a safe degradation into a reported failure on a
        playbook that had already succeeded."""
        out, _ = await self._complete_configure(
            store=AsyncMock(side_effect=RuntimeError("storage down"))
        )
        assert out.status == "applied"

    async def test_a_failure_to_RESOLVE_does_not_fail_the_configure_either(self):
        out, store = await self._complete_configure(
            resolve=AsyncMock(side_effect=RuntimeError("ansible-inventory exploded"))
        )
        assert out.status == "applied"
        store.assert_not_awaited()


def test_the_baseline_is_written_from_exactly_one_place():
    """Derived, not listed: a second writer is a second thing that can record a
    baseline for a configure that did NOT succeed, which is the one condition
    the whole narrowing rests on."""
    import ast
    import pathlib

    src_root = pathlib.Path(narrow.__file__).resolve().parent.parent
    callers = []
    for f in sorted(src_root.rglob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "store_baseline" and f.name != "inventory_narrowing.py":
                callers.append(f"{f.relative_to(src_root)}:{node.lineno}")
    assert len(callers) == 1, (
        f"`store_baseline` is called from {len(callers)} places: {callers}. It must be "
        "called from exactly one, inside the success path of `complete_configure` — a "
        "second caller could record a baseline for a configure that did not succeed, "
        "and the next derived run would then narrow against an estate no diff describes."
    )
    assert callers[0].endswith("run_service.py:" + callers[0].split(":")[-1])
    assert "run_service.py" in callers[0], callers[0]
