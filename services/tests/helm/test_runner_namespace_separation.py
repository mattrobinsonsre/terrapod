"""The separated runner-namespace topology actually works when an operator picks it.

GHSA-p8xx-7rwg-9f72. Runner Jobs run arbitrary Terraform/Tofu and the chart
places them in `listener.runnerNamespace`, which defaults to the release
namespace — so by default they sit beside the control plane, where the listener's
`secrets: create` grant covers every Secret in the namespace and, with
NetworkPolicies off, runner code can reach Postgres and Redis directly.

**Where the runner Jobs run is the operator's decision, so the defaults do not
move.** What this pins is the other half: that choosing the separated topology
produces something that works. Two things were broken in a way that only shows
up once the isolation is on, which is the worst direction for a security
control to fail in:

* `server_url` in runners.yaml fell back to a bare Service name. The listener
  hands that value to every runner Job as `TP_API_URL`, and a bare name resolves
  only through the pod's own DNS search path — so it resolved from the release
  namespace and nowhere else.
* The API's NetworkPolicy admitted runners with a bare `podSelector`, which in
  NetworkPolicy semantics matches only pods in the policy's own namespace. With
  the runners moved out, that rule denied every run's calls to the API.

Source-level, deliberately: these are Go templates and rendering them needs a
helm binary the unit tier does not have. The rendered proof lives in the
`helm-smoke` CI job, which does have one. What source tells you is whether the
handling is *written*, which is what both regressions removed.
"""

from __future__ import annotations

import re
from pathlib import Path

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"


def _read(name: str) -> str:
    path = _TEMPLATES / name
    assert path.exists(), f"{name} has moved; update this test rather than deleting it"
    return path.read_text()


class TestTheInClusterAPIURLIsFullyQualified:
    """A bare Service name is the one form that cannot cross a namespace."""

    def test_the_helper_yields_a_cluster_local_fqdn(self):
        helpers = _read("_helpers.tpl")
        assert 'define "terrapod.inClusterAPIURL"' in helpers, (
            "terrapod.inClusterAPIURL has gone; the in-cluster API URL is back to "
            "being built inline, which is where the bare-name bug lived"
        )
        body = helpers.split('define "terrapod.inClusterAPIURL"', 1)[1].split("{{- end", 1)[0]
        assert "svc.cluster.local" in body, (
            "the in-cluster API URL is no longer fully qualified. A bare Service "
            "name resolves only from the pod's own namespace, so every runner Job "
            "in a separate runner namespace fails to reach the API."
        )
        assert ".Release.Namespace" in body, (
            "the FQDN does not name the namespace the API Service is actually in, "
            "so it cannot resolve from anywhere"
        )

    def test_the_runner_config_takes_its_fallback_from_the_helper(self):
        """`server_url` is also every runner Job's `TP_API_URL`.

        `deployment-web.yaml` is deliberately NOT covered: the web pod is always
        in the release namespace with the API, so a bare name is correct there
        and qualifying it would be churn. The runner channel is the one that
        crosses a namespace boundary.
        """
        body = _read("configmap-runner.yaml")
        server_url = [ln for ln in body.splitlines() if ln.strip().startswith("server_url:")]
        assert len(server_url) == 1, f"expected one server_url line, found {len(server_url)}"
        line = server_url[0]

        assert "terrapod.inClusterAPIURL" in line, (
            f"server_url no longer falls back to terrapod.inClusterAPIURL: {line.strip()}"
        )
        assert "-api:8000" not in line, (
            "server_url has gone back to a bare Service name. The listener passes "
            "this to every runner Job as TP_API_URL, and runner Jobs run in "
            f"listener.runnerNamespace, where it does not resolve: {line.strip()}"
        )


class TestTheAPIPolicyAdmitsRunnersFromTheirOwnNamespace:
    def test_the_runner_peer_branches_on_whether_the_namespace_is_separate(self):
        body = _read("networkpolicy-api.yaml")
        assert "terrapod.runnerNamespaceIsSeparate" in body, (
            "the API NetworkPolicy no longer distinguishes a separate runner "
            "namespace, so its runner peer is a bare podSelector again — which "
            "matches only pods in the API's own namespace and therefore denies "
            "every run's API call once the runners are moved out."
        )

        separate, _, rest = body.partition(
            '{{- if include "terrapod.runnerNamespaceIsSeparate" . }}'
        )
        assert rest, "the separated branch has moved; update this test"
        true_branch, _, false_branch = rest.partition("{{- else }}")
        false_branch = false_branch.partition("{{- end }}")[0]

        assert "namespaceSelector" in true_branch, (
            "the separated branch admits runners with no namespaceSelector, so it "
            "matches nothing: a bare podSelector is scoped to the policy's own "
            "namespace."
        )
        assert "terrapod.runnerNamespace" in true_branch, (
            "the namespaceSelector does not name the runner namespace, so it is "
            "either wider than intended or matches nothing"
        )
        assert "terrapod-runner" in true_branch, (
            "the separated branch no longer selects runner pods at all"
        )

        # And the co-located arrangement must keep working unchanged: a
        # namespaceSelector there would be redundant, but dropping the peer
        # altogether would break every default install with policies on.
        assert "terrapod-runner" in false_branch, (
            "the co-located branch no longer admits runner pods, which breaks "
            "every default install that turns NetworkPolicies on"
        )

    def test_the_runner_policy_lands_in_the_runner_namespace(self):
        body = _read("networkpolicy-runner.yaml")
        assert "terrapod.runnerNamespace" in body, (
            "the runner NetworkPolicy no longer derives its namespace from the "
            "shared helper, so it can drift from where the Jobs are actually "
            "created and silently constrain nothing"
        )


class TestTheRunnerNamespaceIsCreatedOnlyWhenItIsReallySeparate:
    def test_creation_is_gated_on_both_the_switch_and_the_separation(self):
        body = _read("namespace.yaml")
        assert ".Values.namespace.createRunner" in body, (
            "namespace.createRunner has gone. Everything the separated topology "
            "needs already renders into the runner namespace — the listener's "
            "Role and RoleBinding, the runner ServiceAccount, the runner "
            "NetworkPolicy — so without the Namespace itself `helm install` "
            "fails on the first of them."
        )
        assert "terrapod.runnerNamespaceIsSeparate" in body, (
            "the runner Namespace is rendered without checking that it differs "
            "from the release namespace, so a release with createRunner on and "
            "no runnerNamespace set declares the release namespace twice."
        )

    def test_the_separation_helper_treats_an_equal_namespace_as_not_separate(self):
        helpers = _read("_helpers.tpl")
        assert 'define "terrapod.runnerNamespaceIsSeparate"' in helpers, (
            "the separation helper has gone; the callers that need to tell the "
            "two arrangements apart have nothing to ask"
        )
        body = helpers.split('define "terrapod.runnerNamespaceIsSeparate"', 1)[1].split(
            "{{- end", 1
        )[0]
        assert re.search(
            r"ne\s+\.Values\.listener\.runnerNamespace\s+\.Release\.Namespace", body
        ), (
            "the helper no longer treats `runnerNamespace` set to the release "
            "namespace as the co-located arrangement. Spelling the same namespace "
            "out explicitly would then render a duplicate Namespace object and a "
            "redundant namespaceSelector."
        )
