"""The runner's cloud-identity credential phase (#1901).

Discovery is unconditional and the API is asked exactly once. That ordering is
the design and these tests pin it: which providers a run uses is a property of
the configuration, so only the runner can answer it, and which identities a
workspace holds is a property of the platform, so only the API can -- so the
intersection costs one hop whichever end sends its half. A gate request first
("does this run mint anything?") would make every federated run pay two hops to
save one engine invocation on the runs that are not federated, and the engine's
graph is a static walk needing no network, no credentials and no state.

The outcomes that must stay apart:

* mints nothing (204, no tokens, or a 404 from an API that predates the
  feature) -- no env and no files, and the run proceeds on the agent pool's
  identity;
* mints and succeeds -- one private file per used target, under its own name;
* mints and fails -- raises, because falling through does not mean no
  credentials, it means the pool's, which are broader than the ones the
  workspace was deliberately moved off.

And the one the restructure introduced: because discovery now runs before the
API has said whether anything is configured, a graph the runner could not read
must be *reported* rather than acted on. The runner cannot know whether it
matters; the API can.
"""

import json
import os
import stat
import subprocess

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

# The same graph with the engine no longer escaping the quotes inside the node
# string. Nothing matches, yet `provider[` is plainly there -- which is exactly
# the distinction that separates "this configuration declares no provider" from
# "we can no longer read the graph", and the only reason an empty result can be
# trusted at all.
GRAPH_FORMAT_MOVED = """digraph {
\t\t"[root] provider["registry.opentofu.org/hashicorp/aws"]" [shape = "diamond"]
}
"""

GRAPH_NO_PROVIDERS = 'digraph { "[root] terraform_data.a" -> "[root] root" }'


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


def _api(*, mint=None, phase="plan"):
    """An API that mints for each name in `mint`; `None` answers 204.

    Returns the handler and a list the request bodies land in, so a test can
    assert what the runner actually told the API -- which is the whole contract
    now that the runner reports its discovery rather than acting on it.
    """
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b"{}"))
        if mint is None:
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "tokens": [
                    {"target": t, "token": f"jwt-for-{t}", "audiences": [f"aud-{t}"]} for t in mint
                ],
                "phase": phase,
                "expires_in": 900,
            },
        )

    return handler, sent


def _found(targets=(), *, outcome="ok", detail=""):
    """A canned `Discovery`, with a counter so a test can prove it was called."""
    calls: list[dict] = []

    def discover_fn(*, binary, cwd):
        calls.append({"binary": binary, "cwd": cwd})
        return cloud_identity.Discovery(outcome=outcome, targets=set(targets), detail=detail)

    return discover_fn, calls


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
        assert cloud_identity.parse_graph(GRAPH_NO_PROVIDERS) == set()


