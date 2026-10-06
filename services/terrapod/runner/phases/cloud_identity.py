"""Fetch this run's cloud identity tokens, after `init` (#1901).

The step that turns per-workspace cloud identity into something the operator's
provider configuration can use. It asks the API which provider configurations
this run mints for, discovers which of them the root module actually uses, and
writes one short-lived RS256 JWT per target to its own path.

**One token per target, each carrying one audience set.** A token audienced for
several targets is replayable between them -- anything that can read the file
can present it to any of them -- and AWS refuses a multi-valued `aud` outright.
So there is no combined token and no shared file: `<dir>/<target>/token`, and a
provider block names the one it needs.

**Nothing here knows AWS from Azure from Vault**, and that is the design rather
than a stage of it. Every federation target reads a token from a file, so
delivering the file is the whole job and the cloud-specific half lives in the
operator's provider block and in `docs/cloud-identity.md`. It is also why this
phase does not set `AWS_WEB_IDENTITY_TOKEN_FILE` or any sibling: the Job spec
carries no cloud credential environment at all, which is precisely what lets a
workspace that has not opted in keep the agent pool's own IRSA or Workload
Identity untouched. Coexistence is achieved by absence.

**After `init`, not before.** Discovery asks the engine, which cannot answer
until the providers are installed -- and with Terragrunt the working directory
moves after `init` too, so this must run against the relocated one. The cost is
that a `pre_init` hook can no longer see the tokens; a hook that talks to a
cloud belongs at `pre_plan` or `pre_apply`, both of which run after this.

**Shaped exactly like `git_auth`**, because it has the same contract: return the
env overrides the caller merges into `os.environ` so the engine subprocess
inherits them, `{}` when there is nothing to do, and raise when something was
asked for and could not be had.

The outcomes are deliberately distinguishable, and the middle ones are why this
phase cannot simply swallow failures:

* **The API does not serve this at all** -- a 404 on the targets request, which
  is an API older than this runner image. Read as "nothing to do", because in
  agent mode the control plane and a runner in another cluster upgrade
  independently and the absence of the route is information rather than a fault.
  Every other failure of that request IS fatal: the runner cannot complete a run
  without the API in any case, so failing adds no realistic new failure mode,
  and treating it as advisory would let anyone able to disrupt one call downgrade
  a workspace to the pool's broader identity without trace.
* **The workspace mints nothing** -- the API answers 204 to the targets request
  and this returns `{}` without ever invoking the engine. The run then
  authenticates with the agent pool's own identity, exactly as it did before
  this feature existed. That is the normal posture for most workspaces, not a
  degraded one, and it is why discovery is gated behind that request rather than
  run unconditionally: a workspace not using this feature pays nothing for it
  and gains no new way to fail.
* **The workspace mints and something fails** -- raise. Falling through would
  not mean "no credentials", it would mean *the pool's* credentials, which are
  broader than the ones the operator deliberately moved this workspace off. A
  run that quietly succeeds under wider permissions than were chosen is worse
  than a run that fails, and it is #1442's rule applied to a credential whose
  failure mode is escalation rather than absence.
* **The runner image predates this phase** -- it never calls the endpoint at
  all, so the run falls back to the pool's identity with nothing to report. That
  is an accepted, documented degradation: no runner-image version reaches the
  API, so it cannot be detected server-side. It is distinguishable from the case
  above only in that the runner never asked.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import httpx
import structlog

# `structlog` directly, not `terrapod.logging_config`: the runner image ships
# only the modules Dockerfile.runner names, and that one is not among them.
# Importing it raises ModuleNotFoundError inside every runner Job while every
# test on a full checkout passes -- which is exactly how it got here. Matches
# every sibling phase, and `debug_linger`, which says the same thing.
logger = structlog.get_logger("runner.phase.cloud_identity")

#: One directory, one subdirectory per target, for every cloud. Under the path
#: the per-run Secret mount already uses, so the runner's writable area and its
#: delivered-file area stay in one place.
TOKEN_DIR = Path("/var/run/terrapod/oidc")

#: Names the directory, not any one file. An operator's provider block builds
#: the path it needs -- `"${var.terrapod_oidc_token_dir}/aws/token"` -- because a
#: target name may carry a dot (`aws.west`) and there is no sane environment
#: variable name for that. One documented convention beats a mangling rule.
TOKEN_DIR_ENV = "TERRAPOD_OIDC_TOKEN_DIR"
TOKEN_DIR_TFVAR_ENV = "TF_VAR_terrapod_oidc_token_dir"

#: The run's phase, exported so a configuration can switch role by phase -- the
#: only way to do that, because HCL cannot otherwise see which phase it is in.
#: Also exported as a `TF_VAR_` so a configuration that declares
#: `variable "terrapod_run_phase"` receives it with no wiring.
PHASE_ENV = "TERRAPOD_RUN_PHASE"
PHASE_TFVAR_ENV = "TF_VAR_terrapod_run_phase"

#: How long the engine gets to render the dependency graph. Generous: it is a
#: graph build, not an evaluation, but a very large root module on a slow node
#: should not be failed for being slow.
DISCOVER_TIMEOUT_SECONDS = 180

#: A provider configuration as the engine's DOT output spells it, with the
#: source address quote-escaped inside the node string:
#:
#:     "[root] provider[\"registry.opentofu.org/hashicorp/aws\"].west"
#:
#: Matched anywhere in the output rather than only on node-declaration lines,
#: and deliberately NOT keyed on `shape = "diamond"`. Two reasons, and the
#: second is the one that matters: the engine prunes provider configurations
#: nothing references, so every occurrence is a configuration the root module
#: actually uses; and keying on a cosmetic attribute would mean a future change
#: to it yields an empty discovery, which is a silent fall-through to the pool's
#: identity. The shape of this pattern cannot change without the graph becoming
#: unreadable.
_PROVIDER_NODE = re.compile(r'provider\[\\"([^\\"]+)\\"\](\.[A-Za-z0-9_-]+)?')


class CloudIdentityUnavailable(RuntimeError):
    """The workspace mints identity tokens and this run could not get them.

    Raised rather than warned. The fall-through is not "no cloud credentials",
    it is the agent pool's -- broader than the ones this workspace was moved off
    -- so continuing would run against real infrastructure under permissions the
    operator did not choose, and would succeed while doing it.
    """


def _write_private(path: Path, content: str) -> None:
    """Write at 0600, created restrictive from the outset.

    Same shape as `git_auth._write_private`: never a world-readable window
    between create and chmod, because this file is a bearer credential for one
    of the workspace's cloud identities until it expires.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def _target_name(source: str, alias: str | None) -> str:
    """`registry.opentofu.org/hashicorp/aws` + `.west` -> `aws.west`.

    The bare provider type, because that is what an operator writes in a
    `provider` block and therefore the only name they can be expected to use as
    a configuration key. Two registry namespaces publishing the same type is not
    a concern: a single configuration cannot use both, so within one workspace
    the type is unambiguous.
    """
    name = source.rsplit("/", 1)[-1]
    return f"{name}{alias}" if alias else name


