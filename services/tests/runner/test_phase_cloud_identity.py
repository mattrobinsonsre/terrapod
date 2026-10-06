"""The runner's cloud-identity credential phase (#1901).

The phase makes two kinds of call and the split is the design: it asks the API
which provider configurations this run mints for, and only then asks the engine
which of them the root module actually uses. A workspace that mints nothing
never reaches the engine at all, which is what keeps the feature from adding
cost or a failure mode to the overwhelming majority of runs.

The outcomes that must stay apart:

* mints nothing (204, an empty list, or a 404 from an API that predates the
  feature) -- no env, no files, no engine invocation, and the run proceeds on
  the agent pool's identity;
* mints and succeeds -- one private file per used target, under its own name;
* mints and fails -- raises, because falling through does not mean no
  credentials, it means the pool's, which are broader than the ones the
  workspace was deliberately moved off.
"""

import os
import stat

import httpx
import pytest

from terrapod.runner.phases import cloud_identity

# A real graph fragment, in the engine's own escaping. Two configurations of one
# provider plus a second provider, with `null.third` and `random.eu` absent --
# the engine prunes configurations nothing references, which is the property
# discovery relies on.
GRAPH = r"""digraph {
	compound = "true"
	newrank = "true"
	subgraph "root" {
		"[root] provider[\"registry.opentofu.org/hashicorp/aws\"]" [label = "provider[\"registry.opentofu.org/hashicorp/aws\"]", shape = "diamond"]
		"[root] provider[\"registry.opentofu.org/hashicorp/aws\"].west" [label = "provider[\"registry.opentofu.org/hashicorp/aws\"].west", shape = "diamond"]
		"[root] provider[\"registry.opentofu.org/hashicorp/vault\"]" [label = "provider[\"registry.opentofu.org/hashicorp/vault\"]", shape = "diamond"]
		"[root] aws_instance.a" -> "[root] provider[\"registry.opentofu.org/hashicorp/aws\"]"
	}
}
"""


def _cfg(**overrides):
    from terrapod.runner.runner_config import RunnerConfig as RC

    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
        "TP_PHASE": "plan",
    }
    base.update(overrides)
    return RC.from_env(env=base)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.com")


def _api(*, targets, token_for=None, phase="plan"):
    """An API that lists `targets` and mints for each one in `token_for`.

    `token_for` defaults to every listed target. A target listed but absent from
    it answers 204, which is the mapping having lost it between the two calls.
    """
    listed = (targets or []) if token_for is None else token_for
    minted = {t: f"jwt-for-{t}" for t in listed}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cloud-identity-targets"):
            seen.append("targets")
            if targets is None:
                return httpx.Response(204)
            return httpx.Response(200, json={"targets": list(targets)})
        target = request.url.params.get("target", "")
        seen.append(f"mint:{target}")
        if target not in minted:
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "token": minted[target],
                "target": target,
                "phase": phase,
                "audiences": [f"aud-{target}"],
                "expires_in": 900,
            },
        )

    return handler, seen


def _never_discover(**_kwargs):
    raise AssertionError(
        "the engine must not be invoked when the workspace mints nothing — that "
        "gating is what keeps this feature free for runs that do not use it"
    )


def _discover(targets):
    def discover(*, binary, cwd):
        return set(targets)

    return discover


class TestParsingTheGraph:
    """Discovery reads the engine's own output, so pin it against a real one."""

    def test_the_provider_configurations_are_read_with_their_aliases(self):
        assert cloud_identity.parse_graph(GRAPH) == {"aws", "aws.west", "vault"}

    def test_a_pruned_configuration_is_absent(self):
        """`null.third` and `random.eu` were declared in the fixture's source
        and referenced by nothing, so the engine never emits them. That is what
        makes an unused aliased provider correctly mint no token."""
        found = cloud_identity.parse_graph(GRAPH)
        assert "null.third" not in found
        assert "random.eu" not in found

    def test_the_bare_type_is_used_not_the_source_address(self):
        """An operator writes `provider "aws"`, so the configuration key they
        can be expected to use is the bare type, never the registry address."""
        assert all("/" not in t for t in cloud_identity.parse_graph(GRAPH))

    def test_output_with_no_provider_node_reads_as_empty(self):
        assert cloud_identity.parse_graph('digraph { "[root] x" -> "[root] y" }') == set()


