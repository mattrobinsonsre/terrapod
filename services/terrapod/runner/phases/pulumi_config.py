"""Set the workspace's Pulumi stack config before the run (#1565, #1898).

A workspace's **native** variables are the engine's own parameter channel, and
on a Pulumi workspace that channel is stack config. They arrive in the same
delivered blob a Terraform run renders into `terrapod.auto.tfvars` — one role,
one list, three deliveries (#1898) — and this phase is Pulumi's delivery of it:
`pulumi config set`, run against the selected stack before the preview. There is
no tfvars file here, because Pulumi has no such thing; config is a flat
key/value namespace the program reads at will, and nothing declares it in
advance.

The two flags each entry carries mean whatever the engine can make of them.
Here `sensitive` becomes `--secret` and `structured` becomes `--path`. A
Terraform run reads `structured` too — it is what decides a raw HCL expression
against a quoted string — but ignores `sensitive` entirely, because there the
file is the mechanism and every value is written into it the same way. A
delivery uses what it can and ignores the rest, which is why one list serves
every engine.

**Keys pass through verbatim, and nothing is prefixed** (#1407 §6). Pulumi
namespaces an unqualified key to the project named in `Pulumi.yaml`, and the
runner is already executing in the project directory, so `region` becomes
`myproject:region` unaided. The explicit `namespace:key` form exists for
provider config such as `aws:region`, which an operator types in full and which
must survive untouched. Both were verified against the CLI rather than assumed:
a transformation here is a thing that can be wrong, so there is none.

**The merge with a committed `Pulumi.<stack>.yaml` is per key, and Terrapod
wins.** `pulumi config set` edits the stack's config file in place, so a key the
workspace sets overwrites the committed value of the same name and a committed
key the workspace does not set survives untouched. That is the decided rule
(#1565) and it is also simply what the mechanism does — there is no merge to
implement, only one to rely on and to test. The reason it is the right way round
is the platform's: a value held in Terrapod is rotatable, RBAC'd and audited,
and a committed one is none of those, so the platform's value is the override.

**`sensitive` means a Pulumi secret, not merely careful delivery.** #1407 §6
called this the genuinely open question, and the answer is `--secret`: the value
is encrypted by the stack's own secrets provider — in agent mode, the one
Terrapod's service backend holds. That buys what Terraform's model cannot here.
Pulumi's *engine* then renders the value as `[secret]` in the preview a reviewer
reads, in the event log, and in any state it reaches. Delivering a sensitive
value as ordinary config would put it in plaintext in the stack config file and
in the preview output, which is the wrong default to arrive at by omission.

**No value ever reaches an argv.** Pulumi takes the value on **stdin** when it
is absent from the command line, and does so under `--non-interactive` — checked
against the CLI, because the help text calls it a prompt and a prompt is exactly
what a non-interactive run does not get. So the command line carries the key and
the flags, and the secret goes down a pipe. This is the same guarantee
`git_auth` holds and for the same reason: the runner streams its output to the
API and the UI, so log-safety has to be mechanism rather than redaction.

**Exactly one trailing newline is appended, and this is load-bearing.** Pulumi
strips one trailing newline from what it reads on stdin, so a value written
as-is loses its own final newline — which silently corrupts anything
line-oriented, a PEM key being the obvious one. Appending one means Pulumi
strips the one we added and the operator's value survives byte-for-byte,
including when it genuinely ends in a newline. Measured, not assumed.

**A failure fails the run.** An operator set that value deliberately, and a
program that runs without it is doing something nobody asked for: `config.get`
with a default would quietly take the default, and only `config.require` would
complain. Dropping the entry with a warning would be the silent-drop failure
this issue exists to remove.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger("runner.pulumi_config")

#: Mounted from the per-run vars Secret — the SAME blob a Terraform run renders
#: into its tfvars file (#1898). Keep in sync with runner/job_template.py
#: (_TFVARS_SECRET_KEY / _TFVARS_FILENAME), the listener's _create_vars_secret,
#: and `job_entrypoint._VARS_FILE`.
#:
#: The file keeps its Terraform name on purpose: it is part of the
#: listener-to-runner contract that a runner up to N-2 minors behind reads, and
#: identifiers keep their names the way `terraform.tfvars` does under OpenTofu.
_CONFIG_FILE = Path("/var/run/terrapod/vars/terraform.tfvars.json")


class ConfigError(RuntimeError):
    """A config value could not be set, so the run must not continue."""


def _load(path: Path = _CONFIG_FILE) -> list[dict]:
    """Read the delivered entries, or an empty list when there are none.

    A workspace with no native variables mounts no file, which is not an error
    — the stack simply runs on whatever its repository committed.
    """
    if not path.exists():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ConfigError(f"could not read the delivered Pulumi config: {exc}") from exc
    if not isinstance(parsed, list):
        raise ConfigError("the delivered Pulumi config is not a list")
    return [e for e in parsed if isinstance(e, dict)]


def _set_one(binary: str, entry: dict, *, stack: str, timeout: float) -> None:
    """Set one key, with its value on stdin.

    Nothing about the value is logged, formatted into a message, or placed on
    the command line — including in the failure path, where pulumi's own stderr
    is reported but the value is not. pulumi does not echo the value it read, so
    passing its message through is safe; the key and the flags are ours and are
    safe by construction.
    """
    key = str(entry.get("key") or "")
    if not key:
        raise ConfigError("a delivered Pulumi config entry has no key")

    argv = [binary, "config", "set"]
    # `sensitive`/`structured` are the delivered blob's names (#1898). A
    # listener that predates it sends neither, which resolves to a plain,
    # non-nested `config set` -- exactly what every run did before secrets and
    # paths existed, rather than a failure.
    if entry.get("sensitive"):
        argv.append("--secret")
    if entry.get("structured"):
        argv.append("--path")
    if stack:
        argv += ["--stack", stack]
    argv += ["--non-interactive", key]

    # The one appended newline pulumi will strip; see the module docstring.
    value = f"{entry.get('value') or ''}\n"

    try:
        result = subprocess.run(  # noqa: S603 — argv is built here, never interpolated
            argv,
            input=value.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"could not set Pulumi config key {key!r}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise ConfigError(
            f"could not set Pulumi config key {key!r} (pulumi exited {result.returncode}): {detail}"
        )


def apply(
    binary: str,
    *,
    stack: str = "",
    path: Path = _CONFIG_FILE,
    timeout: float = 60.0,
) -> int:
    """Set every delivered config value on the stack. Returns how many were set.

    Called after the stack is selected and before the preview or update, so the
    program sees the workspace's config on both phases — they run in different
    pods, so this happens twice, exactly as dependency installation does.
    """
    entries = _load(path)
    if not entries:
        return 0

    for entry in entries:
        _set_one(binary, entry, stack=stack, timeout=timeout)

    # Keys and the secret flag only. The count is what an operator wants in the
    # log, and the keys are what they need to match against the workspace.
    logger.info(
        "pulumi config set",
        count=len(entries),
        keys=[str(e.get("key") or "") for e in entries],
        secret_keys=[str(e.get("key") or "") for e in entries if e.get("sensitive")],
    )
    return len(entries)
