"""The OIDC issuer signing key is never minted by the chart.

It is a *published trust root*: every cloud that federates to this deployment
holds its public half, fetched from the JWKS. So re-minting it does not merely
invalidate tokens in flight -- it breaks every federated workspace at once,
across every cloud, until each one re-fetches.

That is exactly what a generating template does under `helm template`, which is
what Argo CD and Flux run: `lookup` returns nothing and `.Release.IsInstall` is
always true, so an unguarded branch mints a fresh key on *every render*. The
chart therefore does not generate this key at all. The API generates it on first
startup and persists it, serialised across replicas by a transaction-scoped
advisory lock (`auth/oidc_signing.py`, following `auth/ca.py`), so no manifest
ever contains a random value for a renderer to re-mint or a pruner to delete.

An operator-supplied key stays authoritative: it arrives as env from a
`secretKeyRef` and wins on every startup, and is never copied into the database
-- or the first value supplied would win for ever and every later rotation would
be silently ignored.

Source-level, for two reasons: these are Go templates, and the Python test image
ships the chart with no helm binary (so the rendered checks in this directory
skip in CI). What matters here is whether a generating call is *written*, which
is precisely what source tells you. The rendered half -- `oidc_issuer` reaching
config.yaml, `signing_key_pem` reaching no ConfigMap -- is asserted by the
`helm-smoke` job, which has helm.
"""

from __future__ import annotations

import re
from pathlib import Path

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"

#: Helm's generators. `genPrivateKey` and `genCA`/`genSelfSignedCert` produce key
#: material directly; `randAlphaNum`/`randAscii`/`randNumeric`/`uuidv4` are the
#: shapes a passphrase or seed would arrive as.
_GENERATORS = re.compile(
    r"\b(genPrivateKey|genCA|genSelfSignedCert|genSignedCert|"
    r"randAlphaNum|randAscii|randAlpha|randNumeric|uuidv4|derivePassword)\b"
)

#: A line is about the OIDC signing key if it names it. Deliberately narrow:
#: `embedded-postgresql.yaml` legitimately generates a dev password, so a
#: "no generators anywhere" gate would fail on something unrelated and get
#: weakened or deleted rather than fixed.
_OIDC = re.compile(r"oidc[-_]?signing|signing[-_]?key_pem|oidcSigningKey", re.IGNORECASE)

#: A Helm comment, `{{- /* ... */}}`, possibly spanning lines.
_HELM_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)

#: The key being *emitted* into config.yaml, i.e. at the start of a line
#: (ignoring indentation) and followed by a colon.
_RENDERS_SIGNING_KEY = re.compile(r"^\s*signing_key_pem\s*:", re.MULTILINE)


def _template_files() -> list[Path]:
    files = sorted(p for p in _TEMPLATES.glob("*.yaml"))
    assert files, f"no templates found under {_TEMPLATES}; has the chart moved?"
    return files


def test_no_template_generates_the_oidc_signing_key():
    offenders: list[str] = []
    for path in _template_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _GENERATORS.search(line) and _OIDC.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "the chart generates the OIDC issuer signing key:\n  "
        + "\n  ".join(offenders)
        + "\n\nUnder `helm template` (Argo CD, Flux) `lookup` returns nothing and "
        "IsInstall is always true, so this re-mints the key on every render and "
        "breaks every federated workspace in every cloud at once. The API "
        "generates and persists it instead -- see auth/oidc_signing.py."
    )


def test_no_template_declares_a_secret_holding_the_oidc_signing_key():
    """Not even a non-generating one.

    A Secret the chart *declares* is a Secret the chart owns: it carries the
    release's labels, a pruning controller deletes it when it leaves the
    manifest, and two releases targeting one namespace fight over it. The
    operator's own Secret is referenced by name (`existingSecret`) and created
    outside the release, which is what keeps it theirs.
    """
    offenders: list[str] = []
    for path in _template_files():
        body = path.read_text()
        if "kind: Secret" not in body:
            continue
        # The reference in deployment-api.yaml is a secretKeyRef, not a Secret.
        if "kind: Secret" in body and _OIDC.search(body):
            offenders.append(path.name)

    assert not offenders, (
        f"these templates declare a Secret for the OIDC signing key: {offenders}. "
        "An operator-supplied key is referenced by name via "
        "`api.oidcSigningKey.existingSecret` and created outside the release, so "
        "the release never owns the trust root."
    )


def test_the_signing_key_is_never_rendered_into_the_config_map():
    """The private key is a secret, so it goes nowhere near a ConfigMap.

    The config-channel contract: non-sensitive settings flow through the
    ConfigMap, credentials arrive as env from a `secretKeyRef`. A ConfigMap is
    world-readable to anything that can read ConfigMaps in the namespace, which
    is a far wider set than the Secret readers.
    """
    configmap = _TEMPLATES / "configmap-api.yaml"
    assert configmap.exists(), "configmap-api.yaml has moved; update this test"
    body = configmap.read_text()

    # Strip Helm comments first, and match the emitted YAML *key* rather than
    # the bare name. The template deliberately carries a comment saying the key
    # is absent and why -- a bare-name grep matches that comment and fails on
    # the documentation of the correct behaviour.
    emitted = _HELM_COMMENT.sub("", body)
    assert not _RENDERS_SIGNING_KEY.search(emitted), (
        "configmap-api.yaml renders signing_key_pem as a config key. The private "
        "key is a credential: it reaches the pod as env from a secretKeyRef in "
        "deployment-api.yaml, never through the ConfigMap."
    )
    # The non-sensitive half of the block must still be there, or this test
    # passes for the wrong reason -- a block that renders nothing at all.
    assert "oidc_issuer:" in body, (
        "configmap-api.yaml no longer renders the oidc_issuer block at all. The "
        "non-sensitive settings (enabled, public_url, the TTLs) must reach the "
        "pod through config.yaml, or an operator sets them and they are inert."
    )


def test_the_supplied_key_is_gated_on_the_operator_having_supplied_one():
    """The env var renders only when `existingSecret` names a Secret.

    Unconditionally, every deployment would reference a Secret that does not
    exist and the API pod would never start.
    """
    deployment = _TEMPLATES / "deployment-api.yaml"
    assert deployment.exists(), "deployment-api.yaml has moved; update this test"
    lines = deployment.read_text().splitlines()

    idx = [i for i, line in enumerate(lines) if "OIDC_ISSUER__SIGNING_KEY_PEM" in line]
    assert idx, (
        "deployment-api.yaml no longer passes the OIDC signing key. Without it "
        "`api.oidcSigningKey.existingSecret` is a value nothing reads, so BYO is "
        "silently unsupported."
    )

    # Walk back to the nearest enclosing `{{- if ... }}`; it must test the
    # supplied-secret value. Looking at the gate rather than the exact spelling
    # keeps this from failing over whitespace.
    for i in idx:
        gate = None
        for line in reversed(lines[:i]):
            if "{{- if" in line or "{{ if" in line:
                gate = line
                break
        assert gate is not None and "oidcSigningKey" in gate, (
            f"line {i + 1} passes the OIDC signing key but its nearest enclosing "
            f"`if` is {gate!r}. It must be gated on "
            "`.Values.api.oidcSigningKey.existingSecret`, or every deployment "
            "references a Secret that may not exist and the API never starts."
        )
