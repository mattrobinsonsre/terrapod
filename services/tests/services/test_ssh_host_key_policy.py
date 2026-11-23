"""The three-state SSH host-key policy, end to end on the server side (#2592).

OpenSSH has three behaviours and ansible can express two, which is the whole
reason this is not the boolean it replaced. `accept-new` -- trust on first
contact, refuse on change -- is the state a growing fleet needs once approved
keys exist, because `ssh_args` is process-wide: with only `yes` and `no`,
declaring one approved key makes every unpinned ADDRESS in that inventory
unreachable.

Four properties, and the middle two are the ones a boolean could not have:

* the **rule** refuses anything outside the three, and refuses a BOOLEAN with a
  message naming the attribute it replaced -- a caller written against the old
  API sends `true`, and a message listing three strings with no hint that the
  spelling is retired is the difference between fixing a call and guessing;
* the **anchor** renders each policy as the `StrictHostKeyChecking` OpenSSH
  takes, and `accept-new` KEEPS the approved-keys file while `off` points at
  `/dev/null`. Those two are the point of the state existing: a pinned host is
  verified strictly against the file and an unpinned one is still reachable;
* the **runner** reads it from the Job's own environment, with `strict` when
  absent -- the safe direction for a runner claiming a run from an API that
  predates the policy;
* the **run** snapshots it, so a workspace edited mid-queue cannot change what a
  queued run enforces.

The behavioural half of that last one -- editing the workspace after creation
leaves the run's policy alone -- is in
`tests/integration/test_native_workspace_routes.py`, because it is a CONTRAST
between two live rows and a mocked snapshot cannot see it.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from terrapod.db.models import (
    DEFAULT_SSH_HOST_KEY_POLICY,
    SSH_HOST_KEY_POLICIES,
    SSH_HOST_KEY_POLICY_TO_OPENSSH,
)
from terrapod.engines.pulumi import PulumiStrategy
from terrapod.engines.terraform import TerraformStrategy
from terrapod.runner.phases.configure_known_hosts import ANSIBLE_DEFAULT_SSH_ARGS, ssh_args
from terrapod.runner.runner_config import RunnerConfig
from terrapod.services import workspace_settings as ws

ANCHOR = pathlib.Path("/tp/known-hosts/terrapod-known-hosts")

_ENV = {
    "TP_RUN_ID": "run-1",
    "TP_PHASE": "configure",
    "TP_WORKSPACE_ID": "ws-1",
    "TP_API_URL": "http://api",
    "TP_AUTH_TOKEN": "t",
}


class TestTheRule:
    """`validate_ssh_host_key_policy`, which is what keeps a CHECK violation from
    reaching the caller as a 500."""

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    def test_every_policy_the_model_offers_is_accepted_unchanged(self, policy):
        """Derived from the model rather than listed, so a fourth state cannot be
        added to the column while the validator goes on refusing it."""
        assert ws.validate_ssh_host_key_policy(policy) == policy

    def test_there_are_exactly_three_because_that_is_the_whole_argument(self):
        assert len(SSH_HOST_KEY_POLICIES) == 3
        assert set(SSH_HOST_KEY_POLICIES) == {"strict", "accept-new", "off"}

    @pytest.mark.parametrize("raw", [True, False])
    def test_a_boolean_is_refused_and_the_message_names_the_retired_attribute(self, raw):
        """The deliberate migration aid, and the reason it is a separate branch.

        `bool` is not in the set, so a bare membership test refuses it anyway --
        with a message naming three strings and no hint that the caller is using
        a spelling that no longer exists. The MESSAGE is the feature here, so the
        message is what is pinned: a caller written against
        `ansible-host-key-checking` sends `true`, and needs to be told which of
        the three it now means.
        """
        with pytest.raises(ValueError) as exc:
            ws.validate_ssh_host_key_policy(raw)
        message = str(exc.value)
        assert "ansible-host-key-checking" in message, (
            "a caller sending a boolean is using the retired attribute; naming it "
            "is the difference between fixing the call and guessing"
        )
        assert "not a boolean" in message
        # And it must say which state each of the old values became, or the
        # operator still has to guess.
        assert "'strict'" in message and "true" in message
        assert "'off'" in message and "false" in message

    def test_the_generic_refusal_does_NOT_name_the_retired_attribute(self):
        """The control for the test above, and what makes its assertion bind.

        A reader is rightly tempted to delete the boolean branch as redundant --
        `bool` is not in the tuple, so the generic path refuses it anyway and the
        status code is the same. What would be lost is the MESSAGE, and the test
        above can only prove that if the two messages differ: were the generic
        message to mention the retired attribute as well, that test would pass
        with the branch gone and would be asserting nothing.
        """
        with pytest.raises(ValueError) as exc:
            ws.validate_ssh_host_key_policy("yes")
        assert "ansible-host-key-checking" not in str(exc.value), (
            "the two refusals must differ, or the boolean test above passes for the wrong reason"
        )

    @pytest.mark.parametrize("raw", ["yes", "no", "accept_new", "ask"])
    def test_the_openssh_and_ansible_spellings_are_refused(self, raw):
        """We take the POLICY name, not the value we render it as.

        `yes`/`no` are what `StrictHostKeyChecking` takes and `ask` is OpenSSH's
        fourth; accepting any of them would give two spellings for one state and
        let a caller contradict itself. `accept_new` is the underscore slip.
        """
        with pytest.raises(ValueError, match="must be 'strict', 'accept-new', or 'off'"):
            ws.validate_ssh_host_key_policy(raw)

    @pytest.mark.parametrize("raw", ["", None])
    def test_neither_spelling_of_empty_is_an_inherit_marker(self, raw):
        """Unlike `engine_version`, where `""` means "the deployment's default".

        There is no deployment-wide host-key policy to inherit -- the column is
        `NOT NULL` with a `strict` default -- so an empty value is a caller
        clearing a field that cannot be cleared, and folding it to `strict`
        silently would make a mistyped payload look accepted.
        """
        with pytest.raises(ValueError):
            ws.validate_ssh_host_key_policy(raw)

    @pytest.mark.parametrize("raw", ["STRICT", "Strict", "Accept-New", "OFF", " strict "])
    def test_case_and_whitespace_are_refused_rather_than_normalised(self, raw):
        """Validate, never transform: the Terraform provider writes the server's
        answer back into state, so normalising a configured value is "Provider
        produced inconsistent result after apply" on every apply."""
        with pytest.raises(ValueError):
            ws.validate_ssh_host_key_policy(raw)

    @pytest.mark.parametrize("raw", [1, 0, 1.5, ["strict"], {"policy": "strict"}, object()])
    def test_a_non_string_is_refused(self, raw):
        with pytest.raises(ValueError):
            ws.validate_ssh_host_key_policy(raw)

    def test_the_default_is_the_behaviour_that_already_existed(self):
        """`strict` rather than a new restriction: ansible's own
        `HOST_KEY_CHECKING` defaults true, so first contact with an unseen host
        already failed closed."""
        assert DEFAULT_SSH_HOST_KEY_POLICY == "strict"
        assert DEFAULT_SSH_HOST_KEY_POLICY in SSH_HOST_KEY_POLICIES


