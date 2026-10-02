"""No credential an operator supplies in values is rendered into a manifest.

GHSA-93m3-v3h4-4qvw. Four credentials were rendered as literal env values when
supplied in `values.yaml`: the database URL (the API Deployment plus the
migrations, preflight, bootstrap and backup Jobs), the listener's join token, and
the bootstrap admin password and pool token.

A Deployment or Job spec is not a credential store. Anyone with `get` on them in
the namespace reads it, `helm get values` reads it, and so does whatever holds
the rendered manifest — a GitOps repository, a CI log, a Terraform state file.
The join token is the sharpest of the four: a listener that joins the pool claims
runs and receives every variable those runs resolve, sensitive ones included.

All four now reach their consumer by `secretKeyRef`, from a chart-managed Secret.
Supplying your own Secret (`postgresql.existingSecret`,
`listener.existingSecret`, `bootstrap.existingSecret`,
`bootstrap.poolTokenExistingSecret`) is still better and still recommended,
because a value given in `values.yaml` passes through the release's own stored
values whatever the chart then does with it — but that is not a reason to put it
in the manifest as well.

Source-level, deliberately: these are Go templates and rendering them needs a
helm binary the unit tier does not have. The rendered proof is in the
`helm-smoke` CI job, which asserts that a credential passed with `--set` appears
in no object other than a Secret.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"
_SCHEMA = _HELM_ROOT / "values.schema.json"

#: The values paths that hold a credential. Each must never be interpolated into
#: a `value:` line — only into a Secret's `stringData`.
#:
#: This is a ledger, so the thing that can quietly empty it is a rename: a path
#: that no longer exists is a check that passes for the wrong reason.
#: `test_every_listed_path_still_exists` closes that by requiring each one to be
#: declared in values.schema.json, which is the chart's own declaration surface.
_CREDENTIAL_PATHS = (
    "postgresql.url",
    "listener.joinToken",
    "bootstrap.adminPassword",
    "bootstrap.poolToken",
)

#: A `value:` line interpolating a values path, e.g.
#:   - name: FOO
#:     value: {{ .Values.postgresql.url | quote }}
_LITERAL_VALUE = re.compile(r"^\s*value:\s*\{\{[^}]*\.Values\.([A-Za-z0-9_.]+)")


def _templates() -> list[Path]:
    return sorted(p for p in _TEMPLATES.glob("*.yaml"))


def _schema_has(path: str) -> bool:
    node = json.loads(_SCHEMA.read_text())
    for part in path.split("."):
        props = node.get("properties")
        if not isinstance(props, dict) or part not in props:
            return False
        node = props[part]
    return True


class TestNoCredentialIsInterpolatedIntoAValue:
    def test_the_sweep_finds_literal_value_lines_at_all(self):
        """The regex has to match something, or the test below proves nothing.

        Plenty of `value:` lines are legitimate — image tags, ports, feature
        flags, pool names. If this finds none, the pattern has stopped matching
        the chart's shape and every assertion after it is vacuous.
        """
        found = [
            m.group(1)
            for p in _templates()
            for m in (_LITERAL_VALUE.search(line) for line in p.read_text().splitlines())
            if m
        ]
        assert len(found) > 10, (
            f"the literal-value sweep matched only {len(found)} lines, which is "
            "too few to be reading the chart correctly — fix the pattern"
        )

    def test_no_template_renders_a_credential_as_a_literal(self):
        offenders: list[str] = []
        for path in _templates():
            for n, line in enumerate(path.read_text().splitlines(), 1):
                m = _LITERAL_VALUE.search(line)
                if m and m.group(1) in _CREDENTIAL_PATHS:
                    offenders.append(f"{path.name}:{n} -> .Values.{m.group(1)}")

        assert not offenders, (
            "these templates render an operator-supplied credential into a "
            f"manifest as a literal: {offenders}. Anyone with read on Deployments "
            "or Jobs — and anything that stores the rendered manifest — can read "
            "it. Put it in a chart-managed Secret and reference it with "
            "secretKeyRef, as secret-database-url.yaml and secret-bootstrap.yaml do."
        )

    def test_every_listed_path_still_exists(self):
        """A renamed path would silently empty the ledger above."""
        missing = [p for p in _CREDENTIAL_PATHS if not _schema_has(p)]
        assert not missing, (
            f"these credential paths are no longer declared in values.schema.json: "
            f"{missing}. They were renamed or removed, so the check above now "
            "guards nothing. Update _CREDENTIAL_PATHS to the new spelling."
        )


class TestEachCredentialReachesItsConsumerFromASecret:
    def test_the_listener_takes_its_join_token_by_secret_key_ref(self):
        body = (_TEMPLATES / "deployment-listener.yaml").read_text()
        block = body.split("TERRAPOD_JOIN_TOKEN", 1)
        assert len(block) == 2, "the listener no longer sets TERRAPOD_JOIN_TOKEN at all"
        # The env entry immediately following the name must be a secretKeyRef.
        following = block[1][:400]
        assert "secretKeyRef" in following, (
            "the listener's join token is not delivered by secretKeyRef. It is "
            "the credential that lets a listener claim runs and read their "
            "variables, and it was previously a literal in this Deployment."
        )

    def test_the_bootstrap_job_takes_both_credentials_by_secret_key_ref(self):
        body = (_TEMPLATES / "job-bootstrap.yaml").read_text()
        for var in ("TERRAPOD_BOOTSTRAP_ADMIN_PASSWORD", "TERRAPOD_BOOTSTRAP_POOL_TOKEN"):
            # Every occurrence, because the password has two branches (an
            # operator's existingSecret and the chart-managed Secret) and only
            # checking the first would miss a literal in the second.
            for chunk in body.split(f"- name: {var}\n")[1:]:
                head = chunk[:300]
                assert "secretKeyRef" in head, (
                    f"{var} is set from something other than a Secret in "
                    f"job-bootstrap.yaml: {head.splitlines()[:3]}"
                )

    def test_a_join_token_change_rolls_the_listener_pods(self):
        """Env from a secretKeyRef is snapshotted at pod start and never refreshes.

        Rotating the join token under a running Deployment would otherwise leave
        every pod holding the old one — and the join path is where that bites,
        because it is only taken when the credentials Secret is absent, which is
        long after the pod started. Same reasoning as `checksum/token-signing`
        on the API Deployment.
        """
        body = (_TEMPLATES / "deployment-listener.yaml").read_text()
        assert "checksum/join-token" in body, (
            "deployment-listener.yaml has no checksum annotation over the "
            "join-token Secret, so changing the token leaves running pods on the "
            "old one with nothing to notice it. Add one, as checksum/runner-config "
            "does for the ConfigMap."
        )


class TestTheChartManagedSecretsAreGatedLikeTheirConsumers:
    def test_the_database_url_secret_renders_only_where_the_api_does(self):
        body = (_TEMPLATES / "secret-database-url.yaml").read_text()
        assert ".Values.api.enabled" in body, (
            "secret-database-url.yaml renders a database credential without "
            "checking that this release deploys an API. A listener-only install "
            "would then hold a Secret nothing in it reads, and two releases in "
            "one namespace would fight over owning it."
        )
        # And it must stand down for an operator who supplies their own, or for
        # the embedded datastore, both of which already have a Secret.
        assert ".Values.postgresql.existingSecret" in body
        assert ".Values.postgresql.deploy" in body

    def test_the_join_token_secret_renders_only_where_the_listener_does(self):
        body = (_TEMPLATES / "secret-listener-join-token.yaml").read_text()
        assert ".Values.listener.enabled" in body, (
            "secret-listener-join-token.yaml renders a join token without "
            "checking that this release deploys a listener"
        )
        assert ".Values.listener.existingSecret" in body, (
            "it does not stand down for an operator's own existingSecret, so two "
            "Secrets would claim to hold the token"
        )

    def test_the_credential_secrets_precede_every_hook_that_reads_them(self):
        """The earliest consumer is a pre-install hook, so a plain Secret is too late.

        Hook resources are created before any ordinary resource, so the
        migrations Job (`pre-install,pre-upgrade`, weight -5) would start before
        a non-hook Secret existed. These two must therefore be hooks themselves,
        at a weight below every consumer's — and below the embedded datastores
        at -20, so ordering holds however the two are combined.
        """
        weights: dict[str, int] = {}
        for name in ("secret-database-url.yaml", "secret-bootstrap.yaml"):
            body = (_TEMPLATES / name).read_text()
            assert '"helm.sh/hook": pre-install,pre-upgrade' in body, (
                f"{name} is not a pre-install,pre-upgrade hook, so it does not "
                "exist when the migrations Job hook runs — and pre-install alone "
                "would never pick up an edited value on upgrade"
            )
            m = re.search(r'"helm\.sh/hook-weight":\s*"(-?\d+)"', body)
            assert m, f"{name} has no hook weight, so its ordering is undefined"
            weights[name] = int(m.group(1))

        consumers = {
            "job-migrations.yaml": -5,
            "embedded-postgresql.yaml": -20,
        }
        for name, weight in weights.items():
            for consumer, consumer_weight in consumers.items():
                assert weight < consumer_weight, (
                    f"{name} is at weight {weight}, which is not before "
                    f"{consumer} at {consumer_weight} — the consumer would run "
                    "with no Secret to read"
                )


#: Values keys whose `0` is a documented opt-out, so `| default N` on them would
#: discard it. Go templates treat 0 as the empty value exactly as they treat
#: `false`, which is the trap `test_no_boolean_default_swallow.py` guards for
#: booleans — this is the same trap one type over.
#:
#: Scoped to a named list rather than banning `| default <number>` outright: the
#: chart has ~87 numeric defaults and for most of them 0 is not a meaningful
#: value (a `health_port: 0` or a `heartbeat_interval: 0` is not an opt-out, it is
#: a mistake), so a blanket ban would be almost entirely false positives. The
#: general gap is real and left open deliberately; add a key here when its 0
#: starts meaning something.
_ZERO_MEANS_SOMETHING = {
    "bootstrap.poolTokenMaxUses": "0 = unlimited uses",
    "bootstrap.poolTokenTTLSeconds": "0 = never expires",
}


class TestAZeroThatMeansSomethingIsNotSwallowed:
    def test_no_template_defaults_over_a_meaningful_zero(self):
        offenders: list[str] = []
        for path in _templates():
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for key, meaning in _ZERO_MEANS_SOMETHING.items():
                    leaf = key.rsplit(".", 1)[-1]
                    if f".{leaf}" in line and re.search(r"\|\s*default\s+\d", line):
                        offenders.append(f"{path.name}:{n} {key} ({meaning})")

        assert not offenders, (
            "`| default N` discards an operator's explicit 0, because Go "
            f'templates treat 0 as empty: {offenders}. Use `dig "key" N '
            ".Values.parent`, which substitutes only when the key is genuinely "
            "absent. For the bootstrap token limits the consequence is not "
            "cosmetic — an operator's deliberate 'no limit' becomes the "
            "tightest possible limit on a token a listener needs."
        )

    def test_each_listed_key_is_still_declared(self):
        """A rename would empty the list above without failing anything."""
        missing = [k for k in _ZERO_MEANS_SOMETHING if not _schema_has(k)]
        assert not missing, (
            f"these keys are no longer in values.schema.json: {missing}. Update "
            "_ZERO_MEANS_SOMETHING to their new spelling."
        )
