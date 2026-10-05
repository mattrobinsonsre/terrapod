"""A Secret only the API consumes is rendered only where the API is deployed.

`secret-token-signing.yaml` was gated only on whether an operator had supplied
their own secret, not on whether this release deploys an API. So a listener-only
release (`api.enabled: false`) rendered it too: a randomly generated key that
nothing in that release reads, labelled `component: api`, carrying
`helm.sh/resource-policy: keep` so it outlived the release that made it. Pointed
at a namespace the API release also targets, two releases each declared the same
Secret with different generated values and fought over owning it.

**That template is gone** (#1994) -- Terrapod generates its own signing key and
persists it in the database, so no manifest contains one. The gate it needed is
still enforced below for the two Secrets that remain API-only, and the test that
used to check how the key was generated has been inverted into a check that the
chart never generates one again.

Source-level rather than rendered: these are Go templates and the unit tier has
no helm binary. What matters is whether the gate is *written*, which is exactly
what source tells you.
"""

from __future__ import annotations

import re
from pathlib import Path

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"

#: Templates whose object is consumed by the API alone. Each must refuse to
#: render when `api.enabled` is false, the same condition `deployment-api.yaml`
#: uses for the Deployment that reads them.
_API_ONLY_TEMPLATES = (
    # GHSA-93m3-v3h4-4qvw. The database URL is read by the API Deployment and the
    # migrations, preflight, bootstrap and backup Jobs — every one of which is
    # api-gated — and the bootstrap credentials by the bootstrap Job alone. A
    # listener-only release holds neither and must render neither.
    "secret-database-url.yaml",
    "secret-bootstrap.yaml",
)

#: The gate as `deployment-api.yaml` writes it. Matching on the values path
#: rather than the exact spelling keeps this from failing over whitespace while
#: still requiring the condition to be present.
_API_GATE = re.compile(r"\.Values\.api\.enabled")

#: A Secret in this chart declaring the signing key as its own data. Deliberately
#: NOT a search for `randAlphaNum`: `embedded-postgresql.yaml` legitimately
#: generates a dev password with it, so a gate that wide would fail on an
#: unrelated template. What must never come back is the chart *holding* this key.
_KEY_DECLARATION = re.compile(r"^\s*token_signing_key:", re.MULTILINE)


def test_api_only_secrets_do_not_render_without_an_api():
    assert _API_ONLY_TEMPLATES, "every subject has been removed; this guard now proves nothing"

    offenders = []
    for name in _API_ONLY_TEMPLATES:
        path = _TEMPLATES / name
        assert path.exists(), f"{name} has moved; update this test rather than deleting it"
        body = path.read_text()
        if not _API_GATE.search(body):
            offenders.append(name)

    assert not offenders, (
        "these templates render an API-only object without checking that this "
        f"release deploys an API: {offenders}. A listener-only install then "
        "creates a Secret nothing reads, and two releases in one namespace "
        "fight over owning it. Gate on `.Values.api.enabled`, as "
        "deployment-api.yaml does."
    )


def test_the_api_deployment_still_gates_on_the_same_value():
    """The gate above is only meaningful while the Deployment agrees with it."""
    body = (_TEMPLATES / "deployment-api.yaml").read_text()
    assert _API_GATE.search(body), (
        "deployment-api.yaml no longer gates on `.Values.api.enabled`, so the "
        "secret gate is now pinned to a condition its consumer does not share. "
        "Re-derive both from whatever replaced it."
    )


def test_a_supplied_signing_key_change_rolls_the_api_pods():
    """An operator-supplied key injected as env must roll the Deployment.

    Terrapod's own key is read from the database on every startup, so replicas
    cannot disagree about it. A *supplied* key is still env from a `secretKeyRef`,
    which is snapshotted at pod start and never refreshes, and the key is a
    per-process module global -- not Redis-backed state like sessions or the
    scheduler, so the fleet has no way to notice it disagrees. A Secret that
    changes under a running Deployment therefore leaves older pods on the old key
    while newer ones use the new one, and a runner token minted by one is rejected
    by the other: half of every run's API calls 401 until a human restarts it
    (v1.7.3).

    The `checksum/` pod-template annotation is what makes that rotation atomic.
    """
    body = (_TEMPLATES / "deployment-api.yaml").read_text()
    assert "checksum/token-signing" in body, (
        "deployment-api.yaml has no checksum annotation for the token signing "
        "key, so changing a supplied key leaves running pods on the old one and "
        "half of every run's calls 401. Add a checksum/ annotation over the key, "
        "as checksum/config does for the ConfigMap."
    )


def test_the_chart_never_mints_a_signing_key():
    """The chart must not hold the signing key -- the application owns it.

    This replaces a test that checked *how* the chart generated one. It generated
    it with `randAlphaNum`, which makes a rendered manifest something other than a
    pure function of its inputs; under `helm install` a `lookup` for the existing
    Secret rescued that, but under `helm template` -- which is what Argo CD and
    Flux run -- `lookup` returns nothing and `.Release.IsInstall` is ALWAYS true,
    so every render minted a fresh key and rotated it under the running fleet.

    An application-generated key cannot have that problem, so the fix was to
    remove the generation rather than tighten its guard. This asserts it stays
    removed: the failure mode is someone re-adding a template "so the key exists
    before the API starts", which reintroduces the whole class.
    """
    offenders = [
        p.name for p in sorted(_TEMPLATES.glob("*.yaml")) if _KEY_DECLARATION.search(p.read_text())
    ]
    assert not offenders, (
        f"these templates declare a token signing key: {offenders}. The chart must "
        "not hold this key -- the API generates it on first startup and persists "
        "it in the database (#1994), which is what keeps a random value out of a "
        "rendered manifest. An operator-supplied key is referenced through "
        "`api.tokenSigningKey.existingSecret`; it is never created here."
    )
