"""Running a Pulumi program inside the runner Job (#1523, #1576).

Two phases, mirroring Terraform's plan/apply in Pulumi's own words:

    pulumi preview [--save-plan=<file>]   the preview phase
    pulumi up      [--plan=<file>]        the update phase

#1501 verified that pairing end to end. Saving the plan and applying *that* plan
is what makes the two phases one decision rather than two independent runs — the
same property Terraform gets from `plan -out` / `apply <file>`, and the reason an
approved preview cannot quietly apply something else.

**State stays in the Job (#1576).** An agent run never uses Terrapod as a live
Pulumi backend. The stack lives in a file backend in this Job's workspace, the way
a Terraform run keeps `terraform.tfstate` beside its configuration: the stack's
deployment is fetched through the run's artifact API and imported at the start,
and after an update it is exported and handed back once. A preview hands back
nothing. The file backend needs a secrets provider, so each stack gets a
passphrase that exists only for the life of the Job; the deployment arrives with
its secrets in plaintext for exactly that reason, and leaves the same way, to be
sealed again by the API.

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
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

# structlog directly, as every other phase module does — not
# `terrapod.logging_config.get_logger`, which is a one-line wrapper around this
# exact call and is *not* shipped in the runner image. Importing it cost nothing
# in tests, where the whole package is importable, and crashed every Pulumi run
# on `ModuleNotFoundError: No module named 'terrapod.logging_config'` the moment
# the orchestrator reached the phase. The runner image copies a hand-listed set
# of modules; anything outside it does not exist at runtime.
log = structlog.get_logger("runner.pulumi_exec")


#: The prefix the runner addresses the API by.
#:
#: The alias, not the canonical `/api/v1`, and deliberately: a runner image lags
#: the API by design (the N-2 skew guarantee), so it may be talking to a server
#: on either side of #1528 and only the alias is served by both. This mirrors
#: `platform_tool.py`, which reaches the API the same way. The literal is
#: repeated rather than imported because the runner image ships no `api/`
#: package to import `prefixes` from.
_API_PREFIX = "/api/terrapod/v1"

#: Where the run's stack lives. Inside the workspace emptyDir, so it is writable
#: under the hardened pod and goes when the Pod does.
DEFAULT_STATE_DIR = "/workspace/.terrapod-pulumi"

#: The lines of a committed `Pulumi.<stack>.yaml` that record which secrets
#: provider the stack was last used with.
_SECRETS_CONFIG_LINE = re.compile(r"^(encryptionsalt|secretsprovider|encryptedkey)\s*:")
_SALT_LINE = re.compile(r"^encryptionsalt\s*:\s*[\"']?([^\"'\s]+)", re.MULTILINE)
#: A `secure:` config value, block (`secure: v1:...`) or flow (`{secure: ...}`).
_SECURE_VALUE = re.compile(r"(^|[\s{,])secure\s*:", re.MULTILINE)

#: Marks a plan file that carries the key it was sealed under (see `bundle_plan`).
_PLAN_BUNDLE_MARK = "terrapod-pulumi-plan"


class LocalStackError(RuntimeError):
    """The run's stack could not be set up or read back, so the run must stop."""


@dataclass(frozen=True)
class StackKeys:
    """What seals the stack's secrets: its salt line and its passphrase."""

    salt: str
    passphrase: str


@dataclass(frozen=True)
class LocalStack:
    """The run's stack as set up in this Job."""

    #: `organization/<project>/<stack>`, as the file backend spells it.
    ref: str
    #: The working copy's `Pulumi.<stack>.yaml`.
    config_path: Path
    state_dir: Path
    keys: StackKeys
    #: The state serial the imported deployment was read at; quoted back on upload.
    base_serial: int
    #: The deployment as imported, secrets in plaintext; None for a new stack.
    deployment: dict[str, Any] | None


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
        # own short-lived token is what it presents. It is the token's only use:
        # the backend is a directory in this Job, and the service surface refuses
        # a runner token outright (#1576).
        env["PULUMI_ACCESS_TOKEN"] = token
    return env


