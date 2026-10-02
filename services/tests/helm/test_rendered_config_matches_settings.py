"""The rendered ConfigMap and the `Settings` model must agree.

Three gates already exist and none of them covers this. `test_values_contract`
snapshots the keys in **values.yaml**; `test_config_contract` snapshots the keys
`Settings` accepts; `helm-smoke` greps the rendered output for a handful of
specific strings. So the chart can stop rendering a key, or render one the API has
never heard of, and every gate stays green.

Both halves have now happened, in the same release:

- A `| default true` cleanup deleted the render line for
  `vcs.require_connection_authorization` as collateral. The key stayed in
  values.yaml and in the values snapshot, so nothing failed — while an operator's
  explicit `false` silently stopped reaching the pod and reverted to the code
  default. They would then have met a 403 whose own text told them to set the key
  that had stopped working.
- A carried-from-`main` block renders `default_pulumi_version` and three siblings
  that exist in no `Settings` field, no values.yaml and no snapshot. Harmless only
  because `Settings` is `extra="ignore"` — and actively confusing, because
  `values.schema.json` is `additionalProperties: false`, so an operator who tries to
  override one is rejected by the schema while the chart hard-codes a value.

This test is the missing comparison. It renders the chart and asks, of every leaf
in `config.yaml`, whether `Settings` has somewhere to put it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve()


def _root() -> pathlib.Path | None:
    for cand in HERE.parents:
        if (cand / "helm" / "terrapod" / "Chart.yaml").is_file():
            return cand
    return None


ROOT = _root()

#: Keys the chart renders on purpose that `Settings` does not model. Each entry is
#: a claim that the API is *meant* to ignore it, so each needs a reason — otherwise
#: this allowlist becomes the place dead config goes to hide.
RENDERED_BUT_NOT_SETTINGS: dict[str, str] = {}


def _require_helm() -> str:
    """The helm binary, or an honest outcome.

    **This file cannot run in CI and that is by design of the test image**, not an
    oversight to work around here: `docker/Dockerfile.test` ships the chart but no
    helm binary ("Chart only (values + templates); no helm binary is invoked in the
    Python image"), and CI runs `tests/helm` inside it. So in CI these tests skip —
    which is exactly how a gate becomes decorative, and the regression this file was
    written for would have sailed through.

    The real gate is therefore the `helm-smoke` job, which has a helm binary and
    runs both directions of this check (`scripts/ci/check_rendered_config_keys.py`
    plus an explicit-`false` loop). This file is the local-dev convenience: a
    developer who has helm gets the same check for free before pushing.

    `TERRAPOD_REQUIRE_HELM=1` turns the skip into a failure, so an image that later
    gains helm starts enforcing instead of quietly continuing to skip.
    """
    helm = shutil.which("helm")
    if helm is None:
        if os.environ.get("TERRAPOD_REQUIRE_HELM") == "1":
            pytest.fail(
                "TERRAPOD_REQUIRE_HELM=1 but no helm binary is on PATH. The CI gate "
                "for this is the `helm-smoke` job; see this function's docstring."
            )
        pytest.skip(
            "helm is not on PATH — the CI gate for this is the `helm-smoke` job, "
            "which runs scripts/ci/check_rendered_config_keys.py"
        )
    return helm


def _rendered_config() -> dict:
    helm = _require_helm()
    out = subprocess.run(
        [helm, "template", str(ROOT / "helm" / "terrapod")],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        pytest.fail(f"helm template failed: {out.stderr[-2000:]}")
    for doc in yaml.safe_load_all(out.stdout):
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        data = doc.get("data") or {}
        if "config.yaml" in data:
            return yaml.safe_load(data["config.yaml"]) or {}
    pytest.fail("no ConfigMap rendered a config.yaml")


def _leaf_paths(node, prefix: str = "") -> list[str]:
    """Dotted paths of every scalar/list leaf, so a nested block is checked too."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out += _leaf_paths(v, p)
            else:
                out.append(p)
    return out


def _settings_accepts(path: str) -> bool:
    """Whether `Settings` has a field at this dotted path.

    Walks the Pydantic model tree rather than instantiating it, so this says what
    the API *models*, not what a particular environment happens to set. A list- or
    dict-valued field terminates the walk: its contents are the field's own
    business.
    """
    from terrapod.config import Settings

    model = Settings
    parts = path.split(".")
    for i, part in enumerate(parts):
        fields = getattr(model, "model_fields", None)
        if not fields or part not in fields:
            return False
        ann = fields[part].annotation
        if i == len(parts) - 1:
            return True
        # descend into a nested model; unwrap Optional[...] / unions
        nested = None
        for cand in (ann, *getattr(ann, "__args__", ())):
            if hasattr(cand, "model_fields"):
                nested = cand
                break
        if nested is None:
            # a leaf field (dict/list/scalar) with more path left: the remainder is
            # inside the value, which Settings does not model key-by-key.
            return True
        model = nested
    return True


class TestNothingRenderedIsUnknownToTheAPI:
    def test_every_rendered_key_has_a_settings_field(self):
        if ROOT is None:
            pytest.skip("chart is not shipped in this image")
        unknown = [
            p
            for p in _leaf_paths(_rendered_config())
            if p not in RENDERED_BUT_NOT_SETTINGS and not _settings_accepts(p)
        ]
        assert not unknown, (
            "the chart renders config the API does not model, so it is shipped to "
            "every deployment and silently dropped — and because values.schema.json "
            "is additionalProperties:false, an operator cannot even override it:\n  "
            + "\n  ".join(sorted(unknown))
            + "\n\nRemove the render line, or add a Settings field. If it really is "
            "meant to be ignored, add it to RENDERED_BUT_NOT_SETTINGS with a reason."
        )


class TestTheKeysOperatorsSetActuallyReachThePod:
    """The other direction. These are keys whose whole purpose is that an operator
    can change them, and each has been silently un-rendered at least once or is one
    cleanup away from it.
    """

    #: (values path, the value to set, what it means if it goes missing)
    MUST_RENDER = [
        (
            "api.config.vcs.require_connection_authorization",
            False,
            "an operator who opted out of connection authorization has it revert to "
            "on, and every non-admin workspace create naming a connection 403s",
        ),
        (
            "api.config.vcs.require_push_permission_for_commands",
            False,
            "a PR comment can apply infrastructure again",
        ),
        (
            "api.config.database.pool_pre_ping",
            False,
            "the explicit false is discarded and pre-ping stays on",
        ),
        (
            "api.config.notifications.smtp.use_tls",
            False,
            "the explicit false is discarded and SMTP keeps using TLS",
        ),
    ]

    @pytest.mark.parametrize("path,value,why", MUST_RENDER, ids=lambda v: str(v)[:60])
    def test_an_explicit_value_reaches_the_rendered_config(self, path, value, why):
        if ROOT is None:
            pytest.skip("chart is not shipped in this image")
        helm = _require_helm()

        out = subprocess.run(
            [
                helm,
                "template",
                str(ROOT / "helm" / "terrapod"),
                "--set",
                f"{path}={json.dumps(value)}",
            ],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr[-1500:]

        leaf = path.split(".")[-1]
        # The rendered line, not just the key: a `| default`-style swallow renders
        # the key with the WRONG value, which a presence check would pass.
        expected = f"{leaf}: {json.dumps(value)}"
        assert expected in out.stdout, (
            f"`{path}={value}` does not reach the rendered config as {expected!r}. "
            f"If this stays broken: {why}."
        )
