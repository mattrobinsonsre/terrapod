"""One category for the engine's own parameters, two names on the wire (#1898).

Every engine has exactly one channel for "parameters the platform supplies to
this run" — Terraform's input variables, Pulumi's stack config, Ansible's extra
vars. They are one role with three deliveries, so they are **one category**:
`native`, stored under that name, delivered by whichever engine runs.

The wire keeps two spellings of it, permanently:

- **`terraform` on the TFE-compatible surface.** `tfci` and `go-tfe` have that
  as a constant and an unrecognised value would be a compatibility break, so
  `/api/tfe/v2` (and its `/api/v2` alias) say `terraform` for ever. This is
  exactly the arrangement `structured` has with `hcl` (#1435) — the column
  carries the honest name, the frozen surface carries the compatible one.
- **`native` on Terrapod's own surface**, where the honest name costs nothing
  and says what the thing is.

`terraform` and `pulumi_config` are both accepted on *input* anywhere, folded to
`native` at the write boundary. `pulumi_config` existed for about two hours
(#1565) and is folded rather than refused, so a request written against the
shape an operator was told to use days earlier keeps working.

What this file pins is the seams: the round-trip on each surface, the single run
wire, and the absence of the things #1898 removed — a second config list, a
write-time engine refusal, and an `applies-to-engine` flag that only existed
because a variable could be in a category its engine would never read.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.api.routers import variables as variables_router
from terrapod.services import variable_service
from terrapod.services.variable_service import ResolvedVariable

_NATIVE = variable_service.NATIVE_CATEGORY


def _rv(key, value, *, category=_NATIVE, sensitive=False, structured=False):
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


def _request(path: str):
    return MagicMock(url=MagicMock(path=path))


def _var_attributes() -> dict:
    return variables_router._var_json(_var())["attributes"]


def _var(category: str = _NATIVE, key: str = "region"):
    return MagicMock(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        key=key,
        value="v",
        sensitive=False,
        category=category,
        structured=False,
        value_source="static",
        description="",
        version_id="v1",
        created_at=None,
        updated_at=None,
    )


class TestTheStoredNameIsTheHonestOne:
    def test_native_is_the_category(self) -> None:
        assert _NATIVE in variable_service.VALID_CATEGORIES

    def test_the_engine_named_ones_are_not_stored_names(self) -> None:
        """They are accepted on input and folded. If either were a member here,
        a row could be written under it and the collapse would be undone one
        request at a time."""
        assert "terraform" not in variable_service.VALID_CATEGORIES
        assert "pulumi_config" not in variable_service.VALID_CATEGORIES

    def test_there_are_exactly_four(self) -> None:
        """`native` plus `env` plus the two git-auth ones. Pinned as a set so a
        fifth engine-named category cannot be added without this failing —
        which is the mistake #1898 exists to stop repeating."""
        assert variable_service.VALID_CATEGORIES == frozenset(
            {"native", "env", "git_http_auth", "git_ssh_auth"}
        )


class TestBothInputNamesFoldOntoIt:
    def test_the_tfe_name_folds(self) -> None:
        assert variable_service.canonical_category("terraform") == _NATIVE

    def test_the_short_lived_pulumi_name_folds_too(self) -> None:
        """Folded rather than refused: a `terrapod_variable` resource or a
        script written in the fortnight `pulumi_config` existed keeps applying,
        and lands on the same row it would have."""
        assert variable_service.canonical_category("pulumi_config") == _NATIVE

    def test_the_canonical_name_is_unchanged(self) -> None:
        assert variable_service.canonical_category(_NATIVE) == _NATIVE

    def test_an_absent_category_defaults_to_it(self) -> None:
        assert variable_service.canonical_category(None) == _NATIVE

    def test_but_an_empty_string_is_not_an_absence(self) -> None:
        """A client that sent `"category": ""` asked for something. Deciding it
        meant `native` would be guessing, and it used to be a 422 -- so the fold
        must not quietly widen what is accepted."""
        assert variable_service.canonical_category("") == ""
        with pytest.raises(ValueError):
            variable_service._validated_category("")

    def test_an_unrelated_category_is_untouched(self) -> None:
        """Folding must not reach past its own alias table, or `env` would
        quietly become a native variable."""
        for other in ("env", "git_http_auth", "git_ssh_auth", "nonsense"):
            assert variable_service.canonical_category(other) == other

    def test_the_write_boundary_folds_before_it_validates(self) -> None:
        """Order matters: validating first would reject `terraform` as an
        invalid category, which is the compatibility break the alias exists to
        prevent."""
        assert variable_service._validated_category("terraform") == _NATIVE
        assert variable_service._validated_category("pulumi_config") == _NATIVE

    def test_a_genuinely_invalid_category_is_still_refused(self) -> None:
        with pytest.raises(ValueError):
            variable_service._validated_category("nonsense")


