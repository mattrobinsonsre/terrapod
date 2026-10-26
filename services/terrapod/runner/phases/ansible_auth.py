"""Materialise Ansible credentials before the playbook runs (#1933).

Ansible reaches managed hosts over SSH and decrypts its own secrets with Vault
passwords, and neither fits the `terraform`/`env` variable categories. Two
credential categories carry them, on exactly #1028's path: declared as sensitive
workspace variables (or variable-set variables, which is how one vault password
is shared across the workspaces that need it), resolved server-side, delivered
through the per-run Secret, and materialised here before the tool runs.

This phase mirrors `git_auth`'s contract, deliberately, because the contract is
the part that was got right once already:

* write each credential to a `0600` file,
* return the environment overrides the caller merges,
* return nothing when there is nothing usable,
* and **raise when a credential was DECLARED and could not be applied** --
  #1442's reasoning. Warning and continuing produced a failure several minutes
  later naming neither the credential nor the cause; for a configure it would be
  worse, because by then some of the fleet has been changed.

Three things here differ from git auth, each because ansible runs in another
container:

* **The files go on the shared volume, not the orchestrator's `/tmp`.** The EE
  mounts only `/tp` and `/workspace`, so the PVC-tempfile rule's usual
  destination is invisible to the only process that must read these. Their mode
  is `0600` -- see `CREDENTIAL_FILE_MODE`. It was `0640` while the EE's uid was
  unpinned and the reader might not have shared ours; #1928 pinned it.

* **The environment is RETURNED, never exported.** `configure._forced_env`
  strips every inherited `ANSIBLE_*` before setting its own -- which is what
  stops an inherited `ANSIBLE_INVENTORY_ENABLED=auto` re-enabling the plugin
  dispatcher -- so an `ANSIBLE_VAULT_IDENTITY_LIST` put in `os.environ` here
  would be stripped on the way out, silently, and the configure would run with
  no vault password. It has to travel as data.

* **A machine credential is referred to BY NAME from the inventory, never
  injected.** `key` is the credential's name, and nothing on the credential
  decides which hosts it reaches: the inventory does, by a
  `${terrapod:<name>.<field>}` placeholder this phase resolves and
  `configure_inventory` substitutes into the document it renders. Only the file
  PATH is substituted for a file field; the key never enters the document. That
  render is local to the Job, so a password delivered this way reaches a file
  beside ansible and never the stored artifact, the Resolved view, or anything
  holding inventory read.

**Precedence is ansible's own, because a substituted placeholder is an ordinary
inventory variable.** Host beats group beats `all`, with no Terrapod rule to
learn, and a value the operator set WITHOUT a placeholder outranks one with it
at a lower level.

**That bullet described `key` as "the scope -- `all`, or a group" until
2026-10-26, and the wrong version is recorded here rather than quietly
replaced.** It was the design before placeholders, and believing it is not
harmless: its whole premise is that placement is automatic, so a reader writes
no `${terrapod:...}` placeholder at all. `all` satisfies `CREDENTIAL_NAME`, so
`key: all` is accepted at the write boundary -- measured -- and then nothing
refers to the credential, nothing resolves it, and because an ABSENT reference
is not an unresolved one the run is not refused either. It reports hosts it
could not reach, naming neither the credential nor the reason. Seven surfaces
still carried that account after the mechanism became placeholder-based;
`scripts/docs-audit/check_credential_key_prose.py` is what stops the eighth.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog

from terrapod.runner import exec_subprocess
from terrapod.services.ansible_auth import (
    CREDENTIAL_FILE_MODE,
    MACHINE_CATEGORY,
    MACHINE_FILE_FIELDS,
    MACHINE_SECRET_FIELDS,
    MACHINE_VALUE_FIELDS,
    VAULT_CATEGORY,
    AnsibleAuthError,
    parse_boolean,
)

logger = structlog.get_logger("runner.ansible_auth")

#: Mounted from the per-run vars Secret. Keep in sync with
#: `runner/job_template.py` (`_ANSIBLE_AUTH_SECRET_KEY` / `_ANSIBLE_AUTH_FILENAME`)
#: and the listener's `_create_vars_secret`.
_AUTH_FILE = Path("/var/run/terrapod/vars/ansible-auth.json")

#: Matches `job_template._EE_CREDENTIALS_DIR`, so a runner-tier test needs no Job.
_DEFAULT_CREDENTIALS_DIR = "/tp/credentials"

#: The redaction floor for a secret field delivered as a VALUE (#1934).
#:
#: Eight rather than `exec_subprocess._MIN_SECRET_LEN`, and the split is the
#: point rather than a loosening: the default floor exists for the cloud identity
#: token and justifies itself by that token's shape, while a `password` or
#: `become_password` is substituted into the rendered inventory, so nothing but
#: the redactor stands between it and the two streams. Eight is the floor
#: `summary_redaction.MIN_LITERAL_LENGTH` already uses for matching a literal in
#: free text, so this is the house number rather than a new one -- and below it
#: the trade genuinely inverts, because a value like `prod` matches ordinary
#: output and a log with half its words blacked out stops being read.
#:
#: `private_key` keeps the default floor on purpose: it arrives as a 0600 file,
#: which is the control, and it cannot be short.
_VALUE_SECRET_MIN_LENGTH = 8

#: The env var carrying every labelled vault password at once. One variable for
#: any number of vault IDs: measured against ansible-core 2.18.3, a labelled
#: list decrypts every labelled secret and tries an unlabelled `!vault` block
#: against each identity in turn -- so the single-password case needs no
#: special handling, and a run with only the WRONG password fails loudly on
#: ansible's own "no vault secrets were found that could decrypt". Fail-closed
#: is ansible's behaviour, not something this phase has to add.
_IDENTITY_LIST_ENV = "ANSIBLE_VAULT_IDENTITY_LIST"


class AnsibleAuthUnavailable(RuntimeError):
    """Credentials were declared for this run and could not be applied.

    Fatal, not advisory (#1442). A configure that proceeds without its machine
    credential reaches no host and reports an unreachable fleet; one that
    proceeds without its vault password fails part way through a play, on a
    host it has already changed. Both name something other than the cause.
    """


def credentials_dir() -> Path:
    """The shared directory credential files are written to.

    From `TP_EE_CREDENTIALS_DIR`, which the Job sets. Read from the environment
    for the same reason as `configure_inventory.inventory_dir`: the path is a
    property of the Job's volume layout, not of the run.
    """
    return Path(os.environ.get("TP_EE_CREDENTIALS_DIR") or _DEFAULT_CREDENTIALS_DIR)


def _load(path: Path = _AUTH_FILE) -> list[dict]:
    """Read the mounted credential file.

    Returns `[]` when absent, which is the ordinary state of every plan and
    apply -- the API sends the attribute only for a configure -- and of a
    configure on a workspace that declares no credentials. An absent file is
    therefore not an error, while an unreadable one is: it means credentials
    were delivered and cannot be used.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AnsibleAuthUnavailable(
            f"the delivered Ansible credentials at {path} could not be read ({exc}), so a "
            f"configure would run with no way to authenticate or decrypt"
        ) from exc
    if not isinstance(data, list):
        raise AnsibleAuthUnavailable(
            f"the delivered Ansible credentials at {path} are a "
            f"{type(data).__name__}, not a list of credentials"
        )
    return data


def _write_secret_file(directory: Path, name: str, content: str) -> Path:
    """Write `content` to a `CREDENTIAL_FILE_MODE` file and return its path.

    The mode is set at CREATION rather than chmod-ed afterwards: a key written
    with default permissions and then narrowed is readable for the window in
    between.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CREDENTIAL_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return target


def _safe_name(category: str, key: str) -> str:
    """A filename for a credential's NAME that cannot escape its directory.

    The name is already validated at the write boundary -- `CREDENTIAL_NAME`
    restricts it to characters that cannot traverse -- so this is the belt to
    that braces: a row written before the guard existed, or a future name rule
    that widens, must not be able to name `../`.
    """
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in key) or "default"
    suffix = "vault" if category == VAULT_CATEGORY else "machine"
    return f"{suffix}-{cleaned}"


def run(
    *, path: Path = _AUTH_FILE, directory: Path | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Materialise this run's Ansible credentials.

    Returns `(env, resolved)`:

    * `env` is merged by the caller into the environment `ee_client` hands the
      EE. Empty when no vault password was delivered.
    * `resolved` maps `"<name>.<field>"` to what a placeholder referring to it
      stands for -- the PATH of the file written, for a field delivered as a
      file, or the value itself. `configure_inventory` substitutes these into
      the document it renders.

    Both empty means no credentials were declared, which is an ordinary
    configure against hosts reachable with the EE's own defaults.

    **Every field of every machine credential is resolved, whether or not the
    inventory refers to it.** Which ones are used is the inventory's business,
    and a phase that tried to resolve only the referenced ones would have to
    read the inventory to find out -- coupling the two steps for nothing, since
    an unreferenced credential costs one file nobody opens.

    Raises:
        AnsibleAuthUnavailable: credentials were delivered and could not be
            applied. Never raised for their absence.
    """
    entries = _load(path)
    if not entries:
        return {}, {}

    target = directory if directory is not None else credentials_dir()
    identities: list[str] = []
    resolved: dict[str, str] = {}
    failures: list[str] = []

    for entry in entries:
        category = str(entry.get("category", ""))
        key = str(entry.get("key", ""))
        raw = entry.get("value", "")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(envelope, dict):
                raise AnsibleAuthError(f"its value is a {type(envelope).__name__}, not an object")
            if category == VAULT_CATEGORY:
                identities.append(_apply_vault(target, key, envelope))
            elif category == MACHINE_CATEGORY:
                resolved.update(_apply_machine(target, key, envelope))
            else:
                # Delivered under a category this phase does not handle. Not a
                # failure: a future category would arrive here on a lagging
                # runner, and refusing would break a forward-compatible delivery.
                logger.warning("ignoring an unknown ansible credential category", category=category)
        except (AnsibleAuthError, OSError, ValueError) as exc:
            failures.append(f"{category} {key!r}: {exc}")

    if failures:
        # Declared and unusable -- fatal, per the module docstring. Every
        # failure is named, because fixing one of three and re-running to find
        # the next is the experience this exists to avoid.
        raise AnsibleAuthUnavailable(
            "Ansible credentials were declared for this run and could not be applied: "
            + "; ".join(failures)
        )

    env: dict[str, str] = {}
    if identities:
        # Sorted so the same credentials produce the same list, which makes the
        # value assertable and keeps a log line stable between runs.
        env[_IDENTITY_LIST_ENV] = ",".join(sorted(identities))
    logger.info(
        "ansible credentials materialised",
        vault_ids=len(identities),
        # The REFERENCES, never the values: this line goes to the run log.
        resolvable=sorted(resolved),
    )
    return env, resolved


def _apply_vault(directory: Path, key: str, envelope: dict[str, Any]) -> str:
    """Write one vault password and return its `label@path` identity."""
    password = envelope.get("password") or ""
    if not password:
        raise AnsibleAuthError("it carries no 'password', so there is nothing to decrypt with")
    # Registered BEFORE the file exists, so there is no window in which ansible
    # could print it unredacted. Short values are not registered -- see
    # `exec_subprocess.register_secret` -- and that is why the mechanism matters
    # more than the backstop: the password reaches ansible as a FILE, never on
    # an argv and never as an environment variable's value.
    exec_subprocess.register_secret(password)
    written = _write_secret_file(directory, _safe_name(VAULT_CATEGORY, key), password)
    label = key or "default"
    return f"{label}@{written}"


def _apply_machine(directory: Path, key: str, envelope: dict[str, Any]) -> dict[str, str]:
    """Resolve one machine credential's fields; return `{"name.field": value}`.

    A field in `MACHINE_FILE_FIELDS` resolves to the PATH of a file this writes.
    Everything else resolves to its own value. That split is the whole reason a
    placeholder names a field rather than just a credential.
    """
    out: dict[str, str] = {}
    for field in sorted(MACHINE_FILE_FIELDS):
        material = envelope.get(field)
        if not material:
            continue
        exec_subprocess.register_secret(material)
        # A private key must end in a newline or ssh refuses it with "invalid
        # format", which names neither the key nor the missing byte.
        body = material if material.endswith("\n") else material + "\n"
        written = _write_secret_file(
            directory, f"{_safe_name(MACHINE_CATEGORY, key)}.{field}", body
        )
        out[f"{key}.{field}"] = str(written)
    for field in sorted(MACHINE_VALUE_FIELDS):
        value = envelope.get(field)
        if value is None or value == "":
            continue
        if field == "become":
            # Validated at the write boundary; normalised here so an inventory
            # substituting it gets something ansible reads as a boolean.
            out[f"{key}.{field}"] = "true" if parse_boolean(str(value)) else "false"
            continue
        if field in MACHINE_SECRET_FIELDS:
            # A LOWER floor than the default, because this field has no
            # mechanism behind it (#1934, user's decision 2026-10-26). A private
            # key and a vault password reach ansible as 0600 files, so the file
            # is the control and the redactor is a backstop -- which is what the
            # default floor's own reasoning rests on, and that reasoning does
            # not transfer here: a connection or become password is substituted
            # into the rendered inventory as a VALUE, so redaction is the only
            # control over it. The default floor was chosen for a JWT's shape
            # and has nothing to say about a sudo password an operator typed.
            exec_subprocess.register_secret(str(value), min_length=_VALUE_SECRET_MIN_LENGTH)
        out[f"{key}.{field}"] = str(value)
    if not out:
        raise AnsibleAuthError("it carries no usable field")
    return out
