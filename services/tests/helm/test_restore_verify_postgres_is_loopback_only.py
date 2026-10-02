"""The throwaway Postgres in the restore-verify drill listens on loopback only.

GHSA-mrqj-66jm-rjjr. `POSTGRES_HOST_AUTH_METHOD=trust` is what lets the restore
run with no credential to manage, and it is acceptable only because nothing off
the pod can reach it. That was never enforced: the template's own comment said
"loopback only" while `listen_addresses` was set nowhere, so Postgres applied its
default of `*` and a trust-auth SUPERUSER listened on the pod IP for the life of
the job. `networkPolicies.enabled` is false by default and no policy selects this
pod, so nothing else stopped it either.

A template comment is not a control. This asserts the flag that makes the comment
true, and asserts the trust auth it is the counterweight to — so removing the
binding later fails here rather than silently widening a superuser.

This file can only SKIP in CI, because `docker/Dockerfile.test` ships no helm
binary; the enforcing leg is the `helm-smoke` job. Pinned here anyway so a
developer with helm gets it before pushing, and so the reasoning has a home.
"""

from __future__ import annotations

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


def _render() -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        if os.environ.get("TERRAPOD_REQUIRE_HELM") == "1":
            pytest.fail("TERRAPOD_REQUIRE_HELM=1 but no helm on PATH")
        pytest.skip("helm is not on PATH — the enforcing leg is the helm-smoke job")
    out = subprocess.run(
        [
            helm,
            "template",
            str(ROOT / "helm" / "terrapod"),
            "--set",
            "backup.enabled=true",
            "--set",
            "backup.restoreVerify.enabled=true",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise AssertionError(f"helm template failed: {out.stderr[-1500:]}")
    return [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]


def _sidecar() -> dict:
    for doc in _render():
        if doc.get("kind") != "CronJob":
            continue
        spec = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for c in spec.get("initContainers", []) + spec.get("containers", []):
            if c.get("name") == "throwaway-postgres":
                return c
    raise AssertionError("the restore-verify CronJob renders no throwaway-postgres sidecar")


class TestTheThrowawayPostgresIsNotReachableOffThePod:
    def test_it_binds_to_loopback(self) -> None:
        if ROOT is None:
            pytest.skip("chart is not shipped in this image")
        args = _sidecar().get("args") or []
        joined = " ".join(args)
        assert "listen_addresses=127.0.0.1" in joined, (
            "the throwaway Postgres must be started with "
            "`-c listen_addresses=127.0.0.1`. Without it Postgres defaults to "
            f"listening on every interface, and trust auth then exposes a "
            f"superuser on the pod IP. Rendered args: {args!r}"
        )

    def test_trust_auth_is_still_what_this_counterweights(self) -> None:
        """If the trust auth ever goes away the binding is no longer load-bearing,
        and a future reader should be told that rather than left guessing."""
        if ROOT is None:
            pytest.skip("chart is not shipped in this image")
        env = {e["name"]: e.get("value") for e in _sidecar().get("env", [])}
        assert env.get("POSTGRES_HOST_AUTH_METHOD") == "trust", (
            "this sidecar no longer uses trust auth, so the loopback binding is no "
            "longer the thing standing between the cluster and a superuser — "
            "re-read the test above and decide whether it still earns its place."
        )

    def test_no_tcp_port_is_advertised(self) -> None:
        """`containerPort` never controlled the binding, so advertising 5432 after
        this change would only mislead the next reader."""
        if ROOT is None:
            pytest.skip("chart is not shipped in this image")
        ports = _sidecar().get("ports") or []
        assert not ports, f"the sidecar should advertise no ports; got {ports!r}"
