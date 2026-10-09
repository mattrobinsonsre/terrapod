"""Fetch this run's cloud identity tokens, after `init` (#1901).

The step that turns per-workspace cloud identity into something the operator's
provider configuration can use. It discovers which provider configurations the
root module actually uses, asks the API once, and writes one short-lived RS256
JWT per target to its own path.

**One token per target, each carrying one audience set.** A token audienced for
several targets is replayable between them -- anything that can read the file
can present it to any of them -- and AWS refuses a multi-valued `aud` outright.
So there is no combined token and no shared file: `<dir>/<target>/token`, and a
provider block names the one it needs.

**Discovery is unconditional, and the API is asked exactly once.** Which
provider configurations a run uses is a property of the configuration, so the
runner is the only party that can answer it; which identities a workspace holds
is a property of the platform, so the API is the only party that can answer
that. Resolving the intersection therefore needs one hop whichever way round it
is done, and sending the discovered list up is the cheaper direction: a gate
request first ("does this run mint anything?") would make it two hops for every
federated run to save one engine invocation on the rest -- and `graph` is a
static walk of the configuration, needing no network, no credentials and no
state. The honest reading of a gate is that it optimises the common case by
making the feature's own case worse.

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

* **The API does not serve this at all** -- a 404, which is an API older than
  this runner image. Read as "nothing to do", because in agent mode the control
  plane and a runner in another cluster upgrade independently and the absence of
  the route is information rather than a fault. Every other failure of that
  request IS fatal: the runner cannot complete a run without the API in any
  case, so failing adds no realistic new failure mode, and treating it as
  advisory would let anyone able to disrupt one call downgrade a workspace to
  the pool's broader identity without trace.
* **The workspace mints nothing** -- the API answers 204 and this returns `{}`.
  The run then authenticates with the agent pool's own identity, exactly as it
  did before this feature existed. That is the normal posture for most
  workspaces, not a degraded one.
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

**Because discovery now runs before the API has said whether anything is
configured, the runner reports what it saw rather than deciding on it.** Three
outcomes, and only the API can judge them, because only the API knows whether
this workspace holds any identity at all:

* `ok` -- the graph was read. The target list is authoritative, and may be
  empty for a configuration that declares no provider.
* `failed` -- the graph command errored or timed out.
* `unparsed` -- the graph ran and its output mentions provider nodes, but none
  matched. That is our pattern or the engine's output having moved, never an
  empty answer, and it is the one case a target list cannot express.

The last two are only a problem for a workspace that holds identity, and they
are a serious one: both produce an empty target list, which is indistinguishable
from a provider-less configuration and would otherwise be a silent fall-through
to the pool's identity. Sending the outcome up is what lets the API refuse them
while leaving every workspace that configures nothing completely unaffected.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field
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

#: Names the directory, not any one file, for a shell hook or script that would
#: rather not hard-code it. There is deliberately NO `TF_VAR_` counterpart: a
#: provider block names the documented path directly
#: (`/var/run/terrapod/oidc/<target>/token`), because exporting a Terraform
#: variable would reserve a name inside the operator's own configuration to say
#: something the path already says. The path is the contract; a second way to
#: spell it is a misdirect.
TOKEN_DIR_ENV = "TERRAPOD_OIDC_TOKEN_DIR"

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

#: Cap on what one run may ask to mint. Target names come out of the engine's
#: output rather than from us, so the request is bounded here rather than
#: trusting the graph to be reasonable -- the API bounds it again, because a
#: runner is not a trusted input either.
MAX_TARGETS = 100

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
#: to it yields an empty discovery.
_PROVIDER_NODE = re.compile(r'provider\[\\"([^\\"]+)\\"\](\.[A-Za-z0-9_-]+)?')

#: The bare node prefix, unescaped and unanchored. Its presence in output that
#: produced no `_PROVIDER_NODE` match is what separates "this configuration
#: declares no provider" from "we can no longer read the graph" -- the one
#: distinction a target list cannot carry, and the one that decides whether an
#: empty result is an answer or a defect.
_PROVIDER_MENTION = "provider["


class CloudIdentityUnavailable(RuntimeError):
    """The workspace mints identity tokens and this run could not get them.

    Raised rather than warned. The fall-through is not "no cloud credentials",
    it is the agent pool's -- broader than the ones this workspace was moved off
    -- so continuing would run against real infrastructure under permissions the
    operator did not choose, and would succeed while doing it.
    """


@dataclass(frozen=True)
class Discovery:
    """What the engine's graph said, and how much to trust it.

    `outcome` is sent to the API verbatim, because whether a bad outcome matters
    depends on something only the API knows: a graph we cannot read is fatal for
    a workspace that holds identity and irrelevant for one that does not.
    """

    outcome: str
    targets: set[str] = field(default_factory=set)
    detail: str = ""


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


def discover(*, binary: str, cwd: Path) -> Discovery:
    """Which provider configurations the root module uses, and how sure we are.

    Runs the engine's own graph command, which answers exactly the question and
    prunes configurations nothing references -- so a `provider "aws" { alias =
    "unused" }` nobody points at yields no token, correctly. It is a static walk
    of the configuration: measured against OpenTofu 1.12.6 it needs no network,
    no credentials and no state, and succeeds on a configuration whose `plan`
    refuses for a missing required variable.

    Never raises. This runs before the API has said whether the workspace holds
    any identity, so a graph failure is not yet known to matter -- the outcome
    goes up and the API, which does know, decides. Raising here would fail runs
    that configure no cloud identity at all.
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
        return Discovery(outcome="failed", detail=f"{binary} graph failed to run: {exc}")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return Discovery(
            outcome="failed", detail=f"{binary} graph exited {proc.returncode}. {tail}"
        )

    output = proc.stdout or ""
    targets = parse_graph(output)
    if not targets and _PROVIDER_MENTION in output:
        return Discovery(
            outcome="unparsed",
            detail=(
                f"{binary} graph named provider nodes but none could be read. The "
                f"engine's graph format has moved, or the pattern that reads it has."
            ),
        )
    return Discovery(outcome="ok", targets=targets)