class TestEachSurfaceGetsItsOwnName:
    def test_the_tfe_surface_says_terraform(self) -> None:
        """`tfci` and `go-tfe` have this as a constant. An honest name here
        would be a compatibility break on a frozen surface."""
        assert (
            variables_router._var_json(_var(), tfe_surface=True)["attributes"]["category"]
            == "terraform"
        )

    def test_the_native_surface_says_native(self) -> None:
        assert (
            variables_router._var_json(_var(), tfe_surface=False)["attributes"]["category"]
            == _NATIVE
        )

    def test_the_default_is_the_compatible_name(self) -> None:
        """So a caller that forgets to thread the request cannot break a CLI —
        it can only be too conservative."""
        assert variables_router._var_json(_var())["attributes"]["category"] == "terraform"

    @pytest.mark.parametrize(
        "path,tfe",
        [
            ("/api/tfe/v2/workspaces/ws-1/vars", True),
            ("/api/v2/workspaces/ws-1/vars", True),
            ("/api/v1/workspaces/ws-1/vars", False),
            ("/api/terrapod/v1/workspaces/ws-1/vars", False),
        ],
    )
    def test_the_door_the_request_came_through_decides(self, path, tfe) -> None:
        """All four prefixes serve these routes — two canonical, two deprecated
        aliases — and the category is the one field whose spelling differs. A
        prefix missed here silently sends a client the other surface's name."""
        assert variables_router._is_tfe(_request(path)) is tfe

    def test_no_request_reads_as_the_compatible_surface(self) -> None:
        assert variables_router._is_tfe(None) is True

    def test_only_the_native_category_is_renamed(self) -> None:
        """`env` and the git-auth pair mean the same thing on both surfaces and
        must not acquire a second spelling."""
        for category in ("env", "git_http_auth", "git_ssh_auth"):
            for tfe in (True, False):
                assert variable_service.wire_category(category, tfe_surface=tfe) == category


@pytest.mark.asyncio
class TestTheRunWireCarriesOneList:
    async def test_every_engine_reads_the_same_list(self) -> None:
        """The proof that the two categories were always one: `pulumi_config`
        carried the same four source fields a `terraform` variable did, so a
        Pulumi run and a Terraform run take the identical entry and deliver it
        differently."""
        for engine in ("terraform", "pulumi"):
            attrs = await _claim([_rv("region", "eu-west-1")], engine=engine)
            assert [v["key"] for v in attrs["terraform-vars"]] == ["region"]

    async def test_sensitive_rides_along(self) -> None:
        """Added for the engines that can honour it: Pulumi turns it into
        `--secret`, which makes its own engine render `[secret]` in the preview
        and the state. Terraform ignores it, because there the file is the
        mechanism and every value is written the same way."""
        attrs = await _claim(
            [_rv("region", "eu-west-1"), _rv("dbpass", "sup3rs3cret", sensitive=True)]
        )
        by_key = {v["key"]: v for v in attrs["terraform-vars"]}
        assert by_key["dbpass"]["sensitive"] is True
        assert by_key["region"]["sensitive"] is False

    async def test_structured_keeps_both_spellings(self) -> None:
        """`hcl` is `structured`'s permanent wire twin (#1435); a runner up to
        N-2 minors behind reads the older one."""
        (entry,) = (await _claim([_rv("outer.inner", "nested", structured=True)]))["terraform-vars"]
        assert entry["structured"] is True and entry["hcl"] is True

    async def test_keys_are_sent_verbatim(self) -> None:
        """#1407 §6 makes this a negative rule: never prefix. Pulumi's CLI
        namespaces an unqualified key to the project itself, and `aws:region`
        must survive untouched."""
        attrs = await _claim([_rv("aws:region", "eu-west-1"), _rv("region", "x")])
        assert [v["key"] for v in attrs["terraform-vars"]] == ["aws:region", "region"]

    async def test_env_vars_stay_on_their_own_list(self) -> None:
        """`env` is a different role — it reaches the process environment
        whatever engine runs — so collapsing native categories must not
        collapse this one too."""
        attrs = await _claim([_rv("region", "eu-west-1"), _rv("TOKEN", "t", category="env")])
        assert [v["key"] for v in attrs["terraform-vars"]] == ["region"]
        assert [v["key"] for v in attrs["env-vars"]] == ["TOKEN"]

    async def test_there_is_no_second_config_list(self) -> None:
        """#1565's `pulumi-config` is gone. Pinned because re-adding one is the
        exact regression #1898 undid, and it would present as Pulumi config
        arriving twice rather than as an error."""
        assert "pulumi-config" not in await _claim([_rv("region", "eu-west-1")])