class TestTheAnchorRendersThePolicy:
    """`ssh_args(path, policy=...)` -- what actually reaches `ssh`."""

    @pytest.mark.parametrize(("policy", "strict"), sorted(SSH_HOST_KEY_POLICY_TO_OPENSSH.items()))
    def test_each_policy_states_the_openssh_value(self, policy, strict):
        """Parametrized over the map rather than over three literals, so a map
        entry that changed without this changing cannot pass."""
        assert f"-o StrictHostKeyChecking={strict}" in ssh_args(ANCHOR, policy=policy)

    def test_strict_points_at_the_approved_keys_file(self):
        assert f"-o UserKnownHostsFile={ANCHOR}" in ssh_args(ANCHOR, policy="strict")

    def test_accept_new_KEEPS_the_approved_keys_file(self):
        """The whole point of the third state existing.

        A host the file pins is verified strictly against it; a host nobody has
        pinned is accepted on first contact rather than refused. Pointing
        `accept-new` at `/dev/null` -- the obvious copy of the `off` branch --
        would make every pinned host unverified, which is the opposite of what an
        operator asked for and would look identical in the run log.
        """
        args = ssh_args(ANCHOR, policy="accept-new")
        assert f"-o UserKnownHostsFile={ANCHOR}" in args
        assert "/dev/null" not in args
        assert "-o StrictHostKeyChecking=accept-new" in args

    def test_off_points_at_dev_null_rather_than_at_our_file(self):
        """Not merely cosmetic. With verification off, ssh would otherwise write
        every accepted key into a per-run volume that is discarded anyway, and
        warn on each connection -- a line in the operator's log for something
        they asked for."""
        args = ssh_args(ANCHOR, policy="off")
        assert "-o UserKnownHostsFile=/dev/null" in args
        assert str(ANCHOR) not in args
        assert "-o StrictHostKeyChecking=no" in args

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    def test_the_anchor_comes_first_whatever_the_policy(self, policy):
        """OpenSSH takes the FIRST value for each parameter, so position is the
        mechanism. A policy that emitted ansible's default first would be read
        and discarded."""
        args = ssh_args(ANCHOR, policy=policy)
        assert args.index("UserKnownHostsFile") < args.index("ControlMaster")
        assert args.index("StrictHostKeyChecking") < args.index("ControlMaster")

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    def test_ansibles_own_default_survives_every_policy(self, policy):
        """Forcing `ANSIBLE_SSH_ARGS` REPLACES the option rather than adding to
        it, so a policy branch that dropped the default would silently disable
        SSH multiplexing and compression for that policy alone."""
        assert ssh_args(ANCHOR, policy=policy).endswith(ANSIBLE_DEFAULT_SSH_ARGS)

    def test_the_policy_argument_is_required_and_has_no_default(self):
        """A default would make one of the three the bypass every caller takes by
        omission -- and the caller that matters is the entrypoint threading the
        run's snapshot through, which is exactly the one a silent default would
        let somebody forget to wire up.

        Asserted on the signature because the defect is an ADDED default, which
        no behavioural test can see: every existing call would go on passing.
        """
        policy = inspect.signature(ssh_args).parameters["policy"]
        assert policy.default is inspect.Parameter.empty, (
            "`policy` has acquired a default; one of the three states is now what "
            "a caller gets for forgetting to pass it"
        )
        assert policy.kind is inspect.Parameter.KEYWORD_ONLY, (
            "keyword-only, so it cannot be passed positionally where the path goes"
        )

    def test_an_unknown_policy_raises_rather_than_emitting_nothing(self):
        """Fail closed on a value the column should never hold. Emitting no
        `StrictHostKeyChecking` would leave OpenSSH's own `ask`, which refuses
        non-interactively only by accident of there being no tty."""
        with pytest.raises(KeyError):
            ssh_args(ANCHOR, policy="whatever")


