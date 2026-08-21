"""The chart and the code must derive the same upstream password env var (#1408).

An authenticated pull-through upstream gets its password from a Secret, named per
upstream. Two places construct that variable's name independently:

- **`deployment-api.yaml`** builds it in Go templating —
  `regexReplaceAll "[^a-zA-Z0-9]" .host "_" | upper`;
- **`OCIUpstreamConfig.password_env_var`** builds it in Python when the code goes
  looking for the credential.

If those ever disagree the chart sets one variable and the code reads another. There
is no error: the fetch simply proceeds anonymously, so a private upstream answers
401 and the operator sees "image not found" for an image that plainly exists, with
a correct-looking Secret mounted and nothing pointing at the cause.

This gate makes the two agree by construction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from terrapod.config import OCIUpstreamConfig


def _deployment_template() -> str:
    """The chart lives at /app/helm in the test image and ../.. from here otherwise."""
    for candidate in (
        Path("/app/helm/terrapod/templates/deployment-api.yaml"),
        Path(__file__).resolve().parents[3] / "helm/terrapod/templates/deployment-api.yaml",
    ):
        if candidate.exists():
            return candidate.read_text()
    pytest.skip("helm chart not available")


def _chart_expression() -> str:
    """Extract the sanitising expression the chart actually uses."""
    template = _deployment_template()
    match = re.search(r"TERRAPOD_OCI_UPSTREAM_\{\{\s*(.+?)\s*\}\}_PASSWORD", template)
    assert match, "the per-upstream password env var is no longer rendered by the chart"
    return match.group(1)


def _chart_sanitise(host: str) -> str:
    """Reproduce the chart's transformation, asserting it is the one we think.

    Pinned literally rather than interpreted: if someone changes the chart's
    expression, this fails loudly here instead of silently diverging at runtime.
    """
    expression = _chart_expression()
    assert expression == 'regexReplaceAll "[^a-zA-Z0-9]" .host "_" | upper', (
        f"the chart's sanitising expression changed to {expression!r}; update "
        "OCIUpstreamConfig.password_env_var to match, then update this test"
    )
    return re.sub(r"[^a-zA-Z0-9]", "_", host).upper()


@pytest.mark.parametrize(
    "host",
    [
        "quay.io",
        "ghcr.io",
        "registry.example.com",
        "registry.example.com:5000",  # a port is the case most likely to differ
        "internal-registry",
        "10.0.0.1:5000",
        "a.b.c.d.e.f",
    ],
)
def test_chart_and_code_agree_on_the_env_var_name(host: str) -> None:
    expected = f"TERRAPOD_OCI_UPSTREAM_{_chart_sanitise(host)}_PASSWORD"
    assert OCIUpstreamConfig(host=host).password_env_var == expected


def test_the_chart_still_renders_the_variable_at_all() -> None:
    """Guards the whole mechanism, not just its spelling: removing the env block
    would leave authenticated upstreams silently anonymous."""
    template = _deployment_template()
    assert "TERRAPOD_OCI_UPSTREAM_" in template
    assert "secretKeyRef" in template


def test_the_password_is_never_rendered_into_the_configmap() -> None:
    """A credential in a ConfigMap is readable by anything that can read the
    ConfigMap, which is a far wider set than can read the Secret."""
    for candidate in (
        Path("/app/helm/terrapod/templates/configmap-api.yaml"),
        Path(__file__).resolve().parents[3] / "helm/terrapod/templates/configmap-api.yaml",
    ):
        if candidate.exists():
            configmap = candidate.read_text()
            break
    else:
        pytest.skip("helm chart not available")

    oci_block = re.search(r"^ *oci:.*?(?=^ *provider_cache:)", configmap, re.S | re.M)
    assert oci_block, "the oci block is no longer rendered into the ConfigMap"
    assert "password" not in oci_block.group(0).lower()
    assert "existingSecret" not in oci_block.group(0)
