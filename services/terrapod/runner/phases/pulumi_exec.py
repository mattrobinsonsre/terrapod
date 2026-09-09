"""Running a Pulumi program inside the runner Job (#1523).

Two phases, mirroring Terraform's plan/apply in Pulumi's own words:

    pulumi preview --save-plan=<file>     the preview phase
    pulumi up      --plan=<file>          the update phase

#1501 verified that pairing end to end. Saving the plan and applying *that* plan
is what makes the two phases one decision rather than two independent runs — the
same property Terraform gets from `plan -out` / `apply <file>`, and the reason an
approved preview cannot quietly apply something else.

**Plugin downloads must be redirected, and the pattern must be `.*`.**
`PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES` takes `pattern=url` pairs; a pattern that
matches nothing makes the CLI fall back to `get.pulumi.com` **silently**, so the
failure never appears for anyone with a route out and appears as a hang for
someone air-gapped. That asymmetry is why it is `.*` rather than something
anchored and tidier, and why the air-gap gate carries a Pulumi row (#1483,
#1485).

Kept in `runner/phases/` with the other phase modules, so the runner image ships
it and nothing here reaches for a model or a session.
"""

from __future__ import annotations

import json
import os

from terrapod.logging_config import get_logger

log = get_logger(__name__)


#: The prefix the runner addresses the API by.
#:
#: The alias, not the canonical `/api/v1`, and deliberately: a runner image lags
#: the API by design (the N-2 skew guarantee), so it may be talking to a server
#: on either side of #1528 and only the alias is served by both. This mirrors
#: `platform_tool.py`, which reaches the API the same way. The literal is
#: repeated rather than imported because the runner image ships no `api/`
#: package to import `prefixes` from.
_API_PREFIX = "/api/terrapod/v1"


def backend_env(api_url: str) -> dict[str, str]:
    """Point the CLI at Terrapod as its state backend (#1523).

    Pulumi resolves its backend from `pulumi login` or `PULUMI_BACKEND_URL`, and
    a runner Job has no interactive login to perform — so the URL is handed to it
    directly. Without this the CLI silently falls back to its default host: an
    air-gapped deployment hangs, and one with egress talks to the wrong backend
    entirely, which is the worse of the two because it looks like it worked.

    The base is the service surface from #1522; the CLI appends its own `/api/...`
    paths to whatever it is given, which is what lets that surface be mounted
    inside Terrapod's API namespace instead of taking the root.

    The token that authenticates against it is already set by
    `plugin_override_env` — one credential serves both the backend and the plugin
    proxy, because both are this same API.
    """
    if not api_url:
        return {}
    return {"PULUMI_BACKEND_URL": f"{api_url.rstrip('/')}{_API_PREFIX}/pulumi"}


def plugin_override_env(api_url: str, token: str) -> dict[str, str]:
    """Point plugin downloads at Terrapod rather than get.pulumi.com.

    The `.*` is load-bearing. An anchored pattern that fails to match does not
    error — the CLI simply uses its default host, so a deployment with egress
    keeps working and an air-gapped one hangs on a download nobody can see. The
    only safe pattern is the one that cannot miss.
    """
    if not api_url:
        return {}
    base = api_url.rstrip("/")
    env = {"PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES": f".*={base}{_API_PREFIX}/package-cache/pulumi"}
    if token:
        # The proxy authenticates like every other Terrapod cache; the runner's
        # own short-lived token is what it presents.
        env["PULUMI_ACCESS_TOKEN"] = token
    return env


def _common_argv(cfg) -> list[str]:  # type: ignore[no-untyped-def]
    """Flags both phases share."""
    argv: list[str] = ["--non-interactive"]
    stack = os.environ.get("TP_PULUMI_STACK", "")
    if stack:
        argv += ["--stack", stack]
    if os.environ.get("TP_REFRESH", "").lower() == "false":
        argv.append("--refresh=false")
    parallelism = os.environ.get("TP_PARALLELISM", "")
    if parallelism:
        argv += ["--parallel", parallelism]
    for urn in json.loads(os.environ.get("TP_TARGET_URNS", "[]") or "[]"):
        argv += ["--target", urn]
    return argv


def preview_argv(plan_file: str, cfg=None) -> list[str]:  # type: ignore[no-untyped-def]
    """`pulumi preview --save-plan=<file>`.

    The saved plan is what the update consumes, so the two phases agree on one
    file path — a mismatch surfaces as "no plan file" on the update, a long way
    from the preview that should have written it.
    """
    return ["preview", f"--save-plan={plan_file}", *_common_argv(cfg)]


def update_argv(plan_file: str, cfg=None) -> list[str]:  # type: ignore[no-untyped-def]
    """`pulumi up --plan=<file>`, or `destroy` when the run is a destroy.

    A destroy takes no plan: there is nothing to preview into a file that
    `destroy` would read back, and passing one is rejected.
    """
    if os.environ.get("TP_DESTROY", "").lower() == "true":
        return ["destroy", "--yes", *_common_argv(cfg)]
    return ["up", "--yes", f"--plan={plan_file}", *_common_argv(cfg)]
