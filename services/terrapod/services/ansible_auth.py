"""The shape of an Ansible credential, shared by the API and the runner (#1933).

Two credential classes reach a configure, both carried by the existing
credential-category mechanism (#1028's `git_http_auth`/`git_ssh_auth`): the
machine credential ansible connects with, and the Ansible Vault password it
decrypts its own secrets with. `key` is the SCOPE in both, exactly as it is a
URL pattern for git auth.

**This module is PURE and must stay so -- `typing` and the standard library,
nothing from Terrapod.** It ships in the runner image, which installs 21
packages and no PyYAML: a module-level import of anything heavier here makes
`job_entrypoint` unimportable, and because the entrypoint imports the configure
phase at module level, *every run of every kind* then dies at startup rather
than only a configure. That is not hypothetical -- it is how `inventory_render`
came to exist, and it reached a real runner pod.

**Why the field names live here rather than once per side.** The API validates
the envelope at the write boundary and the runner reads it back at configure
time. A disagreement about a single key -- `become_pass` against
`become_password` -- is invisible: the API accepts the credential, the runner
finds nothing under the name it expects, and the configure runs with no
escalation and no error. One producer removes the class.

## The machine credential

`key` is a host pattern: the literal `all`, or the name of a group in the
workspace's inventory. The runner writes key material to a `0600` file on the
shared volume and injects the *path* -- never the key -- into the resolved
inventory as variables for that scope, so **ansible's own group precedence does
the scoping** and a per-group credential costs no logic of ours.

The injected document is the runner's local copy, rendered into the Job's
shared volume from the artifact the API stored at run creation. So a password
delivered this way reaches a file beside ansible and **never** the stored
artifact, the Resolved view, or anything holding inventory read.

## The Ansible Vault password

`key` is the vault ID label and `value` is `{"password": …}`. Several entries
become one `ANSIBLE_VAULT_IDENTITY_LIST`, which is how multiple vault IDs work
(measured, ansible-core 2.18.3: a labelled list decrypts both secrets, and an
unlabelled `!vault` block is tried against each identity in turn, so the
single-password case needs no special handling). A label is therefore part of an
env var whose delimiters are `,` and `@`, which is why both are refused.
"""

from __future__ import annotations

import json
import re
from typing import Any

MACHINE_CATEGORY = "ansible_machine_auth"
VAULT_CATEGORY = "ansible_vault_auth"

#: The scope meaning "every host in this run". Ansible's own root group, which
#: is also why `inventory_resolution.DERIVED_GROUPS` refuses it as a *declared*
#: group name: it is derived, so nothing may control its membership -- but it is
#: exactly the right name for a credential that applies to everything.
ALL_HOSTS = "all"

#: Envelope field -> the inventory variable whose value is a FILE PATH we write.
#: Separate from the value fields because the file has to be materialised, and
#: because the distinction is the security property: the key never appears in
#: the document, only the path to it.
MACHINE_FILE_FIELDS: dict[str, str] = {
    "private_key": "ansible_ssh_private_key_file",
}

#: Envelope field -> the inventory variable carrying the value verbatim.
MACHINE_VALUE_FIELDS: dict[str, str] = {
    "user": "ansible_user",
    "password": "ansible_password",
    "become": "ansible_become",
    "become_method": "ansible_become_method",
    "become_user": "ansible_become_user",
    "become_password": "ansible_become_password",
}

MACHINE_FIELDS = frozenset(MACHINE_FILE_FIELDS) | frozenset(MACHINE_VALUE_FIELDS)
VAULT_FIELDS = frozenset({"password"})

#: The fields whose value is secret, so the runner can register them with the
#: log redactor without re-deriving which is which.
MACHINE_SECRET_FIELDS = frozenset({"private_key", "password", "become_password"})

#: A vault ID label is interpolated into `ANSIBLE_VAULT_IDENTITY_LIST`, whose
#: syntax is `label@file,label@file`. A label containing either delimiter does
#: not produce a broken label -- it produces a DIFFERENT, silently wrong list.
_VAULT_LABEL = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

#: `become` is a boolean in ansible; accept the strings an operator would write
#: and refuse anything else rather than letting a typo read as false.
_TRUE = frozenset({"true", "yes", "1", "on"})
_FALSE = frozenset({"false", "no", "0", "off"})


class AnsibleAuthError(ValueError):
    """An Ansible credential cannot be used as written.

    Raised by `validate_envelope` and translated to HTTP 422 by the variables
    router. Refused at the write boundary rather than dropped at resolution:
    git auth drops a malformed envelope inside its resolver, which spends the
    mistake on an `init` failure naming neither the credential nor the cause
    (#1442). A configure would spend it worse -- part way through a fleet.
    """