class TestTheRemovedMachineryStaysRemoved:
    """Three things existed only because a variable could sit in a category the
    workspace's engine would never read. With one category that state cannot
    arise, so the machinery that surfaced it is not merely unused — it would be
    describing something impossible."""

    def test_no_write_path_refuses_a_category_for_its_engine(self) -> None:
        """A 422 was written first and removed: #1407 §6 settles that engine
        mismatch is permissive, and a write-time check couples two things that
        need no coupling."""
        import inspect

        assert "_reject_category_on_wrong_engine" not in inspect.getsource(variables_router)

    def test_a_variable_no_longer_claims_an_engine(self) -> None:
        assert "applies-to-engine" not in _var_attributes()

    def test_no_health_condition_reports_inert_variables(self) -> None:
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        assert "variables_not_consumed" not in src
        assert "_resolve_inert_var_ws" not in src

    def test_nothing_asks_which_engine_consumes_a_category(self) -> None:
        assert not hasattr(variable_service, "consumed_by_engine")
        assert not hasattr(variable_service, "CATEGORY_ENGINE")
        assert not hasattr(variable_service, "PULUMI_CONFIG_CATEGORY")


class TestTheEngineGateIsUnaffected:
    """#1429 requires every Pulumi-serving surface to be gated. The category is
    not one: it is the same category a Terraform workspace uses, so gating it
    would take Terraform's variables away with Pulumi's.

    The gating that matters is structural and unchanged — a Pulumi workspace can
    only be created on the native surface, which validates the engine against
    `known_engines()`, so with Pulumi off there is no Pulumi workspace and
    nothing that delivers a variable as stack config.
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

    def test_the_category_stays_valid_whatever_the_gate_says(self) -> None:
        """It always was the Terraform category. Refusing it with Pulumi off
        would break every Terraform workspace on the deployment."""
        from terrapod.config import settings

        before = settings.engines.pulumi.enabled
        try:
            for state in (False, True):
                settings.engines.pulumi.enabled = state
                assert _NATIVE in variable_service.VALID_CATEGORIES
        finally:
            settings.engines.pulumi.enabled = before


@pytest.mark.asyncio
class TestTheWireToleratesEitherStoredSpelling:
    """A rolling upgrade runs old and new API replicas against one database, so
    an older replica can still WRITE `terraform` while a newer one reads it.

    A bare `== "native"` filter would drop that variable from the run without a
    word — the silent-delivery failure #1898 exists to remove, reintroduced by
    the fix for it. So the wire folds the category rather than comparing it.
    """

    async def test_a_row_written_under_the_old_name_is_still_delivered(self) -> None:
        attrs = await _claim([_rv("region", "eu-west-1", category="terraform")])
        assert [v["key"] for v in attrs["terraform-vars"]] == ["region"]

    async def test_and_one_written_under_the_short_lived_name_too(self) -> None:
        attrs = await _claim([_rv("region", "eu-west-1", category="pulumi_config")])
        assert [v["key"] for v in attrs["terraform-vars"]] == ["region"]

    async def test_an_unrelated_category_is_still_excluded(self) -> None:
        """The tolerance must not become a catch-all: `env` has its own list and
        reaching it through this one would deliver it twice."""
        attrs = await _claim([_rv("TOKEN", "t", category="env")])
        assert attrs["terraform-vars"] == []
