"""Running Pulumi in a runner Job, against Terrapod as its backend (#1879).

An agent-mode Pulumi run points the CLI at Terrapod's Pulumi service surface and
drives the ordinary update lifecycle against it: begin, checkpoint, complete. The
Job holds no backend of its own.

**Why that is safe, since it was once thought not to be.** #1576 moved these runs
to a file backend inside the Job, on the reasoning that Pulumi checkpoints
continuously and a live backend would therefore move the workspace's state
mid-run with no decision point. The property that actually matters is narrower —
an apply's state must not be *published* until something decides to publish it —
and the service surface already satisfies it: a checkpoint is held against the
update and becomes a state version only when the update completes (#1564), and a
preview's lease cannot checkpoint at all (#1550). State is written continuously
and published once, which is the Terraform principle by a different mechanism.

**What this module no longer does, and must not grow back.** The file-backend
model needed a great deal of scaffolding, all of which existed only to bridge
between a stack the Job owned and one Terrapod owned: rewriting the stack ref to
the `organization/…` form a DIY backend demands, stripping the committed
secrets-provider lines from the working copy, minting a passphrase and pinning
its salt, importing the deployment on the way in and exporting it on the way out,
and sealing a saved plan with the key it was encrypted under so the update Pod
could open what the preview Pod wrote. None of it has a purpose when both phases
speak to one backend with one secrets provider, and its return would mean the
divergent second state path was back.

Two consequences worth stating because they were the point:

- a stack whose repository commits `secure:` config values simply works — the CLI
  opens them through the service, which is what #1577 existed to work around;
- the runner is never handed the deployment in plaintext, which the file backend
  required because a Job-local passphrase stack cannot open service ciphertext.

**The cost, accepted.** An agent apply is coupled to API availability in a way a
Terraform apply is not: Pulumi has no defer-writes mode, so an interruption
mid-apply can fail an update that a Terraform run — holding `terraform.tfstate`
locally and pushing once — would have survived. That is a Pulumi characteristic
rather than a Terrapod defect, and `docs/pulumi.md` says so plainly.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import structlog

logger = structlog.get_logger("runner.pulumi_exec")

#: The runner reaches the API on the DEPRECATED alias, deliberately. A runner
#: image may lag the API by design (the N-2 skew guarantee), so it may be talking
#: to a server on either side of #1528 and only the alias is served by both. This
#: mirrors `platform_tool.py`, which reaches the API the same way. The literal is
#: repeated rather than imported because the runner image ships no `api/` package
#: to import `prefixes` from.
_API_PREFIX = "/api/terrapod/v1"


class StackError(RuntimeError):
    """The run's stack could not be reached or set up, so the run must stop."""


def plugin_override_env(api_url: str, token: str, proxy_port: int) -> dict[str, str]:
    """Point plugin downloads at the loopback shim rather than get.pulumi.com.

    The `.*` is load-bearing. An anchored pattern that fails to match does not
    error — the CLI simply uses its default host, so a deployment with egress
    keeps working and an air-gapped one hangs on a download nobody can see. The
    only safe pattern is the one that cannot miss.

    **Through `CacheProxy`, not straight at the API (#1906).** The cache requires
    a credential and Pulumi's plugin downloader sends none — `PULUMI_ACCESS_TOKEN`
    belongs to the service backend and is not carried to a plugin host — so
    pointing the CLI at the API directly answered 401 on every provider download
    and no program using any provider could run. The shim holds the token.

    `proxy_port` is required rather than defaulted, because a default would make
    the broken direct URL the thing a caller gets by forgetting.
    """
    if not api_url:
        return {}
    env = {"PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES": f".*=http://127.0.0.1:{proxy_port}"}
    if token:
        env["PULUMI_ACCESS_TOKEN"] = token
    return env


