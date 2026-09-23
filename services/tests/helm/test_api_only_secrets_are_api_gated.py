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