class TestTheRunnerReadsItFromItsJob:
    """`RunnerConfig`, where the run's snapshot arrives as `TP_SSH_HOST_KEY_POLICY`."""

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    def test_the_policy_is_read_from_the_supplied_environment(self, policy):
        cfg = RunnerConfig.from_env({**_ENV, "TP_SSH_HOST_KEY_POLICY": policy})
        assert cfg.ssh_host_key_policy == policy

    def test_it_is_read_from_the_MAPPING_and_not_the_process_environment(self, monkeypatch):
        """The regression test for a real defect in the first implementation.

        `from_env` binds `e = env if env is not None else os.environ` and every
        other field reads `e`. This one read `os.environ` directly, so a caller
        handing it an explicit mapping could not set the policy at all -- it was
        `strict` whatever the mapping said, unless the ambient environment
        happened to carry the variable, in which case the ambient value won.

        Both directions are wrong and both are invisible in production, where
        `os.environ` IS the source: it bites only the explicit-mapping callers,
        which is to say the tests that would otherwise have caught it.
        """
        monkeypatch.setenv("TP_SSH_HOST_KEY_POLICY", "off")

        from_mapping = RunnerConfig.from_env({**_ENV, "TP_SSH_HOST_KEY_POLICY": "accept-new"})
        assert from_mapping.ssh_host_key_policy == "accept-new", (
            "the supplied mapping must win; reading os.environ here makes the "
            "mapping-driven path untestable and leaks the ambient environment"
        )

        silent = RunnerConfig.from_env(_ENV)
        assert silent.ssh_host_key_policy == "strict", (
            "a mapping that says nothing means `strict`, not whatever the process "
            "that happens to be running the test has in its environment"
        )

    @pytest.mark.parametrize("raw", [None, ""])
    def test_absent_or_empty_means_strict(self, raw):
        """The safe direction for a runner claiming a run from an API that
        predates the policy: it enforces rather than relaxing."""
        env = dict(_ENV)
        if raw is not None:
            env["TP_SSH_HOST_KEY_POLICY"] = raw
        assert RunnerConfig.from_env(env).ssh_host_key_policy == "strict"