class CacheProxy(threading.Thread):
    """A loopback shim that holds the run's token so a client need not carry it.

    Two clients in a Pulumi run reach Terrapod's package cache and **cannot
    authenticate to it**, for different reasons and with the same consequence:

    - **the go command**, which will talk plain HTTP to a module proxy quite
      happily but will never carry a credential over one. Not in the URL
      ("refusing to pass credentials to insecure URL"), not from a netrc, and not
      from a `GOAUTH` helper either, whose header it drops in silence. All three
      were tried against a real proxy before this was written.
    - **Pulumi's plugin downloader** (#1906), which sends no credential at all.
      `PULUMI_ACCESS_TOKEN` is the service backend's and is not carried to a
      plugin host, so every provider download answered 401 and no program using
      any provider could run.

    Every Terrapod package-cache route requires authentication, and the runner
    reaches the API over an in-cluster HTTP URL in many deployments — so on that
    path both clients can reach the cache and neither can use it.

    This closes the gap without weakening anything. It listens on 127.0.0.1,
    forwards to the API with the run's own token, and the client is pointed at it:
    the client carries no credential, so it has nothing to refuse. The token
    travels exactly the hop it already travels for this run's artifacts, its state
    and its binaries.

    **Not credentials in the URL**, which both clients would accept in some form.
    Pulumi prints the URL it fetched from in its own download warnings, and the
    runner streams its log to the API and the UI — the same reason pip's
    credential goes in a `.netrc` and npm's in an `.npmrc` rather than an index
    URL.

    `segment` names the cache it fronts (`go`, `pulumi`), and serves GET alone.
    """

    def __init__(self, api_url: str, token: str, segment: str) -> None:
        super().__init__(daemon=True)
        self._upstream = f"{api_url.rstrip('/')}{_API_PREFIX}/package-cache/{segment}"
        self._token = token
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]

    def _handler(self):  # type: ignore[no-untyped-def]
        upstream, token = self._upstream, self._token

        class Handler(BaseHTTPRequestHandler):
            # The runner's log is the run's log; the proxy's own chatter would
            # bury the client's output in it.
            def log_message(self, *args: object) -> None:  # noqa: A003
                return

            def do_GET(self) -> None:  # noqa: N802
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    # Redirects are followed HERE, not handed to the client.
                    # The cache answers a download with a 302 to presigned
                    # storage, and the go command does not follow one -- it
                    # reports the 302 as the error. The target needs no
                    # credential, so following it costs nothing and keeps the
                    # client out of it.
                    with httpx.stream(
                        "GET",
                        upstream + self.path,
                        headers=headers,
                        timeout=120.0,
                        follow_redirects=True,
                    ) as r:
                        self.send_response(r.status_code)
                        # Pulumi's downloader compares what it copied against
                        # Content-Length and fails when they disagree — and an
                        # absent header reads as -1, so it never matches:
                        # "expected -1 bytes but copied 19525050". The go
                        # command does not check, which is how this shim ran
                        # without it. Same fix as the BFF's, for the same
                        # reason.
                        #
                        # Only when the body was not content-encoded: httpx
                        # decompresses transparently, so on an encoded response
                        # the upstream's length describes bytes this shim no
                        # longer has.
                        if not r.headers.get("content-encoding"):
                            length = r.headers.get("content-length")
                            if length:
                                self.send_header("Content-Length", length)
                        ctype = r.headers.get("content-type")
                        if ctype:
                            self.send_header("Content-Type", ctype)
                        self.end_headers()
                        # Streamed, not buffered: a module zip or a provider
                        # plugin runs to tens of megabytes and this is in the
                        # runner's own process.
                        for chunk in r.iter_bytes():
                            self.wfile.write(chunk)
                except Exception as exc:  # noqa: BLE001 - reported to the client
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(str(exc).encode())

        return Handler

    def run(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def stop(self) -> None:
        """Stop serving. Safe to call whether or not the thread ever started.

        `shutdown()` waits for the serve loop to acknowledge it, so on a server
        that never began serving it blocks for ever -- which, called from the
        `finally` that guarantees cleanup, would hang the run instead of ending
        it. The liveness check is what makes the guarantee safe to make.
        """
        if self.is_alive():
            self._server.shutdown()
        self._server.server_close()


def service_backend_env(api_url: str, token: str) -> dict[str, str]:
    """Point the CLI at Terrapod as its Pulumi service backend (#1881).

    `PULUMI_BACKEND_URL` and `PULUMI_ACCESS_TOKEN` together are the env-var form
    of `pulumi login`, so the Job needs no login step and writes no credentials
    under `$HOME`.

    **Agent mode owns the backend.** These are set from the run's own
    configuration and applied AFTER any workspace-supplied environment, so a
    variable named `PULUMI_BACKEND_URL` cannot redirect a run's state somewhere
    Terrapod does not know about. The Terraform path holds the same line with its
    backend override file.

    Returns nothing without an API to talk to, which is how the runner's own
    tests and a no-API smoke run still work: the CLI then falls back to whatever
    the environment says, and a run with no API was never going to store state.
    """
    if not api_url:
        return {}
    base = api_url.rstrip("/")
    env = {"PULUMI_BACKEND_URL": f"{base}{_API_PREFIX}/pulumi"}
    if token:
        env["PULUMI_ACCESS_TOKEN"] = token
    return env


def stack_ref() -> str:
    """The stack this run operates on, as Terrapod names it.

    `default/<project>/<stack>` — the form the service backend uses and the one
    the listener already sends. The file-backend model had to rewrite this to
    `organization/<project>/<stack>`, because a DIY backend accepts no other
    organization; against Terrapod the name is used as it stands.
    """
    return os.environ.get("TP_PULUMI_STACK", "")


def _pulumi(binary: str, args: list[str], *, child_grace: float, what: str) -> None:
    """Run a setup command, raising `StackError` if it fails.

    No log file: `exec_subprocess.run` truncates the one it is given, and these
    run beside the phase's own command. Teeing to stdout puts them in the combined
    log all the same.
    """
    from terrapod.runner import exec_subprocess

    result = exec_subprocess.run(
        [binary, *args], log_file=None, child_grace_seconds=child_grace, tee_to_stdout=True
    )
    if result.exit_code != 0:
        raise StackError(f"could not {what} (pulumi exited {result.exit_code})")


def select_stack(binary: str, *, child_grace: float = 25.0) -> str:
    """Select the run's stack on the service backend, and return its ref.

    Every phase command already carries `--stack`, so this is a pre-flight rather
    than a requirement: it turns "the stack this run names does not exist" into a
    failure at the start of the run, with the CLI's own message, instead of a
    confusing one part-way through a preview.

    The stack is not created if it is missing. A Pulumi stack IS a Terrapod
    workspace, so stacks come from Terrapod — which is why the service surface
    refuses `POST /api/stacks/{org}/{project}` with a message saying exactly that.
    """
    ref = stack_ref()
    if not ref:
        raise StackError("the run names no stack")
    _pulumi(
        binary,
        ["stack", "select", ref, "--non-interactive"],
        child_grace=child_grace,
        what=f"select the stack {ref}",
    )
    logger.info("pulumi stack selected", stack=ref)
    return ref


def _common_argv(cfg) -> list[str]:  # type: ignore[no-untyped-def]
    """Flags every phase shares.

    `--refresh` is passed either way rather than only when false (#1559). The
    platform's default is `refresh: true`, and Pulumi's own default differs by
    command and by stack option, so saying nothing meant the run did whatever
    the stack happened to be configured for -- which is not what the person who
    left the setting alone asked for.
    """
    argv: list[str] = ["--non-interactive"]
    stack = stack_ref()
    if stack:
        argv += ["--stack", stack]
    refresh = os.environ.get("TP_REFRESH", "").lower() != "false"
    argv.append(f"--refresh={'true' if refresh else 'false'}")
    parallelism = os.environ.get("TP_PARALLELISM", "")
    if parallelism:
        argv += ["--parallel", parallelism]
    for urn in _urns("TP_TARGET_URNS"):
        argv += ["--target", urn]
    return argv


def _urns(var: str) -> list[str]:
    """A run option's resource list, or an empty one if it is absent or junk."""
    try:
        value = json.loads(os.environ.get(var, "[]") or "[]")
    except ValueError:
        return []
    return [str(u) for u in value] if isinstance(value, list) else []


def refresh_only_enabled() -> bool:
    """Whether this run reconciles state and stops (`pulumi refresh`)."""
    return os.environ.get("TP_REFRESH_ONLY", "").lower() == "true"


def is_destroy() -> bool:
    """Whether this run destroys what the stack manages."""
    return os.environ.get("TP_DESTROY", "").lower() == "true"


def bind_plan_enabled() -> bool:
    """Whether this run binds its update to the preview's saved plan (#1553).

    The engine sets `TP_PULUMI_BIND_PLAN` only when the workspace opts in.
    Absent — the default, and also what a listener older than the setting
    sends — means unbound.
    """
    return os.environ.get("TP_PULUMI_BIND_PLAN", "").lower() == "true"


def event_log_env(event_log: str) -> dict[str, str]:
    """What the CLI needs before it will accept `--event-log` (#1560).

    The flag is registered only when `PULUMI_DEBUG_COMMANDS` is set --
    `pkg/cmd/pulumi/operations/preview.go` guards it behind `env.DebugCommands` --
    so passing it without this env is not a no-op: the CLI exits with "unknown
    flag" and the preview never runs. Pulumi's own Automation API sets the same
    variable for the same reason (`sdk/go/auto/local_workspace.go`).

    Returned by the same function that builds the flag's argv, so the two cannot
    drift apart.
    """
    return {"PULUMI_DEBUG_COMMANDS": "true"} if event_log else {}


def preview_argv(plan_file: str, cfg=None, event_log: str = "") -> list[str]:  # type: ignore[no-untyped-def]
    """`pulumi preview`, saving its plan only when the workspace binds updates to it.

    An empty `plan_file` — the default, since binding is an opt-in (#1553) —
    saves nothing: the preview is there for a person to review, and the
    update works out its own changes, as Pulumi is normally run. With binding
    on, the saved plan is what the update consumes, so the two phases agree on
    one file path — a mismatch surfaces as "no plan file" on the update, a
    long way from the preview that should have written it.

    `event_log` asks the engine to also write its events to a file (#1560),
    which is where `has_changes` and the change counts come from. Deliberately
    not `--json`: that would replace the output a person reads with the same
    JSON, and the preview log is the thing the run page shows.
    """
    # What is previewed has to be what will run (#1559). `pulumi preview` shows
    # an ordinary update however the run was created, so a destroy run used to
    # show the approver an update and then destroy the stack, and a refresh-only
    # run showed changes it would never make. Each operation previews itself.
    if is_destroy():
        argv = ["destroy", "--preview-only"]
    elif refresh_only_enabled():
        argv = ["refresh", "--preview-only"]
    else:
        argv = ["preview"]
        # A saved plan constrains an update. `destroy` and `refresh` take no
        # plan and reject the flag, so binding does not apply to them.
        if plan_file:
            argv.append(f"--save-plan={plan_file}")
        for urn in _urns("TP_REPLACE_URNS"):
            argv += ["--replace", urn]
    if event_log:
        argv.append(f"--event-log={event_log}")
    return [*argv, *_common_argv(cfg)]


def update_argv(plan_file: str, cfg=None) -> list[str]:  # type: ignore[no-untyped-def]
    """`pulumi up --plan=<file>`, or `destroy` when the run is a destroy.

    A destroy takes no plan: there is nothing to preview into a file that
    `destroy` would read back, and passing one is rejected.

    An empty `plan_file` drops `--plan` and lets `up` compute its own. That is
    the same degradation the Terraform path makes when the plan artifact is
    unavailable (`has_plan_file`): weaker, because the update is no longer
    constrained to the operations the approved preview showed, but it still
    applies the same configuration — where refusing would strand a run whose
    preview succeeded. The caller logs when it takes this path.
    """
    if is_destroy():
        return ["destroy", "--yes", *_common_argv(cfg)]
    if refresh_only_enabled():
        # The whole operation, mirroring Terraform's refresh-only run: the
        # preview showed what reconciling state would adopt, and this performs
        # exactly that. No plan file -- `refresh` takes none.
        return ["refresh", "--yes", *_common_argv(cfg)]
    replace: list[str] = []
    for urn in _urns("TP_REPLACE_URNS"):
        replace += ["--replace", urn]
    if not plan_file:
        return ["up", "--yes", *replace, *_common_argv(cfg)]
    # A plan already names the operations, replacements included; passing
    # --replace alongside it would be asking for something the plan does not say.
    return ["up", "--yes", f"--plan={plan_file}", *_common_argv(cfg)]