def local_backend_env(state_dir: Path, passphrase: str) -> dict[str, str]:
    """Point the CLI at a file backend in this Job, keyed by `passphrase`.

    Set in the environment rather than by `pulumi login`, which would also write
    credentials under `$HOME` — and would leave whatever an operator's workspace
    variables set for `PULUMI_BACKEND_URL` able to win. Agent mode owns the
    backend, as it does Terraform's with the override file.
    """
    return {
        "PULUMI_BACKEND_URL": state_dir.resolve().as_uri(),
        "PULUMI_CONFIG_PASSPHRASE": passphrase,
    }


def local_stack_ref(tp_stack: str) -> str:
    """The run's stack, `default/<project>/<stack>`, as the file backend names it.

    The file backend takes a fully qualified name only under the literal
    organization `organization`. A two-part `<project>/<stack>` is read as
    `<org>/<stack>` and refused with "organization name must be 'organization'",
    which is what the first attempt at this did.
    """
    parts = [p for p in tp_stack.split("/") if p]
    if len(parts) == 3:
        return f"organization/{parts[1]}/{parts[2]}"
    return tp_stack


def has_secure_config(path: Path) -> bool:
    """Whether the committed stack config holds `secure:` values."""
    if not path.exists():
        return False
    body = "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    return bool(_SECURE_VALUE.search(body))


def reset_secrets_config(path: Path, salt: str = "") -> None:
    """Drop the committed provider lines from the working copy; optionally pin a salt.

    A committed `Pulumi.<stack>.yaml` records the provider the stack was last used
    with — Terrapod's service, a passphrase, a KMS key — and none of them is the
    one this Job's stack uses. Left in place, `stack init` either adopts a salt it
    has no passphrase for ("incorrect passphrase") or a provider it cannot reach.
    Only the working copy changes, never the repository.

    `salt` is written back when the update must open what its preview sealed.
    """
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    kept = [line for line in lines if not _SECRETS_CONFIG_LINE.match(line)]
    if salt:
        kept.insert(0, f"encryptionsalt: {salt}\n")
    if kept or path.exists():
        path.write_text("".join(kept))


def read_salt(path: Path) -> str:
    """The `encryptionsalt` `stack init` recorded, or "" when there is none."""
    if not path.exists():
        return ""
    match = _SALT_LINE.search(path.read_text())
    return match.group(1) if match else ""


def seed_document(deployment: dict[str, Any], salt: str) -> dict[str, Any]:
    """The `stack import` body for this Job's stack.

    The deployment arrives with no provider block and its secrets in plaintext.
    Import needs one naming the new stack's own passphrase salt: without a block
    the CLI crashes ("fatal error. This is a bug!"), and with the salt the stack
    last used it refuses ("incorrect passphrase"). With this one, import accepts
    the plaintext and stores it sealed — all three verified on CLI v3.262.0.
    """
    return {
        "version": 3,
        "deployment": {
            **deployment,
            "secrets_providers": {"type": "passphrase", "state": {"salt": salt}},
        },
    }


def _material(deployment: dict[str, Any] | None) -> dict[str, Any]:
    """What makes two deployments the same stack.

    The manifest is stamped with the time on every write and the provider block
    names a key, so neither says anything about the stack; empty values are
    dropped so a new stack with no resources equals no deployment at all.
    """
    if not deployment:
        return {}
    return {
        k: v
        for k, v in deployment.items()
        if k not in ("manifest", "secrets_providers") and v not in (None, [], {})
    }


