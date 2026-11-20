"""The dev profile must not render the storage-rewrite init container.

#2639 added an init container that patches `.next/routes-manifest.json` before
Next reads it. That is right for a deployment running the production server and
**impossible** for the Tilt dev loop, for two independent reasons:

* Tilt's `docker_build(entrypoint=...)` applies to every container in the pod,
  init containers included, so the init container ran `npx next dev` instead of
  `--emit` and died on `EACCES mkdir /app/.next/dev`;
* even with the right command there is nothing to patch -- `next dev`
  regenerates `.next` at runtime, and `values-local.yaml` masks `/app/.next`
  with an emptyDir anyway.

The symptom was a web pod stuck in `Init:CrashLoopBackOff` while the previous
ReplicaSet kept serving, so `terrapod.local` answered 200 and the stack looked
healthy. Nothing in CI noticed, because every CI stack uses the production
image.

Both halves are asserted, because either one alone restores the breakage: the
template has to be gated, AND the dev profile has to turn it off. Source-level
rather than rendered -- this tier has no helm binary -- with the rendered half
in the `helm-smoke` job.
"""

from __future__ import annotations

import re
from pathlib import Path

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_DEPLOYMENT = _HELM_ROOT / "templates" / "deployment-web.yaml"
_VALUES = _HELM_ROOT / "values.yaml"
_LOCAL = _HELM_ROOT / "values-local.yaml"

#: The values path the gate keys on. One string, used by every assertion here,
#: so a rename cannot leave half of them passing.
_FLAG = "web.storageRewrite.enabled"
_GATE = ".Values.web.storageRewrite.enabled"


def _text(p: Path) -> str:
    assert p.exists(), f"{p} is missing"
    return p.read_text()


def test_the_init_container_is_gated_on_the_flag() -> None:
    src = _text(_DEPLOYMENT)
    assert "name: storage-rewrite" in src, (
        "deployment-web.yaml no longer declares the storage-rewrite init container; "
        "if it was removed on purpose, delete this test with it"
    )
    # The gate must appear BEFORE the container's name, or the container renders
    # unconditionally and the dev profile breaks however the flag is set.
    gate_at = src.find(f"if {_GATE}")
    name_at = src.find("name: storage-rewrite")
    assert gate_at != -1, f"the init container is not gated on {_FLAG}"
    assert gate_at < name_at, (
        f"the {_FLAG} gate appears after the init container's name, so the "
        "container renders regardless -- a handler cannot guard what precedes it"
    )


def test_the_mount_and_the_volume_are_gated_too() -> None:
    # The init container alone is not enough: a subPath mount of a volume that
    # no longer exists fails the pod at creation, which is a different crash
    # with the same effect.
    src = _text(_DEPLOYMENT)
    for needle in ("mountPath: /app/.next/routes-manifest.json", "sizeLimit: 16Mi"):
        at = src.find(needle)
        assert at != -1, f"{needle!r} is gone from deployment-web.yaml"
        before = src[:at]
        assert f"if {_GATE}" in before, (
            f"{needle!r} is not inside a {_FLAG} block, so the dev profile would "
            "still carry it with no init container to populate it"
        )


def test_the_flag_defaults_on_in_values() -> None:
    # Every real deployment wants it: without it the rewrite that gives Pulumi
    # and docker a Content-Length points at a reserved `.invalid` host.
    assert re.search(r"storageRewrite:\s*\n\s*enabled:\s*true", _text(_VALUES)), (
        f"{_FLAG} must default to true in values.yaml"
    )


def test_the_dev_profile_turns_it_off() -> None:
    assert re.search(r"storageRewrite:\s*\n\s*enabled:\s*false", _text(_LOCAL)), (
        "values-local.yaml must disable the storage-rewrite init container -- the "
        "Tilt dev loop runs `next dev`, where it cannot work and the pod will sit "
        "in Init:CrashLoopBackOff"
    )
