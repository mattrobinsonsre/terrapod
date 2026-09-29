"""An agent-mode Pulumi run uses Terrapod as its backend (#1879, #1881).

The CLI is pointed at the Pulumi service surface with the run's own token, and
drives the ordinary update lifecycle against it. The Job holds no backend of its
own: there is no passphrase, no imported deployment, no export handed back.

What these tests pin is mostly the *absence* of the file-backend model, because
every piece of it was scaffolding between a stack the Job owned and one Terrapod
owned, and each piece is individually plausible to reintroduce:

  - the stack ref is used as Terrapod spells it, NOT rewritten to
    `organization/<project>/<stack>` — that form was a DIY backend's demand;
  - `select_stack` selects, and never creates: a Pulumi stack IS a Terrapod
    workspace, so stacks come from Terrapod;
  - the backend env is applied over whatever the environment already holds, so a
    workspace variable cannot redirect a run's state (the entrypoint half of that
    invariant is in `test_job_entrypoint.py`).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from terrapod.runner import exec_subprocess
from terrapod.runner.phases import pulumi_exec

API = "https://terrapod.test"
TOKEN = "runtok:run-1:3600:0:sig"

#: The prefix the runner addresses the API by, written out rather than read off
#: the module under test — a test that imports the constant it is checking pins
#: nothing at all.
#:
#: It is the DEPRECATED alias, and that is deliberate (#1528). A runner image is
#: allowed to lag the API by two minors, so it may be talking to a server on
#: either side of the canonical/alias split and only the alias is served by both.
#: Modernising this to `/api/v1` would send every lagging runner's backend calls
#: to a path its server does not serve, which presents as a Pulumi run that
#: cannot reach its state at all.
ALIAS_PREFIX = "/api/terrapod/v1"


class TestTheBackendEnv:
    def test_it_points_the_cli_at_terrapods_pulumi_surface(self) -> None:
        env = pulumi_exec.service_backend_env(API, TOKEN)
        assert env == {
            "PULUMI_BACKEND_URL": f"{API}{ALIAS_PREFIX}/pulumi",
            "PULUMI_ACCESS_TOKEN": TOKEN,
        }

    def test_the_deprecated_alias_is_the_prefix_on_purpose(self) -> None:
        """Pinned on its own so the reason survives a tidy-up.

        `/api/v1` is canonical and this is not it. A runner lags the API by
        design, so the alias is the only prefix both sides of #1528 serve.
        """
        url = pulumi_exec.service_backend_env(API, TOKEN)["PULUMI_BACKEND_URL"]
        assert url == f"{API}/api/terrapod/v1/pulumi"
        assert "/api/v1/pulumi" not in url

    def test_the_plugin_override_goes_through_the_shim(self) -> None:
        """The prefix the two once had to agree on is now the shim's business.

        The CLI is pointed at loopback (#1906) and `CacheProxy` builds the
        upstream URL itself, so the backend is the only half of this pair that
        still names a path on the API.
        """
        override = pulumi_exec.plugin_override_env(API, TOKEN, 7777)[
            "PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"
        ]
        assert override == ".*=http://127.0.0.1:7777"

        proxy = pulumi_exec.CacheProxy(API, TOKEN, "pulumi")
        try:
            assert proxy._upstream == f"{API}{ALIAS_PREFIX}/package-cache/pulumi"
        finally:
            proxy.stop()

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        env = pulumi_exec.service_backend_env(f"{API}/", TOKEN)
        assert env["PULUMI_BACKEND_URL"] == f"{API}{ALIAS_PREFIX}/pulumi"

    def test_no_api_means_no_backend_at_all(self) -> None:
        """A run with no API was never going to store state, and naming a
        backend at `/api/terrapod/v1/pulumi` with no host in front of it would
        fail later and less legibly than leaving the CLI its default."""
        assert pulumi_exec.service_backend_env("", TOKEN) == {}

    def test_without_a_token_the_backend_is_still_named(self) -> None:
        """The URL is what stops the CLI reaching for pulumi.com; an
        unauthenticated call to Terrapod is refused by Terrapod, which is a far
        better failure than a run silently checkpointing somewhere else."""
        env = pulumi_exec.service_backend_env(API, "")
        assert env == {"PULUMI_BACKEND_URL": f"{API}{ALIAS_PREFIX}/pulumi"}
        assert "PULUMI_ACCESS_TOKEN" not in env

    def test_the_two_env_helpers_agree_on_the_token(self) -> None:
        """Both set `PULUMI_ACCESS_TOKEN`, and the entrypoint applies them one
        after the other — so a disagreement would be won silently by whichever
        ran last."""
        backend = pulumi_exec.service_backend_env(API, TOKEN)
        plugins = pulumi_exec.plugin_override_env(API, TOKEN, 1)
        assert backend["PULUMI_ACCESS_TOKEN"] == plugins["PULUMI_ACCESS_TOKEN"] == TOKEN


class TestTheStackReference:
    def test_the_name_is_used_as_terrapod_spells_it(self, monkeypatch) -> None:
        """NOT `organization/proj/dev`.

        The file backend accepted a fully qualified name only under the literal
        organization `organization`, so the old model rewrote every ref on its
        way to the CLI. Against Terrapod the first segment is the organization —
        always `default` — and rewriting it would name a stack that does not
        exist, which the service answers as "no such stack".
        """
        monkeypatch.setenv("TP_PULUMI_STACK", "default/proj/dev")
        assert pulumi_exec.stack_ref() == "default/proj/dev"
        assert not pulumi_exec.stack_ref().startswith("organization/")

    @pytest.mark.parametrize("given", ["default/proj/dev", "dev", "proj/dev"])
    def test_every_shape_passes_through_untouched(self, monkeypatch, given) -> None:
        monkeypatch.setenv("TP_PULUMI_STACK", given)
        assert pulumi_exec.stack_ref() == given

    def test_an_unset_stack_is_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("TP_PULUMI_STACK", raising=False)
        assert pulumi_exec.stack_ref() == ""


class _FakePulumi:
    """Stands in for the CLI, recording the arguments it was given."""

    def __init__(self, exit_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.exit_code = exit_code

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return MagicMock(exit_code=self.exit_code)


@pytest.fixture
def stack_env(monkeypatch):
    monkeypatch.setenv("TP_PULUMI_STACK", "default/proj/dev")
    for var in ("TP_REFRESH", "TP_PARALLELISM", "TP_TARGET_URNS", "TP_REPLACE_URNS"):
        monkeypatch.delenv(var, raising=False)
    for var in ("TP_DESTROY", "TP_REFRESH_ONLY"):
        monkeypatch.delenv(var, raising=False)


class TestSelectingTheStack:
    def test_it_selects_the_run_s_stack_and_returns_the_ref(self, monkeypatch, stack_env) -> None:
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)

        assert pulumi_exec.select_stack("/cache/pulumi") == "default/proj/dev"
        assert fake.calls == [
            ["/cache/pulumi", "stack", "select", "default/proj/dev", "--non-interactive"]
        ]

    def test_it_never_creates_a_stack(self, monkeypatch, stack_env) -> None:
        """A Pulumi stack IS a Terrapod workspace, so stacks come from Terrapod.

        The service surface refuses `POST /api/stacks/{org}/{project}` saying
        exactly that, so a runner reaching for `stack init` — as the file-backend
        model did on every run — would fail against it anyway; worse, an
        `--create` here would make "this run names a stack nobody has made" look
        like success.
        """
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)

        pulumi_exec.select_stack("/cache/pulumi")

        flat = [arg for call in fake.calls for arg in call]
        assert "init" not in flat
        assert "--create" not in flat
        assert "--secrets-provider" not in flat

    def test_no_stack_named_is_fatal_before_the_cli_runs(self, monkeypatch) -> None:
        monkeypatch.delenv("TP_PULUMI_STACK", raising=False)
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)

        with pytest.raises(pulumi_exec.StackError, match="names no stack"):
            pulumi_exec.select_stack("/cache/pulumi")
        assert fake.calls == []

    def test_a_stack_the_cli_cannot_select_is_fatal(self, monkeypatch, stack_env) -> None:
        """The whole point of the pre-flight: "no such stack" at the start of the
        run, in the CLI's own words, rather than part-way through a preview."""
        monkeypatch.setattr(exec_subprocess, "run", _FakePulumi(exit_code=255))

        with pytest.raises(pulumi_exec.StackError, match="default/proj/dev"):
            pulumi_exec.select_stack("/cache/pulumi")

    def test_no_passphrase_is_minted_for_the_run(self, monkeypatch, stack_env) -> None:
        """The file backend needed a secrets provider and so made one per Job.
        One backend with one secrets provider needs none, and a stray
        `PULUMI_CONFIG_PASSPHRASE` would make the CLI try to open service
        ciphertext with a key that cannot possibly work.
        """
        monkeypatch.delenv("PULUMI_CONFIG_PASSPHRASE", raising=False)
        monkeypatch.setattr(exec_subprocess, "run", _FakePulumi())

        pulumi_exec.select_stack("/cache/pulumi")
        assert "PULUMI_CONFIG_PASSPHRASE" not in os.environ


class TestTheStackReachesEveryCommand:
    """`select_stack` is a pre-flight; `--stack` on each command is the binding."""

    def test_the_preview_names_the_stack_as_terrapod_does(self, stack_env) -> None:
        argv = pulumi_exec.preview_argv("", cfg=None)
        assert argv[argv.index("--stack") + 1] == "default/proj/dev"

    def test_the_update_names_the_stack_as_terrapod_does(self, stack_env) -> None:
        argv = pulumi_exec.update_argv("", cfg=None)
        assert argv[argv.index("--stack") + 1] == "default/proj/dev"

    def test_a_bound_update_still_names_it(self, stack_env) -> None:
        argv = pulumi_exec.update_argv("/workspace/plan.json", cfg=None)
        assert argv[argv.index("--stack") + 1] == "default/proj/dev"
        assert "--plan=/workspace/plan.json" in argv

    def test_an_unnamed_stack_adds_no_flag(self, monkeypatch) -> None:
        """`--stack` with nothing after it would be read as the next flag."""
        monkeypatch.delenv("TP_PULUMI_STACK", raising=False)
        assert "--stack" not in pulumi_exec.preview_argv("", cfg=None)


class TestTheFileBackendIsGone:
    """The scaffolding must not grow back (#1881).

    Each of these existed only to bridge a Job-owned stack to a Terrapod-owned
    one, and each has a plausible-looking reason to be reintroduced by someone
    reading an older comment. Naming them here makes their return a test failure
    rather than a silent second state path.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "LocalStack",
            "StackKeys",
            "LocalStackError",
            "local_backend_env",
            "local_stack_ref",
            "prepare_local_stack",
            "export_local_stack",
            "bundle_plan",
            "unbundle_plan",
            "deployment_changed",
            "has_secure_config",
            "reset_secrets_config",
            "read_salt",
            "seed_document",
            "DEFAULT_STATE_DIR",
        ],
    )
    def test_the_helper_is_not_back(self, name) -> None:
        assert not hasattr(pulumi_exec, name), (
            f"`{name}` is file-backend scaffolding. An agent run speaks to one "
            "backend with one secrets provider (#1881); reintroducing this means "
            "the divergent second state path is back."
        )

    def test_the_runner_no_longer_carries_a_deployment_client(self) -> None:
        """The API routes stay — retiring a surface is its own decision — but
        nothing in the Job fetches or uploads a deployment any more."""
        from terrapod.runner.phases import state as state_phase
        from terrapod.runner.phases import uploads

        assert not hasattr(state_phase, "download_pulumi_deployment")
        assert not hasattr(uploads, "upload_pulumi_deployment")