def is_ansible_auth(category: str) -> bool:
    """Whether this category's value is one of the two Ansible envelopes."""
    return category in (MACHINE_CATEGORY, VAULT_CATEGORY)


def parse_boolean(raw: str) -> bool:
    """Ansible's own boolean spellings; raise on anything else."""
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise AnsibleAuthError(
        f"{raw!r} is not a boolean: write one of "
        f"{', '.join(sorted(_TRUE | _FALSE))}. A value ansible cannot read as "
        f"true would read as false, which silently drops privilege escalation."
    )


def validate_envelope(category: str, key: str, value: str) -> dict[str, Any]:
    """Return the parsed envelope for a credential write, or raise.

    Args:
        category: `ansible_machine_auth` or `ansible_vault_auth`. Any other
            category returns `{}` -- this is called from a shared write guard
            that sees every category.
        key: the credential's scope. A host pattern for a machine credential, a
            vault ID label for a vault password.
        value: the JSON envelope as written.

    Raises:
        AnsibleAuthError: the scope or the envelope cannot be used. The message
            names the field, because "invalid credential" sends an operator to
            the wrong place.
    """
    if not is_ansible_auth(category):
        return {}
    if not isinstance(value, str) or not value.strip():
        raise AnsibleAuthError(
            f"category {category!r} needs a JSON object as its value, and this one is empty"
        )
    try:
        parsed = json.loads(value)
    except ValueError as exc:
        raise AnsibleAuthError(
            f"category {category!r} needs a JSON object as its value: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AnsibleAuthError(
            f"category {category!r} needs a JSON OBJECT as its value, not a {type(parsed).__name__}"
        )

    allowed = MACHINE_FIELDS if category == MACHINE_CATEGORY else VAULT_FIELDS
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        # Refused rather than ignored: a credential field under a name nothing
        # reads is a credential that silently does not apply, and the operator's
        # next signal is a configure that cannot connect.
        raise AnsibleAuthError(
            f"category {category!r} does not carry {', '.join(repr(u) for u in unknown)}; "
            f"its fields are {', '.join(sorted(allowed))}"
        )
    for field, field_value in parsed.items():
        if not isinstance(field_value, str):
            raise AnsibleAuthError(
                f"{field!r} must be a string; a {type(field_value).__name__} cannot be "
                f"written to a credential file or an inventory variable"
            )
    if "become" in parsed:
        parse_boolean(parsed["become"])

    if category == VAULT_CATEGORY:
        if not parsed.get("password"):
            raise AnsibleAuthError(
                "an Ansible Vault credential needs a non-empty 'password'; without one "
                "there is nothing to decrypt with and the configure would fail mid-play"
            )
        _validate_vault_label(key)
        return parsed

    if not (parsed.get("private_key") or parsed.get("password")):
        raise AnsibleAuthError(
            "a machine credential needs 'private_key' or 'password'; with neither, "
            "ansible has no way to authenticate and the credential does nothing"
        )
    return parsed


def delivery_entries(resolved: Any) -> list[dict[str, str]]:
    """The `[{category, key, value}]` a configure's Secret carries.

    `resolved` is the full `ResolvedVariable` list from `resolve_variables`; only
    the two Ansible categories are consumed. Duck-typed on `.category`, `.key`
    and `.value` rather than imported, because this module is pure and importing
    the dataclass would pull the service layer into the runner image.

    A malformed envelope is **dropped with a warning left to the caller** rather
    than raised here: it was already refused at the write boundary by
    `_credential_envelope_guard`, so a bad one at this point means a row written
    before that guard existed, and failing the whole run for it would make a
    historical row un-runnable rather than merely inert. The runner still raises
    for a credential that was DECLARED and could not be applied (#1442) -- the
    two are different: this is "unreadable", that is "readable and unusable".
    """
    out: list[dict[str, str]] = []
    for v in resolved:
        category = getattr(v, "category", "")
        if not is_ansible_auth(category):
            continue
        try:
            validate_envelope(category, getattr(v, "key", ""), getattr(v, "value", ""))
        except AnsibleAuthError:
            continue
        out.append({"category": category, "key": v.key, "value": v.value})
    return out


def _validate_vault_label(key: str) -> None:
    if not _VAULT_LABEL.match(key or ""):
        raise AnsibleAuthError(
            f"vault ID label {key!r} must start with a letter, digit or underscore and "
            f"contain only those plus '.' and '-'. The label is interpolated into "
            f"ANSIBLE_VAULT_IDENTITY_LIST, whose syntax is 'label@file,label@file', so "
            f"a ',' or '@' in it produces a different list rather than a rejected one."
        )