def _request(
    cfg,
    client: httpx.Client,
    method: str,
    url: str,
    *,
    what: str,
    not_found_ok: bool = False,
    json_body: dict | None = None,
) -> dict | None:
    """One retried API call. Returns the body, or None on 204.

    Retries what is worth retrying -- a transient 5xx or a connection error is
    not an answer -- and treats a 4xx as final, because the run being gone, this
    token not being scoped to it, or the configuration having moved since the
    plan cannot be fixed by asking again.

    `not_found_ok` reads a 404 as None rather than a failure, because the
    route's absence is information: an API older than this runner image does not
    serve it at all, and in agent mode the control plane and a runner in another
    cluster upgrade independently. "This API has no such feature" is the same
    answer as "this run mints nothing", and both mean fall through to the agent
    pool's identity. The API's own "run not found" is also a 404 and is read the
    same way, which is harmless: a run that does not exist is not executing.
    """
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
    last = "no attempt made"
    for attempt in (1, 2, 3):
        try:
            resp = client.request(method, url, headers=headers, json=json_body)
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
                last = f"HTTP {resp.status_code}: {resp.text[:400]}"
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


def run(
    cfg,
    *,
    binary: str,
    cwd: Path,
    token_dir: Path | None = None,
    client: httpx.Client | None = None,
    discover_fn=None,
) -> dict[str, str]:
    """Mint and deliver this run's cloud identity tokens.

    Returns the env overrides for `os.environ`, empty when the workspace mints
    nothing. Raises `CloudIdentityUnavailable` when it mints and the tokens could
    not be obtained or written.

    `discover_fn` is injectable so a test can drive the whole phase without an
    engine binary; production passes nothing and gets `discover`.
    """
    if not cfg.has_api:
        return {}

    found = (discover_fn or discover)(binary=binary, cwd=cwd)
    # Sorted and capped before it leaves. The names come from the engine's
    # output, so the request is bounded on the way out as well as on the way in,
    # and a stable order makes the API's log line and ours comparable.
    asking = sorted(found.targets)[:MAX_TARGETS]
    logger.info(
        "cloud identity discovery",
        outcome=found.outcome,
        used=asking,
        detail=found.detail or None,
    )

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))
    try:
        body = _request(
            cfg,
            client,
            "POST",
            f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/cloud-identity-tokens",
            what="minting this run's cloud identity tokens",
            not_found_ok=True,
            json_body={
                "providers": asking,
                "discovery": found.outcome,
                "discovery-detail": found.detail[:400],
            },
        )
    finally:
        if own_client:
            client.close()

    # 204, or a body carrying no tokens: this run mints nothing -- the issuer is
    # not published, the workspace holds no identity, or nothing it holds is
    # used by this configuration. Fall through to the agent pool's identity,
    # exactly as before this feature existed.
    minted = (body or {}).get("tokens") or []
    if not minted:
        return {}

    directory = token_dir or TOKEN_DIR
    delivered: list[str] = []
    for entry in minted:
        target = str(entry.get("target") or "")
        token = entry.get("token") or ""
        if not target or not token:
            raise CloudIdentityUnavailable(
                "The API returned a cloud identity entry with no target or no token, "
                "so there is no way to tell which identity it is for."
            )

        # The API refuses these too, but this is a separate image that may be
        # older or newer than the API it is talking to, and `directory / target`
        # silently accepts a separator or a parent reference: `aws./../vault`
        # lands at the `vault` path and an absolute target escapes the directory
        # entirely. The token is a credential, so the check belongs at the write
        # as well as at the request.
        #
        # `target in (".", "..")` names the two explicitly, mirroring the API's
        # `unsafe_target_reason`. It replaced `".." in target.split(".")`, which
        # could not fire at all: splitting ON the dot means no element it yields
        # can contain one, so `"..".split(".")` is `["", "", ""]` and the clause
        # was unconditionally false for every input. A bare `..` therefore wrote
        # `<token dir>/../token`, one level above the directory, and a bare `.`
        # wrote `<token dir>/token` -- the combined-token path that is
        # deliberately never written, because a token audienced for several
        # targets is replayable between them.
        if "/" in target or "\\" in target or "\x00" in target or target in (".", ".."):
            raise CloudIdentityUnavailable(
                f"The API returned a cloud identity target {target!r} that is not a provider "
                "configuration name — it would not resolve to a path inside this run's token "
                "directory, so it has not been written."
            )

        path = directory / target / "token"
        try:
            _write_private(path, token)
        except OSError as exc:
            raise CloudIdentityUnavailable(
                f"Could not write this run's cloud identity token for {target!r} to {path}: {exc}."
            ) from exc

        delivered.append(target)
        # The audiences, not the token. Which identity a run presented is
        # exactly what a cloud audit log cannot tell you today, so it is worth
        # having on our side; the token itself never reaches a log, because the
        # runner streams stdout verbatim and a JWT in a log line is a credential
        # in a log line.
        logger.info(
            "cloud identity token delivered",
            target=target,
            path=str(path),
            audiences=entry.get("audiences"),
        )

    phase = (body or {}).get("phase") or cfg.phase or ""
    env = {TOKEN_DIR_ENV: str(directory)}
    if phase:
        env[PHASE_ENV] = phase
        env[PHASE_TFVAR_ENV] = phase
    return env