class TestTheEnginesSendItOnEveryRun:
    """Both strategies, because a Pulumi workspace's configure runs the same phase."""

    @staticmethod
    def _env(strategy, options) -> dict[str, str]:
        """`container_env`'s second argument is the LISTENER's `RunnerConfig` --
        the Helm-fed `terrapod.config` one, not the Job's -- and these tests have
        no opinion about the version defaults it supplies. Stubbed the way the
        existing engine tests stub it: `MagicMock()` for Terraform, which reads
        `default_terraform_version` off it, and `None` for Pulumi, which does not.
        """
        from unittest.mock import MagicMock

        cfg = None if isinstance(strategy, PulumiStrategy) else MagicMock()
        return {e["name"]: e["value"] for e in strategy.container_env(options, cfg)}

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    def test_terraform_emits_it_for_every_policy_including_the_default(self, policy):
        """UNCONDITIONAL, unlike `configure_parallelism` beside it, and the
        direction is why: the runner's own fallback is `strict`, so an API that
        stopped sending the key would silently TIGHTEN every run rather than
        leaving a visible default in place. A conditional emit on anything but
        `strict` would be indistinguishable from that."""
        opts = TerraformStrategy().options_from_attrs({"ssh-host-key-policy": policy}, "configure")
        assert self._env(TerraformStrategy(), opts)["TP_SSH_HOST_KEY_POLICY"] == policy

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    def test_pulumi_emits_it_for_every_policy_too(self, policy):
        opts = PulumiStrategy().options_from_attrs({"ssh-host-key-policy": policy}, "configure")
        assert self._env(PulumiStrategy(), opts)["TP_SSH_HOST_KEY_POLICY"] == policy

    @pytest.mark.parametrize("strategy", [TerraformStrategy(), PulumiStrategy()])
    def test_an_absent_key_falls_back_to_strict(self, strategy):
        """An API older than #2592 sends no such key."""
        opts = strategy.options_from_attrs({}, "configure")
        assert opts.ssh_host_key_policy == "strict"

    @pytest.mark.parametrize("strategy", [TerraformStrategy(), PulumiStrategy()])
    def test_an_explicit_null_falls_back_to_strict(self, strategy):
        """A run created before the column carries `ssh-host-key-policy: null`.

        `or` the default rather than `.get(key, default)`, so both shapes land on
        the same value -- a `.get` with a default returns `None` for the second.
        """
        opts = strategy.options_from_attrs({"ssh-host-key-policy": None}, "configure")
        assert opts.ssh_host_key_policy == "strict"

    @pytest.mark.parametrize("strategy", [TerraformStrategy(), PulumiStrategy()])
    def test_the_fallback_is_the_ENFORCING_state(self, strategy):
        """Stated separately because `strict` being the fallback is the whole
        safety argument, and a fallback of `off` would pass every test above."""
        opts = strategy.options_from_attrs({}, "configure")
        assert SSH_HOST_KEY_POLICY_TO_OPENSSH[opts.ssh_host_key_policy] == "yes"


