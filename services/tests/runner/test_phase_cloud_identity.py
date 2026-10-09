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

    def test_the_directory_is_never_exported_as_a_terraform_variable(self, tmp_path):
        """Inverted from a test that asserted the opposite, and kept rather than
        deleted because it is what protects the decision.

        Exporting `TF_VAR_terrapod_oidc_token_dir` reserved a name inside the
        operator's own configuration in order to say something the documented
        path already says. A provider block names
        `/var/run/terrapod/oidc/<target>/token` directly. The plain env var
        survives for a shell hook, which cannot read a Terraform variable at
        all; what must not come back is a second, Terraform-shaped way to spell
        the same path.
        """
        env, _ = self._run(tmp_path, mint=["aws"])
        assert env[cloud_identity.TOKEN_DIR_ENV] == str(tmp_path / "oidc")
        assert not [k for k in env if k.startswith("TF_VAR_") and "token_dir" in k]

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
        writable mount or every federated run fails here.

        Made unwritable by giving the directory a REGULAR FILE as its parent,
        not by a restrictive mode. A mode is a permission check, and CI runs
        pytest as root, which holds `CAP_DAC_OVERRIDE` and walks straight
        through one -- so the first version of this test passed locally as an
        ordinary user and, in CI, cheerfully wrote the token into the directory
        it had just declared unwritable. `ENOTDIR` is a type error rather than a
        permission check, so no privilege bypasses it and the test means the
        same thing for every uid that runs it.
        """
        parent = tmp_path / "a-file-not-a-directory"
        parent.write_text("")
        handler, _ = _api(mint=["aws"])
        msg = self._expect_raise(tmp_path, handler, token_dir=parent / "oidc")
        assert "aws" in msg
        assert str(parent) in msg


class TestAnEngineThatCannotDiscover:
    """`discover_providers=False` — Pulumi (#2006).

    A Pulumi program is arbitrary code whose provider instances are built at
    runtime, so there is nothing to enumerate before `preview` and `preview` is
    what needs the credentials. The runner therefore runs nothing, sends an
    empty list, and the API — which reads the engine off the workspace row —
    mints the workspace's whole resolved mapping.
    """

    def test_the_engine_is_never_invoked(self, tmp_path):
        """The property that matters, and the one a `providers == []` assertion
        would not catch: discovery must not run at all.

        `discover_fn` is deliberately passed AND asserted unused, so this fails
        if the switch is ever reduced to "discover, then discard the answer" —
        which would send the same empty list while invoking an engine that, for
        Pulumi, has no `graph` subcommand to invoke.
        """
        handler, sent = _api(mint=["aws"])
        discover_fn, calls = _found({"aws"})
        cloud_identity.run(
            _cfg(),
            binary="pulumi",
            cwd=tmp_path,
            discover_providers=False,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert calls == [], "the engine was asked to graph a program it cannot graph"
        assert len(sent) == 1, "the API is still asked exactly once"

    def test_it_sends_an_empty_list_and_a_clean_outcome(self, tmp_path):
        """`ok`, not `failed` or `unparsed`.

        Nothing went wrong — there was no graph to read, and those two outcomes
        exist to describe one that could not be. It also matters across skew: an
        API that does read the outcome would refuse the run for an absence that
        is entirely normal for this engine.
        """
        handler, sent = _api(mint=["aws"])
        cloud_identity.run(
            _cfg(),
            binary="pulumi",
            cwd=tmp_path,
            discover_providers=False,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
        )
        assert sent[0]["providers"] == []
        assert sent[0]["discovery"] == "ok"
        assert "does not discover" in sent[0]["discovery-detail"]

    def test_the_tokens_the_api_mints_are_still_delivered(self, tmp_path):
        """The whole point: the runner asked for nothing and is given several.

        Delivery is unchanged — one file per target, 0600, at the documented
        path — because nothing about writing a token depends on how its target
        was chosen.
        """
        handler, _ = _api(mint=["aws", "gcp", "vault.eu"])
        env = cloud_identity.run(
            _cfg(),
            binary="pulumi",
            cwd=tmp_path,
            discover_providers=False,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
        )
        for target in ("aws", "gcp", "vault.eu"):
            path = tmp_path / "oidc" / target / "token"
            assert path.read_text() == f"jwt-for-{target}"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert env[cloud_identity.TOKEN_DIR_ENV] == str(tmp_path / "oidc")

    def test_a_workspace_that_mints_nothing_still_falls_through(self, tmp_path):
        """204 is not an error here either — most workspaces hold no identity,
        and such a run keeps the agent pool's own, exactly as before #1901."""
        handler, sent = _api(mint=None)
        env = cloud_identity.run(
            _cfg(),
            binary="pulumi",
            cwd=tmp_path,
            discover_providers=False,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
        )
        assert env == {}
        assert len(sent) == 1

    def test_discovery_still_runs_by_default(self, tmp_path):
        """The default is unchanged, so the Terraform path cannot be altered by
        a caller that forgets the argument."""
        handler, _ = _api(mint=None)
        discover_fn, calls = _found({"aws"})
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert len(calls) == 1


class TestATargetFromTheAPIIsRefusedBeforeItBecomesAPath:
    """The runner's own traversal guard, which nothing was exercising.

    It is deliberate duplication: the API refuses these targets too, at the
    workspace write AND on the mint, but this is a separately versioned image
    that may be older or newer than the API it is talking to, and a token is a
    bearer credential for one of the workspace's cloud identities. So the check
    belongs at the write as well as at the request -- and the whole point of a
    defence-in-depth check is that it holds when the other layer does not, which
    is exactly the condition no test was creating.

    Deleting the six lines in `phases/cloud_identity.py` passed every one of the
    555 lines this file had before, because every fixture here sends well-formed
    targets. The API-side twin is well covered, which is what made this look
    covered.

    `target` is echoed back by the API and joined as
    `<token dir>/<target>/token`, so the consequence of each case below is a
    token written somewhere the operator's provider block is not reading from --
    or, worse, somewhere another provider block IS.
    """

    def _refuse(self, tmp_path, target, *, token="jwt-value"):
        """Drive one target through the real phase and return the message."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tokens": [{"target": target, "token": token, "audiences": ["aud"]}],
                    "phase": "plan",
                    "expires_in": 900,
                },
            )

        discover_fn, _ = _found({"aws"})
        with pytest.raises(cloud_identity.CloudIdentityUnavailable) as exc:
            cloud_identity.run(
                _cfg(),
                binary="tofu",
                cwd=tmp_path,
                token_dir=tmp_path / "run" / "oidc",
                client=_client(handler),
                discover_fn=discover_fn,
            )
        return str(exc.value)

    def _nothing_written(self, tmp_path, token="jwt-value"):
        """No file anywhere under the temp tree holds the token.

        Asserted over the WHOLE tree rather than under the token directory,
        because escaping that directory is the thing being prevented -- a check
        scoped to it would pass precisely when the guard failed.
        """
        leaked = [
            p for p in tmp_path.rglob("*") if p.is_file() and token in p.read_text(errors="ignore")
        ]
        assert not leaked, f"the token was written to {leaked}"

    @pytest.mark.parametrize(
        "target",
        [
            "aws/../vault",  # lands at the path the operator's `vault` block reads
            "../token",
            "/etc/terrapod",  # absolute: `Path / "/x"` discards the directory entirely
            "aws\\..\\vault",
            "aws\x00/vault",
        ],
    )
    def test_a_target_that_would_not_resolve_inside_the_token_directory_is_refused(
        self, tmp_path, target
    ):
        msg = self._refuse(tmp_path, target)
        assert "not a provider configuration name" in msg
        assert "has not been written" in msg
        self._nothing_written(tmp_path)

    @pytest.mark.parametrize("target", ["..", "."])
    def test_a_bare_parent_or_current_reference_is_refused(self, tmp_path, target):
        """The two the clause as first written could not catch, because
        `target.split(".")` splits ON the dot and so can never yield an element
        equal to `".."` -- `"..".split(".")` is `["", "", ""]`.

        `..` wrote `<token dir>/../token`, one level above the directory. `.`
        wrote `<token dir>/token`, which is the combined-token path
        `test_no_combined_token_file_is_written` asserts must never exist -- so a
        single malformed target could manufacture the very file whose absence is
        the reason tokens are minted per target at all.

        The API refuses both (`unsafe_target_reason` tests `name in (".", "..")`
        explicitly), so this is reachable only across image skew -- and skew is
        the only reason this guard exists.
        """
        msg = self._refuse(tmp_path, target)
        assert "not a provider configuration name" in msg
        self._nothing_written(tmp_path)
        assert not (tmp_path / "run" / "token").exists()

    def test_the_refusal_names_the_offending_target(self, tmp_path):
        """An operator reading a failed run log needs to know which entry was
        rejected; there may be several, and the token must not be in the line."""
        msg = self._refuse(tmp_path, "aws/../vault", token="jwt-secret")
        assert "aws/../vault" in msg
        assert "jwt-secret" not in msg

    def test_a_legitimate_aliased_target_is_still_written(self, tmp_path):
        """The guard must not refuse the form it exists to protect: one dot
        separates provider from alias, so `vault.eu` is ordinary and common.
        Without this the parametrisations above would pass against a guard that
        refused everything."""
        handler, _ = _api(mint=["vault.eu"])
        discover_fn, _ = _found({"vault.eu"})
        cloud_identity.run(
            _cfg(),
            binary="tofu",
            cwd=tmp_path,
            token_dir=tmp_path / "run" / "oidc",
            client=_client(handler),
            discover_fn=discover_fn,
        )
        assert (tmp_path / "run" / "oidc" / "vault.eu" / "token").read_text() == "jwt-for-vault.eu"

    def test_the_split_form_could_never_have_fired(self):
        """The Python fact the dead clause turned on, asserted rather than only
        described, so the guard is not 'simplified' back to the form that read
        as equivalent. Splitting ON the dot means no element can contain one."""
        assert ".." not in "..".split(".")
        assert "." not in ".".split(".")
        assert ".." not in "a..b".split(".")
