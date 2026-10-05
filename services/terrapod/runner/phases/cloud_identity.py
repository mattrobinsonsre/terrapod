"""Fetch this run's cloud identity token before `init` (#1901).

The pre-engine step that turns per-workspace cloud identity into something the
operator's provider configuration can use. It asks the API to mint a short-lived
RS256 JWT for this run, writes it to one fixed path, and exports two environment
variables naming that path and the run's phase.

**One file, whatever the cloud.** Nothing here knows AWS from Azure from Vault,
and that is the design rather than a stage of it: every federation target reads
a token either from a file or from a value a configuration can read out of one,
so delivering the token is the whole job and the cloud-specific half lives in
the operator's own provider block and in `docs/cloud-identity.md`. That is what
makes the same token serve every cloud and every other OIDC-federating service
at once, instead of Terrapod growing a branch per vendor.

**Shaped exactly like `git_auth`**, because it has the same contract: return the
env overrides the caller merges into `os.environ` so the `init` subprocess
inherits them, `{}` when there is nothing to do, and raise when something was
asked for and could not be had.

The three outcomes are deliberately distinguishable, and the middle one is why
this phase cannot simply swallow failures:

* **The workspace mints nothing** — the API answers 204 and this returns `{}`.
  The run then authenticates to the cloud with the agent pool's own identity,
  exactly as it did before this feature existed. That is the normal posture for
  most workspaces, not a degraded one.
* **The workspace mints and the mint fails** — raise. Falling through here would
  not mean "no credentials", it would mean *the pool's* credentials, which are
  broader than the ones the operator deliberately moved this workspace off. A run
  that quietly succeeds under wider permissions than were chosen is worse than a
  run that fails, and it is #1442's rule applied to a credential whose failure
  mode is escalation rather than absence.
* **The runner image predates this phase** — it never calls the endpoint at all,
  so the run falls back to the pool's identity with nothing to report. That is an
  accepted, documented degradation: no runner-image version reaches the API, so
  it cannot be detected server-side. It is distinguishable from the case above
  only in that the runner never asked.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import httpx

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

#: The one path, for every cloud. Under the directory the per-run Secret mount
#: already uses, so the runner's own writable area and its delivered-file area
#: stay in one place.
TOKEN_PATH = Path("/var/run/terrapod/oidc/token")

#: Names the file. An operator's provider block can read the path from here
#: rather than hard-coding it, though the path is stable and documented either
#: way.
TOKEN_FILE_ENV = "TERRAPOD_OIDC_TOKEN_FILE"

#: The run's phase, exported so a configuration can switch role by phase — the
#: only way to do that, because HCL cannot otherwise see which phase it is in.
#: Also exported as a `TF_VAR_` so a configuration that declares
#: `variable "terrapod_run_phase"` receives it with no wiring.
PHASE_ENV = "TERRAPOD_RUN_PHASE"
PHASE_TFVAR_ENV = "TF_VAR_terrapod_run_phase"


class CloudIdentityUnavailable(RuntimeError):
    """The workspace mints an identity token and this run could not get one.

    Raised rather than warned. The fall-through is not "no cloud credentials",
    it is the agent pool's — broader than the ones this workspace was moved off
    — so continuing would run against real infrastructure under permissions the
    operator did not choose, and would succeed while doing it.
    """


def _write_private(path: Path, content: str) -> None:
    """Write at 0600, created restrictive from the outset.

    Same shape as `git_auth._write_private`: never a world-readable window
    between create and chmod, because this file is a bearer credential for the
    workspace's whole cloud identity until it expires.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def _mint(cfg, client: httpx.Client | None = None) -> dict | None:
    """Ask the API for this run's token.

    Returns the response body, or None when the workspace mints nothing (204).
    Raises `CloudIdentityUnavailable` on anything else, after retrying what is
    worth retrying — a transient 5xx or a connection error is not an answer.

    The phase is NOT sent. The server takes it from the runner token presented
    here, which is phase-bound, so a plan-phase Job cannot ask for the apply
    identity however it frames the request.
    """
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/cloud-identity-token"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))
    last: str = "no attempt made"
    try:
        for attempt in (1, 2, 3):
            try:
                resp = client.post(url, headers=headers)
                if resp.status_code == 204:
                    return None
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except json.JSONDecodeError as exc:
                        last = f"200 with a body that is not JSON: {exc}"
                        break
                # A 4xx is final: the run is gone, or this token is not scoped to
                # it. Retrying cannot change either, and retrying a refusal just
                # delays the failure.
                if 400 <= resp.status_code < 500:
                    last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    break
                last = f"HTTP {resp.status_code}"
                logger.info(
                    "cloud identity mint non-200 — will retry",
                    attempt=attempt,
                    status=resp.status_code,
                )
            except httpx.RequestError as exc:
                last = str(exc)
                logger.info(
                    "cloud identity mint request failed — will retry",
                    attempt=attempt,
                    err=str(exc),
                )
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    finally:
        if own_client:
            client.close()

    raise CloudIdentityUnavailable(
        f"This workspace is configured for cloud identity federation but a token "
        f"could not be minted for this run: {last}. Continuing would run against "
        f"the agent pool's own cloud identity, which is broader than the one this "
        f"workspace was given, so the run is failed here instead."
    )


def run(
    cfg, *, token_path: Path | None = None, client: httpx.Client | None = None
) -> dict[str, str]:
    """Mint and deliver this run's cloud identity token.

    Returns the env overrides for `os.environ`, empty when the workspace mints
    nothing. Raises `CloudIdentityUnavailable` when it mints and the token could
    not be obtained or written.
    """
    if not cfg.has_api:
        return {}

    body = _mint(cfg, client=client)
    if body is None:
        return {}

    token = (body or {}).get("token") or ""
    if not token:
        raise CloudIdentityUnavailable(
            "The API answered 200 for this run's cloud identity token but the "
            "response carried no token."
        )

    path = token_path or TOKEN_PATH
    try:
        _write_private(path, token)
    except OSError as exc:
        raise CloudIdentityUnavailable(
            f"Could not write this run's cloud identity token to {path}: {exc}."
        ) from exc

    phase = body.get("phase") or cfg.phase or ""
    env = {TOKEN_FILE_ENV: str(path)}
    if phase:
        env[PHASE_ENV] = phase
        env[PHASE_TFVAR_ENV] = phase

    # The audiences, not the token. Which identity a run presented is exactly
    # what a cloud audit log cannot tell you today, so it is worth having on our
    # side; the token itself never reaches a log, because the runner streams
    # stdout verbatim and a JWT in a log line is a credential in a log line.
    logger.info(
        "cloud identity token delivered",
        path=str(path),
        phase=phase or None,
        audiences=body.get("audiences"),
        expires_in=body.get("expires_in"),
    )
    return env