def parse_graph(output: str) -> set[str]:
    """Every provider configuration the graph mentions, as `type[.alias]`."""
    return {_target_name(m.group(1), m.group(2)) for m in _PROVIDER_NODE.finditer(output)}


def discover_targets(*, binary: str, cwd: Path) -> set[str]:
    """Which provider configurations the root module actually uses.

    Runs the engine's own graph command, which answers exactly the question and
    prunes configurations nothing references -- so a `provider "aws" { alias =
    "unused" }` nobody points at yields no token, correctly.

    Raises on failure. By the time this is reached the API has already said this
    run mints for something, so the operator has asked for federation and a
    graph we cannot read means we cannot tell which identity to present. A root
    module whose graph will not build cannot be planned either, so the run is
    doomed regardless and failing here says why in one line instead of at the
    cloud's token exchange.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - the engine binary the run already uses
            [binary, "graph"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=DISCOVER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloudIdentityUnavailable(
            f"Could not discover this run's provider configurations: {binary} graph "
            f"failed to run ({exc}). This workspace is configured for cloud identity "
            f"federation, so the run is failed here rather than continuing under the "
            f"agent pool's own identity."
        ) from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise CloudIdentityUnavailable(
            f"Could not discover this run's provider configurations: {binary} graph "
            f"exited {proc.returncode}. {tail}"
        )

    return parse_graph(proc.stdout or "")


def _request(
    cfg,
    client: httpx.Client,
    method: str,
    url: str,
    *,
    what: str,
    not_found_ok: bool = False,
    params: dict[str, str] | None = None,
) -> dict | None:
    """One retried API call. Returns the body, or None on 204.

    Retries what is worth retrying -- a transient 5xx or a connection error is
    not an answer -- and treats a 4xx as final, because the run being gone or
    this token not being scoped to it cannot be fixed by asking again.

    `not_found_ok` reads a 404 as None rather than a failure, for the one call
    where the route's absence is information: an API older than this runner
    image does not serve the targets endpoint at all, and in agent mode the
    control plane and a runner in another cluster upgrade independently. "This
    API has no such feature" is the same answer as "this run mints nothing", and
    both mean fall through to the agent pool's identity.
    """
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
    last = "no attempt made"
    for attempt in (1, 2, 3):
        try:
            resp = client.request(method, url, headers=headers, params=params)
            if resp.status_code == 204:
                return None
            if resp.status_code == 404 and not_found_ok:
                logger.info("cloud identity not served by this API — falling through", url=url)
                return None
            if resp.status_code == 200:
                try:
                    return resp.json()
                except json.JSONDecodeError as exc:
                    last = f"200 with a body that is not JSON: {exc}"
                    break
            if 400 <= resp.status_code < 500:
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                break
            last = f"HTTP {resp.status_code}"
            logger.info(f"{what} non-200 — will retry", attempt=attempt, status=resp.status_code)
        except httpx.RequestError as exc:
            last = str(exc)
            logger.info(f"{what} request failed — will retry", attempt=attempt, err=str(exc))
        if attempt < 3:
            time.sleep(2 ** (attempt - 1))

    raise CloudIdentityUnavailable(
        f"This workspace is configured for cloud identity federation but {what} "
        f"failed for this run: {last}. Continuing would run against the agent pool's "
        f"own cloud identity, which is broader than the one this workspace was given, "
        f"so the run is failed here instead."
    )


def _base(cfg) -> str:
    return f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}"


def run(
    cfg,
    *,
    binary: str,
    cwd: Path,
    token_dir: Path | None = None,
    client: httpx.Client | None = None,
    discover=None,
) -> dict[str, str]:
    """Mint and deliver this run's cloud identity tokens.

    Returns the env overrides for `os.environ`, empty when the workspace mints
    nothing. Raises `CloudIdentityUnavailable` when it mints and the tokens could
    not be obtained or written.

    `discover` is injectable so a test can drive the whole phase without an
    engine binary; production passes nothing and gets `discover_targets`.
    """
    if not cfg.has_api:
        return {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))
    try:
        body = _request(
            cfg,
            client,
            "GET",
            f"{_base(cfg)}/cloud-identity-targets",
            what="the cloud identity targets request",
            not_found_ok=True,
        )
        # 204, or a body naming no targets: this run mints nothing. Return
        # before touching the engine -- see the module docstring on why the
        # gating matters.
        configured = [str(t) for t in (body or {}).get("targets") or []]
        if not configured:
            return {}

        used = (discover or discover_targets)(binary=binary, cwd=cwd)
        if not used:
            # The run mints for something and the graph named no provider at
            # all. A configuration that reaches a cloud with no provider
            # configuration does not exist, so this is our parser or the
            # engine's output having moved -- not an empty answer. Failing here
            # is what stops that becoming a silent fall-through to the pool's
            # identity.
            raise CloudIdentityUnavailable(
                "This workspace is configured for cloud identity federation but no "
                "provider configuration could be read out of the dependency graph, "
                "so there is no way to tell which identity to present. The run is "
                "failed here rather than continuing under the agent pool's own "
                "identity."
            )

        # Only the intersection. A configured target the root module never uses
        # is not an error -- the mapping is per workspace and a configuration
        # need not use every provider in it -- and a used provider nothing maps
        # to is the common case for most providers in most workspaces.
        wanted = sorted(set(configured) & used)
        logger.info(
            "cloud identity discovery",
            configured=sorted(configured),
            used=sorted(used),
            minting_for=wanted,
        )
        if not wanted:
            return {}

        directory = token_dir or TOKEN_DIR
        phase = ""
        delivered: list[str] = []
        for target in wanted:
            minted = _request(
                cfg,
                client,
                "POST",
                f"{_base(cfg)}/cloud-identity-token",
                # Through `params`, not interpolated into the URL. A target name
                # is derived from the engine's graph output rather than written
                # by us, so letting it reach the query string unencoded would
                # make the engine's output able to shape the request.
                params={"target": target},
                what=f"minting the cloud identity token for {target!r}",
            )
            if minted is None:
                # Raced: the mapping lost this target between the two calls.
                # Not a failure -- nothing now maps to it, which is the same
                # answer as never having been configured for it.
                logger.info("cloud identity target no longer maps", target=target)
                continue

            token = minted.get("token") or ""
            if not token:
                raise CloudIdentityUnavailable(
                    f"The API answered 200 for {target!r} but the response carried no token."
                )

            path = directory / target / "token"
            try:
                _write_private(path, token)
            except OSError as exc:
                raise CloudIdentityUnavailable(
                    f"Could not write this run's cloud identity token for {target!r} "
                    f"to {path}: {exc}."
                ) from exc

            phase = phase or (minted.get("phase") or "")
            delivered.append(target)
            # The audiences, not the token. Which identity a run presented is
            # exactly what a cloud audit log cannot tell you today, so it is
            # worth having on our side; the token itself never reaches a log,
            # because the runner streams stdout verbatim and a JWT in a log line
            # is a credential in a log line.
            logger.info(
                "cloud identity token delivered",
                target=target,
                path=str(path),
                audiences=minted.get("audiences"),
                expires_in=minted.get("expires_in"),
            )
    finally:
        if own_client:
            client.close()

    if not delivered:
        return {}

    phase = phase or cfg.phase or ""
    env = {TOKEN_DIR_ENV: str(directory), TOKEN_DIR_TFVAR_ENV: str(directory)}
    if phase:
        env[PHASE_ENV] = phase
        env[PHASE_TFVAR_ENV] = phase
    return env