class TestTheWorkspaceMintsNothing:
    def test_a_204_returns_no_env_and_never_touches_the_engine(self, tmp_path):
        handler, seen = _api(targets=None)
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover=_never_discover,
        )
        assert env == {}
        assert seen == ["targets"]
        assert not (tmp_path / "oidc").exists()

    def test_an_empty_target_list_returns_no_env(self, tmp_path):
        handler, _ = _api(targets=[])
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover=_never_discover,
        )
        assert env == {}

    def test_a_404_falls_through_because_the_api_predates_the_feature(self, tmp_path):
        """Agent pools upgrade independently of the control plane, so a runner
        newer than its API is a real posture. The route's absence is
        information, not a fault — and failing here would fail every run in a
        fleet whose API has not caught up."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="Not Found")

        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover=_never_discover,
        )
        assert env == {}

    def test_no_api_configured_returns_no_env(self, tmp_path):
        env = cloud_identity.run(
            _cfg(TP_API_URL=""),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            discover=_never_discover,
        )
        assert env == {}

    def test_nothing_used_overlaps_what_is_configured(self, tmp_path):
        """Configured for `gcp`, root module uses `aws`. Not an error: the
        mapping is per workspace and a configuration need not use every
        provider in it."""
        handler, seen = _api(targets=["gcp"])
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover=_discover({"aws"}),
        )
        assert env == {}
        assert seen == ["targets"]


class TestOneFilePerTarget:
    def _run(self, tmp_path, *, targets, used, token_for=None):
        handler, seen = _api(targets=targets, token_for=token_for)
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover=_discover(used),
        )
        return env, seen

    def test_each_used_target_gets_its_own_file_under_its_own_name(self, tmp_path):
        env, _ = self._run(
            tmp_path, targets=["aws", "aws.west", "vault"], used={"aws", "aws.west", "vault"}
        )
        assert env[cloud_identity.TOKEN_DIR_ENV] == str(tmp_path / "oidc")
        for target in ("aws", "aws.west", "vault"):
            assert (tmp_path / "oidc" / target / "token").read_text() == f"jwt-for-{target}"

    def test_no_combined_token_file_is_written(self, tmp_path):
        """A token audienced for several targets is replayable between them, and
        AWS refuses a multi-valued `aud` outright — so there is deliberately no
        shared file beside the per-target ones."""
        self._run(tmp_path, targets=["aws", "vault"], used={"aws", "vault"})
        assert not (tmp_path / "oidc" / "token").exists()
        assert sorted(p.name for p in (tmp_path / "oidc").iterdir()) == ["aws", "vault"]

    def test_only_the_intersection_is_minted(self, tmp_path):
        """Configured for three, root module uses two: exactly two requests."""
        _, seen = self._run(tmp_path, targets=["aws", "vault", "gcp"], used={"aws", "vault"})
        assert seen == ["targets", "mint:aws", "mint:vault"]

    def test_an_unused_alias_mints_nothing(self, tmp_path):
        _, seen = self._run(tmp_path, targets=["aws", "aws.west"], used={"aws"})
        assert seen == ["targets", "mint:aws"]
        assert not (tmp_path / "oidc" / "aws.west").exists()

    def test_every_file_is_private(self, tmp_path):
        self._run(tmp_path, targets=["aws", "vault"], used={"aws", "vault"})
        for target in ("aws", "vault"):
            mode = (tmp_path / "oidc" / target / "token").stat().st_mode
            assert stat.S_IMODE(mode) == 0o600

    def test_the_phase_is_exported_both_ways(self, tmp_path):
        """HCL cannot otherwise see which phase it is in, and switching role by
        phase is the whole point of the apply increment."""
        env, _ = self._run(tmp_path, targets=["aws"], used={"aws"})
        assert env[cloud_identity.PHASE_ENV] == "plan"
        assert env[cloud_identity.PHASE_TFVAR_ENV] == "plan"

    def test_the_directory_is_exported_as_a_tfvar_too(self, tmp_path):
        """So a configuration builds `"${var.terrapod_oidc_token_dir}/aws/token"`
        rather than hard-coding the path."""
        env, _ = self._run(tmp_path, targets=["aws"], used={"aws"})
        assert env[cloud_identity.TOKEN_DIR_TFVAR_ENV] == str(tmp_path / "oidc")

    def test_no_per_cloud_env_is_set(self, tmp_path):
        """Setting AWS_WEB_IDENTITY_TOKEN_FILE and friends is what would break
        coexistence with the pod's own IRSA: the Job spec carries no cloud
        credential env at all, and absence is what leaves a non-federating
        workspace's pool identity untouched."""
        env, _ = self._run(tmp_path, targets=["aws"], used={"aws"})
        forbidden = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "VAULT_")
        assert not [k for k in env if k.startswith(forbidden)]

    def test_a_target_that_stops_mapping_mid_phase_is_skipped_not_fatal(self, tmp_path):
        """The two calls are not atomic. A 204 on the mint means nothing maps to
        that target now, which is the same answer as never having been
        configured for it — so the other targets still deliver."""
        env, seen = self._run(
            tmp_path, targets=["aws", "vault"], used={"aws", "vault"}, token_for=["aws"]
        )
        assert env[cloud_identity.TOKEN_DIR_ENV] == str(tmp_path / "oidc")
        assert (tmp_path / "oidc" / "aws" / "token").exists()
        assert not (tmp_path / "oidc" / "vault").exists()
        assert seen == ["targets", "mint:aws", "mint:vault"]

    def test_every_target_losing_its_mapping_returns_no_env(self, tmp_path):
        env, _ = self._run(tmp_path, targets=["aws"], used={"aws"}, token_for=[])
        assert env == {}

    def test_no_token_ever_reaches_a_log(self, tmp_path, capsys):
        """The runner streams stdout verbatim into the run log, so a JWT in a
        log line is a credential in a run log a reader with read access can
        fetch. The audiences are logged; the token is not."""
        self._run(tmp_path, targets=["aws", "vault"], used={"aws", "vault"})
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "jwt-for-aws" not in combined
        assert "jwt-for-vault" not in combined