class TestClassifyingTheDiscovery:
    """`discover` never raises, and the outcome it reports is the whole basis on
    which the API decides whether an empty answer can be believed."""

    def _proc(self, monkeypatch, *, rc=0, stdout="", stderr="", raises=None):
        def fake_run(*args, **kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(args[0], rc, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(cloud_identity.subprocess, "run", fake_run)

    def test_a_readable_graph_is_ok_and_authoritative(self, monkeypatch, tmp_path):
        self._proc(monkeypatch, stdout=GRAPH)
        found = cloud_identity.discover(binary="tofu", cwd=tmp_path)
        assert found.outcome == "ok"
        assert found.targets == {"aws", "aws.west", "vault"}

    def test_a_configuration_with_no_provider_is_ok_and_empty(self, monkeypatch, tmp_path):
        """A real answer, not a defect: a configuration declaring no provider
        cannot reach a cloud, so it needs no token."""
        self._proc(monkeypatch, stdout=GRAPH_NO_PROVIDERS)
        found = cloud_identity.discover(binary="tofu", cwd=tmp_path)
        assert found.outcome == "ok"
        assert found.targets == set()

    def test_provider_nodes_that_cannot_be_read_are_unparsed_not_empty(self, monkeypatch, tmp_path):
        """The one case a target list cannot express. Reporting it as `ok` with
        no targets would be a silent fall-through to the pool's identity for
        exactly the workspaces that were moved off it."""
        self._proc(monkeypatch, stdout=GRAPH_FORMAT_MOVED)
        found = cloud_identity.discover(binary="tofu", cwd=tmp_path)
        assert found.outcome == "unparsed"
        assert found.targets == set()
        assert "moved" in found.detail

    def test_a_non_zero_exit_is_failed_and_carries_the_reason(self, monkeypatch, tmp_path):
        self._proc(monkeypatch, rc=1, stderr="Error: Could not load plugin")
        found = cloud_identity.discover(binary="tofu", cwd=tmp_path)
        assert found.outcome == "failed"
        assert "Could not load plugin" in found.detail

    def test_a_missing_binary_is_failed_not_an_exception(self, monkeypatch, tmp_path):
        self._proc(monkeypatch, raises=OSError("No such file or directory"))
        found = cloud_identity.discover(binary="tofu", cwd=tmp_path)
        assert found.outcome == "failed"

    def test_a_timeout_is_failed_not_an_exception(self, monkeypatch, tmp_path):
        self._proc(monkeypatch, raises=subprocess.TimeoutExpired("tofu", 180))
        found = cloud_identity.discover(binary="tofu", cwd=tmp_path)
        assert found.outcome == "failed"

    def test_it_never_raises_whatever_the_engine_does(self, monkeypatch, tmp_path):
        """It runs before the API has said whether this workspace holds any
        identity, so raising here would fail runs that configure none."""
        for boom in (
            OSError("boom"),
            subprocess.TimeoutExpired("tofu", 1),
            subprocess.SubprocessError("boom"),
        ):
            self._proc(monkeypatch, raises=boom)
            assert cloud_identity.discover(binary="tofu", cwd=tmp_path).outcome == "failed"


class TestTheEngineIsAlwaysAsked:
    """The restructure, pinned. Discovery is not gated behind a request to the
    API, because only the runner can answer what the configuration uses and
    sending that list up costs one hop rather than two."""

    def test_discovery_runs_even_when_the_workspace_mints_nothing(self, tmp_path):
        handler, sent = _api(mint=None)
        discover_fn, calls = _found({"aws"})
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert env == {}
        assert len(calls) == 1, "the engine is asked before the API, unconditionally"
        assert len(sent) == 1, "and the API is asked exactly once"

    def test_the_discovered_providers_are_what_is_sent(self, tmp_path):
        handler, sent = _api(mint=["aws"])
        discover_fn, _ = _found({"vault", "aws", "aws.west"})
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert sent[0]["providers"] == ["aws", "aws.west", "vault"], "sorted, for a stable log"
        assert sent[0]["discovery"] == "ok"

    def test_the_engine_is_asked_in_the_working_directory_it_is_given(self, tmp_path):
        """Terragrunt moves the working directory during `init`, so discovery
        must run against the relocated one rather than the original."""
        handler, _ = _api(mint=None)
        discover_fn, calls = _found()
        moved = tmp_path / "relocated"
        moved.mkdir()
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=moved,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert calls[0]["cwd"] == moved
        assert calls[0]["binary"] == "tofu"

    def test_a_bad_discovery_is_reported_not_acted_on(self, tmp_path):
        """The runner does not know whether it matters. A workspace holding no
        identity must not have its run failed by a graph it never needed."""
        handler, sent = _api(mint=None)
        discover_fn, _ = _found(outcome="failed", detail="tofu graph exited 1. boom")
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert env == {}
        assert sent[0]["discovery"] == "failed"
        assert "exited 1" in sent[0]["discovery-detail"]

    def test_an_unparsed_discovery_is_reported_as_such(self, tmp_path):
        handler, sent = _api(mint=None)
        discover_fn, _ = _found(outcome="unparsed", detail="format moved")
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert sent[0]["discovery"] == "unparsed"

    def test_the_target_list_is_capped_on_the_way_out(self, tmp_path):
        """The names come out of the engine's output rather than from us, so the
        request is bounded here as well as at the API."""
        handler, sent = _api(mint=None)
        discover_fn, _ = _found({f"p{i:04d}" for i in range(cloud_identity.MAX_TARGETS + 50)})
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert len(sent[0]["providers"]) == cloud_identity.MAX_TARGETS

    def test_the_detail_is_truncated(self, tmp_path):
        handler, sent = _api(mint=None)
        discover_fn, _ = _found(outcome="failed", detail="x" * 5000)
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert len(sent[0]["discovery-detail"]) == 400


class TestTheWorkspaceMintsNothing:
    def test_a_204_returns_no_env_and_writes_nothing(self, tmp_path):
        handler, _ = _api(mint=None)
        discover_fn, _ = _found({"aws"})
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert env == {}
        assert not (tmp_path / "oidc").exists()

    def test_an_empty_token_list_returns_no_env(self, tmp_path):
        handler, _ = _api(mint=[])
        discover_fn, _ = _found({"aws"})
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert env == {}

    def test_a_404_falls_through_because_the_api_predates_the_feature(self, tmp_path):
        """Agent pools upgrade independently of the control plane, so a runner
        newer than its API is a real posture. The route's absence is
        information, not a fault — and failing here would fail every run in a
        fleet whose API has not caught up."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="Not Found")

        discover_fn, _ = _found({"aws"})
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert env == {}

    def test_no_api_configured_returns_before_the_engine_is_touched(self, tmp_path):
        """A degenerate invocation with no listener context. Nothing to ask and
        nobody to ask, so there is no reason to pay for a graph."""
        discover_fn, calls = _found({"aws"})
        env = cloud_identity.run(
            _cfg(TP_API_URL=""),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            discover_fn=discover_fn,
        )
        assert env == {}
        assert calls == []


class TestOneFilePerTarget:
    def _run(self, tmp_path, *, mint, used=None, phase="plan"):
        handler, sent = _api(mint=mint, phase=phase)
        discover_fn, _ = _found(used if used is not None else mint)
        env = cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        return env, sent

    def test_each_target_gets_its_own_file_under_its_own_name(self, tmp_path):
        env, _ = self._run(tmp_path, mint=["aws", "aws.west", "vault"])
        assert env[cloud_identity.TOKEN_DIR_ENV] == str(tmp_path / "oidc")
        for target in ("aws", "aws.west", "vault"):
            assert (tmp_path / "oidc" / target / "token").read_text() == f"jwt-for-{target}"

    def test_no_combined_token_file_is_written(self, tmp_path):
        """A token audienced for several targets is replayable between them, and
        AWS refuses a multi-valued `aud` outright — so there is deliberately no
        shared file beside the per-target ones."""
        self._run(tmp_path, mint=["aws", "vault"])
        assert not (tmp_path / "oidc" / "token").exists()
        assert sorted(p.name for p in (tmp_path / "oidc").iterdir()) == ["aws", "vault"]

    def test_only_what_the_api_served_is_written(self, tmp_path):
        """The intersection is the API's to compute, not the runner's: it holds
        the mapping. The runner writes exactly what came back."""
        self._run(tmp_path, mint=["aws"], used={"aws", "aws.west", "vault"})
        assert sorted(p.name for p in (tmp_path / "oidc").iterdir()) == ["aws"]

    def test_every_file_is_private(self, tmp_path):
        self._run(tmp_path, mint=["aws", "vault"])
        for target in ("aws", "vault"):
            mode = (tmp_path / "oidc" / target / "token").stat().st_mode
            assert stat.S_IMODE(mode) == 0o600

    def test_the_phase_comes_from_the_api_not_the_runner(self, tmp_path):
        """HCL cannot otherwise see which phase it is in, and switching role by
        phase is the whole point of the apply increment. The API derives it from
        the presented runner token, so its answer is the authority."""
        env, _ = self._run(tmp_path, mint=["aws"], phase="apply")
        assert env[cloud_identity.PHASE_ENV] == "apply"
        assert env[cloud_identity.PHASE_TFVAR_ENV] == "apply"

    def test_the_directory_is_exported_as_a_tfvar_too(self, tmp_path):
        """So a configuration builds `"${var.terrapod_oidc_token_dir}/aws/token"`
        rather than hard-coding the path."""
        env, _ = self._run(tmp_path, mint=["aws"])
        assert env[cloud_identity.TOKEN_DIR_TFVAR_ENV] == str(tmp_path / "oidc")

    def test_no_per_cloud_env_is_set(self, tmp_path):
        """Setting AWS_WEB_IDENTITY_TOKEN_FILE and friends is what would break
        coexistence with the pod's own IRSA: the Job spec carries no cloud
        credential env at all, and absence is what leaves a non-federating
        workspace's pool identity untouched."""
        env, _ = self._run(tmp_path, mint=["aws"])
        forbidden = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "VAULT_")
        assert not [k for k in env if k.startswith(forbidden)]

    def test_no_token_ever_reaches_a_log(self, tmp_path, capsys):
        """The runner streams stdout verbatim into the run log, so a JWT in a
        log line is a credential in a run log a reader with read access can
        fetch. The audiences are logged; the token is not."""
        self._run(tmp_path, mint=["aws", "vault"])
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "jwt-for-aws" not in combined
        assert "jwt-for-vault" not in combined


class TestItMintsAndFails:
    """Every one of these raises. Falling through would hand the run the agent
    pool's identity, which is broader than the one the workspace was moved off,
    and the run would then succeed against real infrastructure."""

    def _expect_raise(self, tmp_path, handler, *, token_dir=None):
        discover_fn, _ = _found({"aws"})
        with pytest.raises(cloud_identity.CloudIdentityUnavailable) as exc:
            cloud_identity.run(
                _cfg(),
                binary="tofu",
                cwd=tmp_path,
                token_dir=token_dir or (tmp_path / "oidc"),
                client=_client(handler),
                discover_fn=discover_fn,
            )
        return str(exc.value)

    def test_a_500_raises_after_retrying(self, tmp_path):
        """Not advisory. Treating it as such would let anyone able to disrupt
        one call downgrade a workspace to the pool's identity without trace,
        and the runner cannot complete a run without the API in any case."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(500, text="boom")

        assert self._expect_raise(tmp_path, handler)
        assert len(calls) == 3, "retried three times"

    def test_a_409_is_final_and_not_retried_and_carries_the_reason(self, tmp_path):
        """The API refuses for two reasons the operator must be able to tell
        apart — the configuration moved under the run, or the graph could not be
        read for a workspace that holds identity — so its own words are what
        reach the run log. Retrying a refusal only delays the failure."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(409, text="has changed since this run was created")

        msg = self._expect_raise(tmp_path, handler)
        assert len(calls) == 1
        assert "409" in msg
        assert "has changed since this run was created" in msg

    def test_a_connection_error_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        assert self._expect_raise(tmp_path, handler)

    def test_an_entry_with_no_token_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"tokens": [{"target": "aws"}], "phase": "plan"})

        assert "no target or no token" in self._expect_raise(tmp_path, handler)

    def test_an_entry_with_no_target_raises(self, tmp_path):
        """There would be no way to tell which identity the file is for, and
        writing it under a guessed name is worse than failing."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"tokens": [{"token": "jwt"}], "phase": "plan"})

        assert "no target or no token" in self._expect_raise(tmp_path, handler)

    def test_an_unwritable_path_raises_naming_the_path(self, tmp_path):
        """The release blocker this very nearly shipped as: the container runs
        with a read-only root filesystem, so the token directory must be under a
        writable mount or every federated run fails here."""
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o500)
        handler, _ = _api(mint=["aws"])
        try:
            msg = self._expect_raise(tmp_path, handler, token_dir=blocked / "oidc")
            assert "aws" in msg
            assert str(blocked) in msg
        finally:
            os.chmod(blocked, 0o700)