@pytest.mark.asyncio
class TestTheRunSnapshotsItAtCreation:
    """`create_run` copies the workspace's policy onto the run.

    Snapshotted like `configure_parallelism` beside it for a reason they share --
    a run should behave as it was created -- and one they do not: this is a
    SECURITY posture, so making it live would let somebody relax verification
    under a configure already in flight.

    The approved KEYS are read the opposite way, LIVE, so a key marked `revoked`
    while a run sits queued is refused by that run. Both are the safe direction,
    which is why they differ, and the behavioural contrast -- editing the
    workspace after creation -- is in
    `tests/integration/test_native_workspace_routes.py`, because a mocked
    snapshot cannot see it.
    """

    @pytest.fixture(autouse=True)
    def _stub_post_plan_task_stages(self):
        """`create_run` reserves a post-plan task stage now that
        `runs.tfe_post_plan_decisions` defaults on (#2425). These tests drive it
        against a narrow mocked DB and have no opinion about stages."""
        from tests.post_plan_stubs import stub_run_lifecycle_task_stages

        with stub_run_lifecycle_task_stages():
            yield

    @staticmethod
    async def _run_kwargs(policy: str) -> dict:
        """The kwargs `Run(...)` was built with, for a workspace on `policy`."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import AsyncSession

        from terrapod.services import inventory_resolve
        from terrapod.services.run_service import create_run

        workspace = MagicMock()
        workspace.id = uuid.uuid4()
        workspace.name = "policy-ws"
        workspace.auto_apply = False
        workspace.engine_version = "1.11"
        workspace.terragrunt_enabled = False
        workspace.resource_cpu = "1"
        workspace.resource_memory = "2Gi"
        workspace.ee_resource_cpu = ""
        workspace.ee_resource_memory = ""
        workspace.parallelism = 10
        workspace.configure_parallelism = None
        workspace.agent_pool_links = []
        workspace.plan_apply_enabled = True
        workspace.configure_enabled = True
        workspace.configure_after_apply = False
        workspace.auto_configure_enabled = False
        workspace.ansible_version = ""
        workspace.ansible_execution_environment = ""
        # The manual padlock, explicit for the reason the sibling fixture in
        # `test_configure_ee_image.py` records: left as a MagicMock attribute it
        # is TRUTHY, so `create_run` would refuse every run built here as
        # locked (#1975). Do not delete as noise.
        workspace.locked = False
        workspace.ssh_host_key_policy = policy

        db = AsyncMock(spec=AsyncSession)
        with (
            patch("terrapod.services.run_service.Run") as MockRun,
            patch.object(
                inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", {}))
            ),
        ):
            instance = MockRun.return_value
            instance.id = uuid.uuid4()
            instance.status = "pending"
            await create_run(db, workspace, message="m")
            MockRun.assert_called_once()
            return MockRun.call_args[1]

    @pytest.mark.parametrize("policy", SSH_HOST_KEY_POLICIES)
    async def test_the_run_carries_the_workspaces_policy(self, policy):
        """Every state, including the two a boolean could not hold on its own.

        Parametrized rather than driven with one value because a snapshot that
        hardcoded `strict` would pass a single-value test while losing the
        setting for exactly the workspaces that bothered to set it.
        """
        assert (await self._run_kwargs(policy))["ssh_host_key_policy"] == policy

    async def test_a_relaxed_workspace_is_not_quietly_tightened(self):
        """The direction that would be invisible.

        A `create_run` that dropped the value would leave the column's `strict`
        default in place, so a workspace deliberately set to `off` would produce
        runs that refuse every host it has no approved key for -- arriving as
        `Host key verification failed` on a workspace nobody changed, which
        points at the hosts rather than at the run.
        """
        assert (await self._run_kwargs("off"))["ssh_host_key_policy"] != "strict"