def deployment_changed(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    """Whether an update left the stack different from the one it imported."""
    return _material(before) != _material(after)


def bundle_plan(path: Path, keys: StackKeys) -> None:
    """Wrap a saved plan, in place, with the key it was sealed under.

    A saved plan carries its secrets as ciphertext under the preview's stack key,
    and the update runs in another Pod with a stack of its own. Given a fresh
    key, `up --plan` fails with "decrypting secret value: cipher: message
    authentication failed", which is what the #1576 spike's control run did.
    Carrying the key beside the plan leaves the plan's secrets exactly as exposed
    as Terraform's own plan file leaves its sensitive values, which it holds in
    the clear.
    """
    doc = {
        _PLAN_BUNDLE_MARK: 1,
        "encryptionsalt": keys.salt,
        "passphrase": keys.passphrase,
        "plan": path.read_text(),
    }
    path.write_text(json.dumps(doc))


def unbundle_plan(path: Path) -> StackKeys | None:
    """Restore a bundled plan in place and return its key.

    None for a file that is not a bundle — a plan saved by a runner that predates
    bundling — which is left untouched for `up --plan` to try as it stands.
    """
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get(_PLAN_BUNDLE_MARK) != 1:
        return None
    path.write_text(doc["plan"])
    return StackKeys(salt=doc["encryptionsalt"], passphrase=doc["passphrase"])


def _pulumi(binary: str, args: list[str], *, child_grace: float, what: str) -> None:
    """Run a setup or hand-back command, raising `LocalStackError` if it fails.

    No log file: `exec_subprocess.run` truncates the one it is given, and these
    run beside the phase's own command. Teeing to stdout puts them in the combined
    log all the same.
    """
    from terrapod.runner import exec_subprocess

    result = exec_subprocess.run(
        [binary, *args], log_file=None, child_grace_seconds=child_grace, tee_to_stdout=True
    )
    if result.exit_code != 0:
        raise LocalStackError(f"could not {what} (pulumi exited {result.exit_code})")


def _write_private(path: Path, body: str) -> None:
    """Write a file only this process can read — it holds plaintext secrets."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)


def prepare_local_stack(
    cfg,  # type: ignore[no-untyped-def]
    binary: str,
    *,
    keys: StackKeys | None = None,
    child_grace: float = 25.0,
) -> LocalStack:
    """Create the run's stack in a file backend here and import its state.

    `keys` is given only to a bound update, which must open what its preview
    sealed; every other phase gets a passphrase of its own.

    Must run in the program's directory, where `Pulumi.<stack>.yaml` lives.
    """
    from terrapod.runner.phases import state as state_phase

    tp_stack = os.environ.get("TP_PULUMI_STACK", "")
    if not tp_stack:
        raise LocalStackError("the run names no stack")
    ref = local_stack_ref(tp_stack)
    config_path = Path.cwd() / f"Pulumi.{ref.rsplit('/', 1)[-1]}.yaml"

    if has_secure_config(config_path):
        # Those values are sealed by whatever provider the stack used when they
        # were set, and a passphrase made for this Job cannot open them.
        raise LocalStackError(
            f"{config_path.name} holds `secure:` config values. Agent runs cannot "
            "open them yet (#1577); supply those values as workspace variables instead"
        )

    state_dir = Path(os.environ.get("TP_PULUMI_STATE_DIR", DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    passphrase = keys.passphrase if keys else secrets.token_urlsafe(32)
    # The file form would compete with the passphrase set here.
    os.environ.pop("PULUMI_CONFIG_PASSPHRASE_FILE", None)
    os.environ.update(local_backend_env(state_dir, passphrase))
    reset_secrets_config(config_path, keys.salt if keys else "")

    _pulumi(
        binary,
        ["stack", "init", ref, "--secrets-provider", "passphrase", "--non-interactive"],
        child_grace=child_grace,
        what="create the run's stack",
    )
    salt = read_salt(config_path)
    if not salt:
        raise LocalStackError(f"stack init recorded no encryption salt in {config_path.name}")

    base_serial, deployment = 0, None
    if cfg.has_api:
        try:
            base_serial, deployment = state_phase.download_pulumi_deployment(cfg)
        except state_phase.StateDownloadError as exc:
            raise LocalStackError(str(exc)) from exc

    if deployment:
        seed_path = state_dir / "import.json"
        _write_private(seed_path, json.dumps(seed_document(deployment, salt)))
        try:
            _pulumi(
                binary,
                ["stack", "import", "--stack", ref, "--file", str(seed_path), "--non-interactive"],
                child_grace=child_grace,
                what="import the stack's state",
            )
        finally:
            seed_path.unlink(missing_ok=True)

    log.info("pulumi stack ready", stack=ref, serial=base_serial, imported=bool(deployment))
    return LocalStack(
        ref=ref,
        config_path=config_path,
        state_dir=state_dir,
        keys=StackKeys(salt=salt, passphrase=passphrase),
        base_serial=base_serial,
        deployment=deployment,
    )


def export_local_stack(
    binary: str, stack: LocalStack, *, child_grace: float = 25.0
) -> tuple[Path, dict[str, Any] | None]:
    """Export the run's stack with its secrets shown, for handing back.

    Returns the file — which the caller uploads and then deletes, since it holds
    plaintext — and its deployment.
    """
    dest = stack.state_dir / "export.json"
    _pulumi(
        binary,
        ["stack", "export", "--stack", stack.ref, "--show-secrets", "--file", str(dest)],
        child_grace=child_grace,
        what="export the stack after the update",
    )
    os.chmod(dest, 0o600)
    try:
        doc = json.loads(dest.read_text())
    except (OSError, ValueError) as exc:
        dest.unlink(missing_ok=True)
        raise LocalStackError(f"the exported stack could not be read: {exc}") from exc
    deployment = doc.get("deployment") if isinstance(doc, dict) else None
    return dest, deployment if isinstance(deployment, dict) else None


def _common_argv(cfg) -> list[str]:  # type: ignore[no-untyped-def]
    """Flags both phases share."""
    argv: list[str] = ["--non-interactive"]
    stack = os.environ.get("TP_PULUMI_STACK", "")
    if stack:
        argv += ["--stack", local_stack_ref(stack)]
    if os.environ.get("TP_REFRESH", "").lower() == "false":
        argv.append("--refresh=false")
    parallelism = os.environ.get("TP_PARALLELISM", "")
    if parallelism:
        argv += ["--parallel", parallelism]
    for urn in json.loads(os.environ.get("TP_TARGET_URNS", "[]") or "[]"):
        argv += ["--target", urn]
    return argv


def bind_plan_enabled() -> bool:
    """Whether this run binds its update to the preview's saved plan (#1553).

    The engine sets `TP_PULUMI_BIND_PLAN` only when the workspace opts in.
    Absent — the default, and also what a listener older than the setting
    sends — means unbound.
    """
    return os.environ.get("TP_PULUMI_BIND_PLAN", "").lower() == "true"


def preview_argv(plan_file: str, cfg=None) -> list[str]:  # type: ignore[no-untyped-def]
    """`pulumi preview`, saving its plan only when the workspace binds updates to it.

    An empty `plan_file` — the default, since binding is an opt-in (#1553) —
    saves nothing: the preview is there for a person to review, and the
    update works out its own changes, as Pulumi is normally run. With binding
    on, the saved plan is what the update consumes, so the two phases agree on
    one file path — a mismatch surfaces as "no plan file" on the update, a
    long way from the preview that should have written it.
    """
    if not plan_file:
        return ["preview", *_common_argv(cfg)]
    return ["preview", f"--save-plan={plan_file}", *_common_argv(cfg)]


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
    if os.environ.get("TP_DESTROY", "").lower() == "true":
        return ["destroy", "--yes", *_common_argv(cfg)]
    if not plan_file:
        return ["up", "--yes", *_common_argv(cfg)]
    return ["up", "--yes", f"--plan={plan_file}", *_common_argv(cfg)]