class TestItMintsAndFails:
    """Every one of these raises. Falling through would hand the run the agent
    pool's identity, which is broader than the one the workspace was moved off,
    and the run would then succeed against real infrastructure."""

    def _expect_raise(self, tmp_path, handler, *, discover=None):
        with pytest.raises(cloud_identity.CloudIdentityUnavailable) as exc:
            cloud_identity.run(
                _cfg(),
                binary="tofu",
                cwd=tmp_path,
                token_dir=tmp_path / "oidc",
                client=_client(handler),
                discover=discover or _discover({"aws"}),
            )
        return str(exc.value)

    def test_a_500_on_the_targets_request_raises(self, tmp_path):
        """Not advisory. Treating it as such would let anyone able to disrupt
        one call downgrade a workspace to the pool's identity without trace,
        and the runner cannot complete a run without the API in any case."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(500, text="boom")

        msg = self._expect_raise(tmp_path, handler, discover=_never_discover)
        assert "targets" in msg
        assert len(calls) == 3, "retried three times"

    def test_a_500_on_a_mint_raises_naming_the_target(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cloud-identity-targets"):
                return httpx.Response(200, json={"targets": ["aws"]})
            return httpx.Response(500, text="boom")

        assert "'aws'" in self._expect_raise(tmp_path, handler)

    def test_a_4xx_on_a_mint_is_final_and_not_retried(self, tmp_path):
        """A 409 is the configuration having moved under the run. Retrying a
        refusal only delays the failure."""
        mints = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cloud-identity-targets"):
                return httpx.Response(200, json={"targets": ["aws"]})
            mints.append(1)
            return httpx.Response(409, text="configuration changed, queue a new run")

        msg = self._expect_raise(tmp_path, handler)
        assert len(mints) == 1
        assert "409" in msg

    def test_a_connection_error_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        assert self._expect_raise(tmp_path, handler, discover=_never_discover)

    def test_a_200_with_no_token_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cloud-identity-targets"):
                return httpx.Response(200, json={"targets": ["aws"]})
            return httpx.Response(200, json={"target": "aws", "expires_in": 900})

        assert "no token" in self._expect_raise(tmp_path, handler)

    def test_an_unwritable_path_raises_naming_the_path(self, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        handler, _ = _api(targets=["aws"])
        try:
            with pytest.raises(cloud_identity.CloudIdentityUnavailable) as exc:
                cloud_identity.run(
                    _cfg(),
                    binary="tofu",
                    cwd=tmp_path,
                    token_dir=blocked / "oidc",
                    client=_client(handler),
                    discover=_discover({"aws"}),
                )
            assert "aws" in str(exc.value)
        finally:
            os.chmod(blocked, 0o700)

    def test_a_graph_naming_no_provider_at_all_raises(self, tmp_path):
        """The run mints for something and discovery found nothing. A
        configuration that reaches a cloud with no provider configuration does
        not exist, so this is the parser or the engine's output having moved —
        and failing is what stops that becoming a silent fall-through."""
        handler, _ = _api(targets=["aws"])
        msg = self._expect_raise(tmp_path, handler, discover=_discover(set()))
        assert "dependency graph" in msg

    def test_a_discovery_failure_raises(self, tmp_path):
        """By this point the operator has asked for federation, so a graph we
        cannot read means we cannot tell which identity to present."""

        def broken(*, binary, cwd):
            raise cloud_identity.CloudIdentityUnavailable("tofu graph exited 1")

        handler, _ = _api(targets=["aws"])
        assert "graph" in self._expect_raise(tmp_path, handler, discover=broken)
