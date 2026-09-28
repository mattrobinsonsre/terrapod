"""The API half of `pulumi_config` (#1565).

Three seams, each with its own failure worth pinning:

- **the run wire** — `pulumi-config` carries the flags a Pulumi run needs, and
  carries them *separately* from `terraform-vars`, because the two are delivered
  by different mechanisms and folding them together would put the workspace's
  Pulumi config into a tfvars file nothing reads;
- **the category** — accepted on any workspace, per #1407 §6's settled
  "engine mismatch is permissive";
- **the surfacing that replaces the refusal** — a variable an engine never reads
  says so, on itself and on its workspace, because the thing §6 asks to avoid is
  not the write but the silence.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.api.routers import tfe_v2
from terrapod.api.routers import variables as variables_router
from terrapod.services import variable_service
from terrapod.services.variable_service import ResolvedVariable


def _rv(key, value, *, category="pulumi_config", sensitive=False, structured=False):
    return ResolvedVariable(
        key=key,
        value=value,
        category=category,
        structured=structured,
        sensitive=sensitive,
        value_source="static",
    )


async def _claim(resolved, *, engine="pulumi") -> dict:
    lid = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.source = "tfe-api"
    ws = MagicMock()
    ws.var_files = []
    ws.working_directory = ""
    ws.engine = engine
    ws.name = "smoke::dev"
    ws.pulumi_bind_plan = False
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    db.add_all = MagicMock()
    with (
        patch.object(
            runs_router.agent_pool_service,
            "get_listener",
            AsyncMock(return_value={"pool_id": str(uuid.uuid4()), "name": "l"}),
        ),
        patch.object(
            runs_router.run_service, "claim_next_run", AsyncMock(return_value=(run, "plan"))
        ),
        patch.object(runs_router.run_service, "transition_run", AsyncMock()),
        patch(
            "terrapod.services.variable_service.resolve_variables",
            AsyncMock(return_value=resolved),
        ),
        patch("terrapod.services.git_auth_service.resolve_git_auth", AsyncMock(return_value=[])),
        patch("terrapod.config.load_runner_config", return_value=MagicMock(hooks_enabled=False)),
        patch.object(
            runs_router, "_run_json", return_value={"data": {"id": "run-x", "attributes": {}}}
        ),
    ):
        resp = await runs_router.next_run(
            listener_id=f"listener-{lid}", identity=MagicMock(listener_id=lid), db=db
        )
    assert resp.status_code == 200, resp.body
    return json.loads(resp.body)["data"]["attributes"]


@pytest.mark.asyncio
class TestTheRunWireCarriesTheConfig:
    async def test_the_flags_come_from_the_variables_own_fields(self) -> None:
        attrs = await _claim(
            [
                _rv("region", "eu-west-1"),
                _rv("dbpass", "sup3rs3cret", sensitive=True),
                _rv("outer.inner", "nested", structured=True),
            ]
        )
        assert attrs["pulumi-config"] == [
            {"key": "region", "value": "eu-west-1", "secret": False, "path": False},
            {"key": "dbpass", "value": "sup3rs3cret", "secret": True, "path": False},
            {"key": "outer.inner", "value": "nested", "secret": False, "path": True},
        ]

    async def test_it_does_not_leak_into_terraform_vars(self) -> None:
        """The two are delivered by different mechanisms — a tfvars file the
        engine reads, versus `pulumi config set` on the stack — so folding them
        together would write the workspace's Pulumi config into a file nothing
        reads, and drop it on the floor."""
        attrs = await _claim(
            [_rv("region", "eu-west-1"), _rv("cidr", "10.0.0.0/16", category="terraform")]
        )
        assert [v["key"] for v in attrs["terraform-vars"]] == ["cidr"]
        assert [c["key"] for c in attrs["pulumi-config"]] == ["region"]

    async def test_a_workspace_with_none_sends_an_empty_list(self) -> None:
        """Empty rather than absent: a listener reading the key unconditionally
        must not have to guard, and the stack then runs on whatever its
        repository committed."""
        attrs = await _claim([_rv("cidr", "10.0.0.0/16", category="terraform")])
        assert attrs["pulumi-config"] == []

    async def test_keys_are_sent_verbatim(self) -> None:
        """#1407 §6 makes this a negative rule: never prefix. The CLI namespaces
        an unqualified key to the project itself, and `aws:region` must survive
        untouched."""
        attrs = await _claim([_rv("aws:region", "eu-west-1"), _rv("region", "x")])
        assert [c["key"] for c in attrs["pulumi-config"]] == ["aws:region", "region"]


class TestTheCategoryIsAcceptedAnywhere:
    """#1407 §6: "Engine mismatch is permissive." Variables are data, and which
    of them apply is decided at run time by the engine that runs."""

    def test_it_is_a_valid_category(self) -> None:
        assert variable_service.PULUMI_CONFIG_CATEGORY in variable_service.VALID_CATEGORIES

    def test_no_write_path_refuses_it_for_being_on_the_wrong_engine(self) -> None:
        """A 422 was written first and removed. Pinned so it does not come back:
        it would reopen a settled decision, and a write-time check couples two
        things that need no coupling."""
        import inspect

        src = inspect.getsource(variables_router)
        assert "_reject_category_on_wrong_engine" not in src


class TestTheMismatchIsSurfacedInstead:
    def test_a_variable_the_engine_never_reads_says_so(self) -> None:
        var = MagicMock(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            key="region",
            value="v",
            sensitive=False,
            category="pulumi_config",
            structured=False,
            value_source="static",
            description="",
            version_id="v1",
            created_at=None,
            updated_at=None,
        )
        assert (
            variables_router._var_json(var, "terraform")["attributes"]["applies-to-engine"] is False
        )
        assert variables_router._var_json(var, "pulumi")["attributes"]["applies-to-engine"] is True

    def test_a_terraform_variable_on_a_pulumi_workspace_says_so_too(self) -> None:
        """The rule is symmetric. A workspace that changed engine keeps its old
        variables, and they are just as inert in that direction."""
        var = MagicMock(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            key="cidr",
            value="v",
            sensitive=False,
            category="terraform",
            structured=False,
            value_source="static",
            description="",
            version_id="v1",
            created_at=None,
            updated_at=None,
        )
        assert variables_router._var_json(var, "pulumi")["attributes"]["applies-to-engine"] is False
        assert (
            variables_router._var_json(var, "terraform")["attributes"]["applies-to-engine"] is True
        )

    def test_an_engine_neutral_category_always_applies(self) -> None:
        """`env` reaches the environment and the `git_*_auth` pair is
        materialized before init, whatever runs after."""
        for category in ("env", "git_http_auth", "git_ssh_auth"):
            for engine in ("terraform", "pulumi"):
                assert variable_service.consumed_by_engine(category, engine) is True

    def test_an_unset_engine_reads_as_terraform(self) -> None:
        """The column defaults to `terraform` and a MagicMock-ish empty string
        must not make every terraform variable look inert."""
        assert variable_service.consumed_by_engine("terraform", "") is True
        assert variable_service.consumed_by_engine("pulumi_config", "") is False

    def test_the_workspace_raises_a_health_condition(self) -> None:
        ws = MagicMock(
            state_diverged=False,
            execution_mode="local",
            vcs_last_error=None,
            drift_detection_enabled=False,
            lifecycle_state="active",
        )
        codes = [
            c["code"] for c in tfe_v2._compute_health_conditions(ws, None, has_inert_vars=True)
        ]
        assert "variables_not_consumed" in codes

    def test_a_clean_workspace_raises_none(self) -> None:
        ws = MagicMock(
            state_diverged=False,
            execution_mode="local",
            vcs_last_error=None,
            drift_detection_enabled=False,
            lifecycle_state="active",
        )
        codes = [
            c["code"] for c in tfe_v2._compute_health_conditions(ws, None, has_inert_vars=False)
        ]
        assert "variables_not_consumed" not in codes

    def test_the_condition_is_a_warning_not_an_error(self) -> None:
        """Nothing is broken — the run works, it just does not deliver that
        variable. An error badge would send someone hunting for an outage."""
        ws = MagicMock(
            state_diverged=False,
            execution_mode="local",
            vcs_last_error=None,
            drift_detection_enabled=False,
            lifecycle_state="active",
        )
        (cond,) = [
            c
            for c in tfe_v2._compute_health_conditions(ws, None, has_inert_vars=True)
            if c["code"] == "variables_not_consumed"
        ]
        assert cond["severity"] == "warning"


class TestTheEngineGateCoversThisWithoutAFlagOfItsOwn:
    """#1429 requires every Pulumi-serving surface to be gated. This one has no
    switch of its own, and deliberately so — the gating is structural.

    `pulumi_config` is a variable *category*, not a registered surface: there is
    no router to leave unmounted and no task to leave unregistered. Gating the
    category itself would also contradict #1407 §6, which settles that a
    category is accepted whatever engine the workspace runs.

    What the gate does cover is the only path that ever delivers it. A Pulumi
    workspace can only be created on the native surface, which validates the
    engine against `known_engines()` — already filtered by the gate — so with
    Pulumi off there is no Pulumi workspace, no Pulumi run, and nothing that
    reads a `pulumi_config` variable. Proven rather than asserted below, and in
    `tests/integration/test_pulumi_config_variables.py`, which has to turn the
    engine on before it can create a workspace at all.

    Nothing is destructive either way: turning Pulumi off leaves the variables
    stored and returns them on re-enable, because they were never touched.

    There is no `helm-smoke` assertion because this adds no Helm value and no
    config key — the config channel is unchanged, which the config-contract
    snapshot confirms by not moving.
    """

    def test_pulumi_is_absent_from_the_engines_on_offer_when_gated_off(self) -> None:
        from terrapod.config import settings
        from terrapod.engines import known_engines

        before = settings.engines.pulumi.enabled
        try:
            settings.engines.pulumi.enabled = False
            assert "pulumi" not in known_engines()
            settings.engines.pulumi.enabled = True
            assert "pulumi" in known_engines()
        finally:
            settings.engines.pulumi.enabled = before

    def test_terraform_is_on_offer_in_both_states(self) -> None:
        """The whole point of the gate: a deployment that came for Terraform
        pays nothing for an engine it does not use, and loses nothing either."""
        from terrapod.config import settings
        from terrapod.engines import known_engines

        before = settings.engines.pulumi.enabled
        try:
            for state in (False, True):
                settings.engines.pulumi.enabled = state
                assert "terraform" in known_engines()
        finally:
            settings.engines.pulumi.enabled = before

    def test_the_category_stays_valid_whatever_the_gate_says(self) -> None:
        """Not an oversight. Refusing the category with Pulumi off would be a
        write-time engine check, which #1407 §6 rules out — and it would make
        turning the engine off destructive to variables that already exist,
        which #1429 rules out separately."""
        from terrapod.config import settings

        before = settings.engines.pulumi.enabled
        try:
            for state in (False, True):
                settings.engines.pulumi.enabled = state
                assert variable_service.PULUMI_CONFIG_CATEGORY in variable_service.VALID_CATEGORIES
        finally:
            settings.engines.pulumi.enabled = before
