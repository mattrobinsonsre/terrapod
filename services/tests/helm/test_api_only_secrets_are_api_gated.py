"""A Secret only the API consumes is rendered only where the API is deployed.

The token signing key signs and verifies four stateless token families entirely
inside the API. A listener asks the API for runner tokens over HTTP and never
signs anything -- the listener image does not even ship `terrapod.auth`.

`secret-token-signing.yaml` was gated only on whether an operator had supplied
their own secret, not on whether this release deploys an API. So a listener-only
release (`api.enabled: false`) rendered it too: a randomly generated key that
nothing in that release reads, labelled `component: api`, carrying
`helm.sh/resource-policy: keep` so it outlived the release that made it. Pointed
at a namespace the API release also targets, two releases each declared the same
Secret with different generated values and fought over owning it.

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
_API_ONLY_TEMPLATES = ("secret-token-signing.yaml",)

#: The gate as `deployment-api.yaml` writes it. Matching on the values path
#: rather than the exact spelling keeps this from failing over whitespace while
#: still requiring the condition to be present.
_API_GATE = re.compile(r"\.Values\.api\.enabled")


def test_api_only_secrets_do_not_render_without_an_api():
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


def test_a_signing_key_change_rolls_the_api_pods():
    """A Secret injected as env must roll the Deployment when it changes.

    Env from a `secretKeyRef` is snapshotted at pod start and never refreshes,
    and the signing key is a per-process module global -- not Redis-backed
    state like sessions or the scheduler, so the fleet has no way to notice it
    disagrees. A Secret that changes under a running Deployment therefore
    leaves older pods on the old key while newer ones use the new one, and a
    runner token minted by one is rejected by the other: half of every run's
    API calls 401 until a human restarts it.

    The `checksum/` pod-template annotation is what makes a rotation atomic.
    `configmap-api.yaml` already had one; the signing key did not.
    """
    body = (_TEMPLATES / "deployment-api.yaml").read_text()
    assert "checksum/token-signing" in body, (
        "deployment-api.yaml has no checksum annotation for the token signing "
        "key, so changing the key leaves running pods on the old one and half "
        "of every run's calls 401. Add a checksum/ annotation over the key, as "
        "checksum/config does for the ConfigMap."
    )


def test_a_key_is_never_minted_without_the_means_to_check_for_one_first():
    """Generation must be conditional on `lookup` actually working.

    A rendered manifest has to be a pure function of its inputs and
    `randAlphaNum` is not. Under `helm install` the `lookup` rescues that by
    finding the existing key and reusing it. Under a GitOps renderer --
    `helm template`, which is what Argo CD and Flux run -- `lookup` returns
    nothing and `.Release.IsInstall` is ALWAYS true, so an unguarded branch
    mints a fresh key on every render against a running deployment.
    """
    body = (_TEMPLATES / "secret-token-signing.yaml").read_text()
    gen = [ln for ln in body.splitlines() if "randAlphaNum" in ln]
    assert gen, "the generation branch has moved; update this test rather than deleting it"

    assert re.search(r"IsInstall.*lookup|lookup.*IsInstall", body), (
        "the key is generated without first proving `lookup` works. Under "
        "`helm template` lookup returns nothing and IsInstall is always true, "
        "so every render mints a new key and rotates it under the running "
        "fleet. Gate generation on a lookup that must succeed."
    )
