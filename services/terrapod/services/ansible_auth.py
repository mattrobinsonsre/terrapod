"""The shape of an Ansible credential, shared by the API and the runner (#1933).

Two credential classes reach a configure, both carried by the existing
credential-category mechanism (#1028's `git_http_auth`/`git_ssh_auth`): the
machine credential ansible connects with, and the Ansible Vault password it
decrypts its own secrets with. `key` is the credential's NAME in both.

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

## Where a credential is USED is said in the inventory

A credential is a NAMED secret and nothing more. The inventory refers to it by a
placeholder, which the runner substitutes while rendering:

    [web:vars]
    ansible_ssh_private_key_file=${terrapod:web-key.private_key}

    [db1]
    db1.example.com ansible_ssh_private_key_file=${terrapod:db-key.private_key}

**Placement belongs to the inventory because that is already where an operator
says which host gets what.** Scoping the credential as well would be a second
place to say the same thing and a second place for the two to disagree -- and it
would only ever fill the connection variables this module thought to enumerate,
where a placeholder fills any of them. An estate whose per-host difference is
expressed through `ansible_ssh_common_args` and a ProxyCommand is served by the
same mechanism as one using `ansible_ssh_private_key_file`, with nothing here
knowing the difference.

It also makes "the API ships no secrets" structural rather than policed: the
artifact the API stores carries the PLACEHOLDER, because that is what the
inventory says, and substitution happens only in the runner's own local render.

The field selector is not decoration. `private_key` substitutes the PATH of a
mode-restricted file the runner writes; `password` substitutes the value. One
credential genuinely has both shapes, and `MACHINE_FILE_FIELDS` below is the
one place that says which is which.

## The Ansible Vault password

`key` is the vault ID label -- which is its name, used as the label -- and
`value` is `{"password": …}`. Several entries become one
`ANSIBLE_VAULT_IDENTITY_LIST`, which is how multiple vault IDs work (measured,
ansible-core 2.18.3: a labelled list decrypts both secrets, and an unlabelled
`!vault` block is tried against each identity in turn, so the single-password
case needs no special handling). The label is part of an env var whose
delimiters are `,` and `@`, which is why both are refused.

A vault password needs no placeholder: it is delivered by the environment rather
than by a variable, so there is nowhere in the inventory for it to be named.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The mode for ANY file on the EE's shared volume that carries credential
#: material: owner read/write, and nothing for group or other.
#:
#: Owner-only is reachable because the EE runs as the SAME uid as the
#: orchestrator: `job_template._ee_security_context` pins `runAsUser` to 1000,
#: so there is exactly one reader and it is us.
#:
#: This was `0640` and the group bit was load-bearing: `fsGroup` granted access
#: through the GROUP, so `0600` would have worked only for an EE running as uid
#: 1000 and an operator-supplied EE on another uid would have failed to read its
#: own credentials after the configure had started. **That premise is gone**:
#: `_ee_security_context` pins `runAsUser` to 1000 (#1928), so there is no longer
#: a reader that does not share our uid -- and these are the files it would be
#: worst to leave open, holding SSH private keys, connection passwords and vault
#: passphrases.
#:
#: The narrowing is recorded in `docs/upgrading-to-2.0.md`: an operator EE that
#: cannot run as 1000 is refused at the startup probe rather than meeting this
#: as an unreadable file mid-playbook.
#:
#: **One producer, because there are two kinds of file that need it** and they
#: are written by different modules. The credential phase writes the key
#: material; the inventory renderer writes a document that carries
#: `ansible_password` and `ansible_become_password` as VARIABLES, because
#: ansible has no `ansible_password_file` and a connection password can only be
#: a variable. Two copies of this number is how one of them ends up `0644`.
CREDENTIAL_FILE_MODE = 0o600

MACHINE_CATEGORY = "ansible_machine_auth"
VAULT_CATEGORY = "ansible_vault_auth"

#: What a placeholder looks like: `${terrapod:<name>.<field>}`.
#:
#: **An ordinary YAML string, deliberately, and NOT a `!terrapod` tag.** The tag
#: form reads better -- it is how `!vault` looks -- and it does not work.
#: Measured: `ansible-inventory` cannot parse an inventory containing an unknown
#: tag, and it fails SILENTLY, exiting 0 having resolved ZERO hosts. `!vault`
#: works only because ansible registers a constructor for it in its own loader,
#: and teaching ansible ours would mean shipping a plugin into the operator's own
#: execution environment -- the thing #2154 exists to avoid.
#:
#: A `${...}` string also parses in a GENERIC YAML parser, which a tag does not:
#: `yaml.safe_load` refuses `!vault` too. So this leaves an inventory no less
#: parseable than it already was, which was the requirement. Unquoted is fine in
#: YAML (a plain scalar may begin with `$`) and in an INI inventory, both
#: measured.
PLACEHOLDER = re.compile(r"\$\{terrapod:([A-Za-z0-9_][A-Za-z0-9_.-]*)\.([a-z_]+)\}")

#: A credential's `key` is its NAME, and a name is interpolated into a
#: placeholder whose own grammar is the delimiter -- so the character set is the
#: placeholder's, not an inventory name's.
CREDENTIAL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

#: Envelope field -> nothing; the field names are the placeholder's second half.
#: `MACHINE_FILE_FIELDS` is the one place that says a field's value is delivered
#: as a FILE, so a placeholder naming it substitutes that file's path.
#: `known_hosts` is here and deliberately NOT in `MACHINE_SECRET_FIELDS`: a
#: host's PUBLIC key is public, so registering it with the log redactor would
#: corrupt the run's log -- replacing a value an operator needs to read when a
#: host key does not match -- in exchange for hiding nothing. It is a file
#: because that is what `-o UserKnownHostsFile=` takes, which is the whole
#: reason the field exists: without it, the only route past an unknown host is
#: to disable verification, and a correct default nobody can satisfy becomes a
#: default everybody turns off (#2318).
MACHINE_FILE_FIELDS: frozenset[str] = frozenset({"private_key", "known_hosts"})

#: The fields whose value a placeholder substitutes verbatim.
#:
#: SECRETS ONLY, and that is the whole design rule (#2413 F9): a credential
#: holds what must not reach the resolved inventory, and everything else is an
#: inventory variable. `user`, `become`, `become_method` and `become_user`
#: used to be here and are gone, because they failed that test on their own
#: terms -- none is secret, none is a file, and substituting one puts a plain
#: value into the resolved inventory, which is the opposite of what
#: `machineHint` promises this category does.
#:
#: The placeholder is PULL-based, so the inventory author writes
#: `${terrapod:web-key.user}` themselves. For a non-secret that is strictly
#: worse than writing `ansible_user: deploy` in the same file: identical
#: visibility, one more indirection, and a second home for the value. Become
#: settings also differ legitimately per host and per group, while a credential
#: is referenced by name from many hosts, so holding them here pushed an
#: inventory-shaped concern into a shared object.
#:
#: `become_password` STAYS even though its siblings went: it is a secret, and
#: ansible has no `ansible_become_password_file`, so it can only ever be a
#: variable -- which is exactly why it needs the log redactor.
MACHINE_VALUE_FIELDS: frozenset[str] = frozenset({"password", "become_password"})

MACHINE_FIELDS = MACHINE_FILE_FIELDS | MACHINE_VALUE_FIELDS
VAULT_FIELDS = frozenset({"password"})

#: Fields this category USED to carry, refused on write and TOLERATED on read.
#:
#: The asymmetry is the point, and it is this project's standing rule: validate
#: at the write boundary, tolerate on the read side. Narrowing the set without it
#: silently disarmed every credential already written with one of these -- the
#: envelope was refused, `delivery_entries` dropped the whole credential, the
#: runner reported "no credentials were declared, which is an ordinary
#: configure", and every host came back unreachable naming neither the
#: credential nor the field. `validate_envelope`'s own comment warns against
#: exactly that outcome, which is how a guard came to cause the thing it was
#: written to prevent.
#:
#: So a stored envelope carrying one of these still delivers its SECRETS, and a
#: new write naming one is still a 422. A placeholder referring to a retired
#: field does not silently blank: `configure_inventory._refuse_unresolved`
#: refuses the run and names every reference it could not resolve, which is the
#: loud, actionable failure an operator needs in order to move the setting into
#: the inventory where it belongs.
#:
#: Not a data migration, deliberately. `variables.value` is `EncryptedText`, so
#: stripping stored envelopes means decrypting and re-encrypting inside Alembic
#: -- and this project keeps key handling in the API precisely because a
#: migration reconstructing it differs in ways that stay invisible until
#: something stops validating. Tolerating on read needs no key and no downtime,
#: and the form repairs a row the next time anyone edits it.
RETIRED_MACHINE_FIELDS: frozenset[str] = frozenset(
    {"user", "become", "become_method", "become_user"}
)

#: The fields whose value is secret, so the runner can register them with the
#: log redactor without re-deriving which is which.
MACHINE_SECRET_FIELDS = frozenset({"private_key", "password", "become_password"})


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


def validate_envelope(
    category: str, key: str, value: str, *, tolerate_retired: bool = False
) -> dict[str, Any]:
    """Return the parsed envelope for a credential write, or raise.

    Args:
        category: `ansible_machine_auth` or `ansible_vault_auth`. Any other
            category returns `{}` -- this is called from a shared write guard
            that sees every category.
        key: the credential's NAME -- what an inventory refers to it by, as
            `${terrapod:<name>.<field>}`. For a vault password the name is also
            the vault ID label. Nothing here is about WHERE the credential is
            used; that is said in the inventory.
        value: the JSON envelope as written.

    Raises:
        AnsibleAuthError: the name or the envelope cannot be used. The message
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
    if tolerate_retired and category == MACHINE_CATEGORY:
        # Dropped BEFORE the unknown check, so a legacy envelope reads as the
        # narrowed one it would be if written today. The caller gets the stripped
        # dict back and delivers that, so nothing downstream -- the per-run
        # Secret, the credential files, the redactor registry -- ever sees a
        # field it has no home for.
        parsed = {k: v for k, v in parsed.items() if k not in RETIRED_MACHINE_FIELDS}
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
    _validate_name(category, key)

    if category == VAULT_CATEGORY:
        if not parsed.get("password"):
            raise AnsibleAuthError(
                "an Ansible Vault credential needs a non-empty 'password'; without one "
                "there is nothing to decrypt with and the configure would fail mid-play"
            )
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

    Nothing is DROPPED here, and that is the whole of #2425 §0.3. There are two
    classes of stored envelope this can meet, and they are different problems
    with different right answers:

    **It carries a field the platform has since retired.** `user`, `become`,
    `become_method` and `become_user` were valid when 2.0.0-alpha.1 shipped and
    are not fields any more. The retired names are stripped and the usable
    remainder is delivered, so an alpha credential goes on working and needs no
    migration and no operator action. It is re-serialised only in that case, so
    an ordinary credential is still carried byte-for-byte as stored.

    **It is unreadable, or usable for nothing.** Not JSON at all; an object with
    no `private_key` and no `password`; a field whose value is not a string. The
    entry is delivered UNCHANGED so that the runner refuses the configure and
    names it: `phases/ansible_auth.run` collects every such failure and raises
    `AnsibleAuthUnavailable` with the category, the key and the reason.

    This used to `continue` for both, with the stated reason that a bad envelope
    here meant a row predating the write guard, so failing the run would make a
    historical row un-runnable rather than merely inert. The narrowing is what
    falsified that: the commonest bad envelope became a perfectly good credential
    with one retired field, dropping it disarmed the fleet, and "merely inert"
    read to the operator as a configure whose every host was unreachable, naming
    neither the credential nor the field. The first class above removes the
    reason the drop existed; the second is the `declared and unusable is fatal`
    contract this module already documents and the runner already implements
    (#1442).

    ABSENT credentials remain not an error: that is the ordinary state of a
    configure against hosts the EE can already reach.
    """
    out: list[dict[str, str]] = []
    for v in resolved:
        category = getattr(v, "category", "")
        if not is_ansible_auth(category):
            continue
        key = getattr(v, "key", "")
        raw = getattr(v, "value", "")
        try:
            envelope = validate_envelope(category, key, raw, tolerate_retired=True)
        except AnsibleAuthError as exc:
            # DELIVERED ANYWAY, not dropped (#2425 §0.3).
            #
            # This used to `continue`, on the stated grounds that a bad envelope
            # here meant a row written before the guard existed. That premise
            # stopped being true the moment the field set narrowed, and the cost
            # was the exact failure the validator's own comment warns about: the
            # credential vanished, the runner reported "no credentials were
            # declared, which is an ordinary configure", and the operator's only
            # signal was a wholly unreachable fleet naming nothing.
            #
            # The runner already has the right behaviour for this and the module
            # docstring already claims it -- a credential DECLARED and unusable
            # is fatal, with every failure named at once. So passing the entry
            # through unchanged is what makes that true: the runner's
            # `phases/ansible_auth.run` collects it into `failures` and raises
            # `AnsibleAuthUnavailable` naming the category, the key and the
            # reason. Unchanged and not the stripped form, so what it reports is
            # the real problem.
            #
            # And NOTHING is logged here, deliberately. This module is
            # stdlib-only because it ships in the runner image, where a
            # module-level import of anything heavier breaks every run
            # (`TestItIsPure`) -- so there is no logger to reach for. Nor is one
            # wanted: the run error above reaches the operator in the run log
            # they are already reading, where an API-pod warning would not.
            #
            # ABSENT credentials remain not an error -- that is the ordinary
            # state of a configure against hosts the EE can already reach, and
            # nothing here changes it.
            del exc  # named for the reader; the runner reports the real reason
            out.append({"category": category, "key": key, "value": raw})
            continue
        # Re-serialised ONLY when something was stripped, so an ordinary
        # credential is delivered byte-for-byte as stored.
        stripped = json.dumps(envelope) if set(envelope) != set(json.loads(raw)) else raw
        out.append({"category": category, "key": key, "value": stripped})
    return out


def _validate_name(category: str, key: str) -> None:
    """A credential's name, which is also how a placeholder refers to it."""
    if CREDENTIAL_NAME.match(key or ""):
        return
    where = (
        "it is interpolated into ANSIBLE_VAULT_IDENTITY_LIST, whose syntax is "
        "'label@file,label@file', so a ',' or '@' in it produces a different list "
        "rather than a rejected one"
        if category == VAULT_CATEGORY
        else "an inventory refers to the credential by it, as "
        "${terrapod:<name>.<field>}, so a character the placeholder's own grammar "
        "uses would make the reference unresolvable"
    )
    raise AnsibleAuthError(
        f"credential name {key!r} must start with a letter, digit or underscore and "
        f"contain only those plus '.' and '-': {where}."
    )


def placeholder_for(name: str, field: str) -> str:
    """The placeholder an inventory writes to refer to one field of one credential.

    One producer, so the docs, the tests and any future validator cannot drift
    from `PLACEHOLDER` -- a reference that does not match the pattern is not a
    rejected reference, it is a literal string handed to ssh as a key path.
    """
    return f"${{terrapod:{name}.{field}}}"


def substitute(value: Any, resolved: dict[str, str]) -> Any:
    """Replace every placeholder in `value`, recursively. Non-strings pass through.

    `resolved` maps `"<name>.<field>"` to what it stands for: the path of the
    file the runner wrote, for a field delivered as a file, or the value itself.

    An UNKNOWN placeholder is left in place rather than blanked, which is what
    lets the caller refuse the render naming it. Substituting an empty string
    would hand ansible a connection with no key and no error -- the failure then
    arrives as an unreachable host, naming neither the credential nor the
    reference.
    """
    if isinstance(value, str):
        return PLACEHOLDER.sub(
            lambda m: resolved.get(f"{m.group(1)}.{m.group(2)}", m.group(0)), value
        )
    if isinstance(value, dict):
        return {k: substitute(v, resolved) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, resolved) for v in value]
    return value


def unresolved(value: Any) -> list[str]:
    """Every placeholder still present, deduplicated and sorted.

    The caller refuses on a non-empty result. Reported as the whole reference
    rather than a count, because the fix is to correct that string or add that
    credential and a number tells the operator neither.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(m.group(0) for m in PLACEHOLDER.finditer(node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(value)
    return sorted(found)
