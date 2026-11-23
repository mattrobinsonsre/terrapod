"""What ansible will accept as an inventory name, and what it will not (#1967).

Pure functions over plain values: no database, no network, no ansible. The
database side is `inventory_service`.

## Terrapod no longer implements a merge

An earlier version of this module merged sources itself -- union the hosts, union
the group memberships, let a later source win a conflicting variable -- having
measured those rules against ansible-core 2.18.3. The rules were right, and
reimplementing them was still the wrong shape: ansible already performs the
merge, the precedence, the group DAG, the derivation of `all` and `ungrouped`,
and the expansion of `--limit`. Terrapod renders the input and reads the output.

What stays here is the part ansible will NOT do for us: refusing a name at the
boundary, before it is stored. Ansible is laxer than this and warns about the
rest, but a name it warns about is one an operator cannot reliably use in a
`--limit` pattern or a `group_vars` filename -- so the refusal belongs at the
write, where it names the offending value, rather than at the point a playbook
mysteriously targets nothing.
"""

from __future__ import annotations

import re
from typing import Any

#: Group names ansible derives rather than accepts.
#:
#: `all` holds every host and `ungrouped` holds the hosts in no other group, so
#: a source declaring either would be asking for a group whose membership it
#: does not control. `all` is the sharper case: the rendered inventory document
#: is ROOTED at `all:`, so a declared group of that name would collide with the
#: document's own structure rather than merely duplicating a derived one.
#:
#: Variables under `all` are an ordinary thing to want, and they are a real
#: ansible structure (`group_vars/all`). They live in `inventory_global_vars`,
#: parented on the workspace -- which is the consequence of this refusal rather
#: than a way around it.
DERIVED_GROUPS = frozenset({"all", "ungrouped"})

#: What ansible will accept as a group name without warning.
_GROUP_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Characters that make a host name unusable as a target rather than merely
#: ugly. `,` and `:` separate patterns in `--limit`; `!`, `&` and `~` are the
#: exclusion, intersection and regex operators. A host called `!web` can never
#: be selected and, worse, silently *excludes* `web` from any pattern naming it.
#: Whitespace is refused for the same reason -- `--limit` splits on it.
_HOST_NAME_FORBIDDEN = frozenset(",:!&~")


class InventoryValidationError(ValueError):
    """A host, group or variable name cannot be used in an ansible inventory.

    Raised by the `validate_*` functions and translated to HTTP 422 by the
    router. The message names the offending value and the reason, because
    "invalid group name" on its own sends an operator to the wrong place.
    """


def validate_group_name(name: str) -> str:
    """Return `name` if ansible can use it as a group, else raise."""
    if not isinstance(name, str) or not name:
        raise InventoryValidationError("a group name must be a non-empty string")
    if name in DERIVED_GROUPS:
        raise InventoryValidationError(
            f"{name!r} is derived by ansible, not declared: 'all' holds every host and "
            f"'ungrouped' holds the hosts in no other group, so a source cannot control "
            f"either one's membership. For variables that apply to every host, set them "
            f"on the inventory itself rather than on a group -- that is ansible's "
            f"'group_vars/all' and Terrapod stores it as such."
        )
    if not _GROUP_NAME.match(name):
        raise InventoryValidationError(
            f"group name {name!r} must start with a letter or underscore and contain only "
            f"letters, digits and underscores. Ansible tolerates more than that but warns, "
            f"and a name it warns about cannot be used reliably in a --limit pattern or a "
            f"group_vars filename."
        )
    return name


def validate_host_name(name: str) -> str:
    """Return `name` if it can be targeted, else raise.

    The forbidden set is not cosmetic: every character in it means something to
    `--limit`, so a name containing one is either unselectable or actively
    changes which other hosts a pattern selects.
    """
    if not isinstance(name, str) or not name:
        raise InventoryValidationError("a host name must be a non-empty string")
    if name.strip() != name or any(c.isspace() for c in name):
        raise InventoryValidationError(
            f"host name {name!r} contains whitespace, which --limit splits on, so the host "
            f"could not be targeted."
        )
    bad = sorted(set(name) & _HOST_NAME_FORBIDDEN)
    if bad:
        raise InventoryValidationError(
            f"host name {name!r} contains {''.join(bad)!r}. Those are --limit's own "
            f"operators and separators: ',' and ':' separate patterns, '!' excludes, '&' "
            f"intersects and '~' introduces a regex. A host named with one of them cannot "
            f"be selected, and a leading '!' would silently exclude the host it names."
        )
    return name


def validate_var_key(key: Any) -> str:
    """Return `key` if it can be used as an ansible variable name, else raise.

    **Deliberately laxer than the group rule.** Ansible stores a variable whose
    name is not an identifier and only warns that it is unreachable as
    `{{ name }}` -- it is still readable via `hostvars['h']['odd-name']`, which
    some roles do on purpose. So the key only has to be a non-empty string;
    refusing more would block working configurations to prevent a warning.
    """
    if not isinstance(key, str) or not key:
        raise InventoryValidationError(f"a variable name must be a non-empty string; got {key!r}")
    if key.strip() != key:
        raise InventoryValidationError(
            f"variable name {key!r} has leading or trailing whitespace, which is almost "
            f"certainly a mistake and is invisible in every surface that displays it."
        )
    return key


#: The key types whose BLOB carries a different algorithm name than the type does.
#:
#: `rsa-sha2-256` and `rsa-sha2-512` are SIGNATURE algorithms; the key they sign
#: with is an RSA key whose own embedded name is `ssh-rsa`. Measured against
#: `x/crypto/ssh`: asking a server for either yields a key whose `.Type()` is
#: `ssh-rsa`. So a validator comparing the blob's embedded name to the declared type
#: has to know that, or it would refuse every correct RSA entry.
_BLOB_NAME_ALIASES: dict[str, str] = {
    "rsa-sha2-256": "ssh-rsa",
    "rsa-sha2-512": "ssh-rsa",
}


def validate_host_key_type(value: Any) -> str:
    """Return `value` if it names an SSH host-key algorithm, else raise.

    Validated against a closed set rather than accepted as free text, and the
    reason is the direction the failure takes: a typo'd algorithm in a declared
    TRUST ANCHOR silently never matches anything, and the symptom -- a host whose
    key is refused -- reads as "the host rotated its key" rather than as a
    configuration error.
    """
    from terrapod.db.models import SSH_HOST_KEY_TYPES

    if not isinstance(value, str) or not value:
        raise InventoryValidationError(f"a host key type must be a non-empty string; got {value!r}")
    if value not in SSH_HOST_KEY_TYPES:
        raise InventoryValidationError(
            f"host key type {value!r} is not one Terrapod recognises. Expected one of "
            f"{', '.join(sorted(SSH_HOST_KEY_TYPES))}, spelled as OpenSSH spells it in "
            f"the second field of a known_hosts line."
        )
    return value


def validate_host_public_key(value: Any, *, key_type: str) -> str:
    """Return `value` if it is the base64 blob of a `key_type` public key, else raise.

    Three refusals, each for a mistake an operator actually makes.

    **A whole known_hosts line.** The commonest paste is
    `host ssh-ed25519 AAAA...`, all three fields. Stored as the blob it would
    render into a line with the host and algorithm doubled, which no client
    matches -- and the symptom is an unknown host, not a parse error.

    **A blob that is not base64.** Caught here rather than at render time, where it
    would reach a file ansible reads and fail inside the configure.

    **A blob whose embedded algorithm disagrees with `key_type`.** An SSH public key
    is a length-prefixed algorithm name followed by its parameters, so the blob says
    what it is. Declaring an ed25519 key as `ssh-rsa` is otherwise accepted and then
    never matches anything.
    """
    import base64
    import struct

    if not isinstance(value, str) or not value:
        raise InventoryValidationError(
            f"a host public key must be a non-empty string; got {value!r}"
        )
    if value.strip() != value:
        raise InventoryValidationError(
            "a host public key has leading or trailing whitespace, which is invisible "
            "in every surface that displays it."
        )
    if " " in value:
        raise InventoryValidationError(
            "a host public key must be the base64 blob ALONE -- the third field of a "
            "known_hosts line. This value contains a space, so it looks like a whole "
            "line (`<host> <type> <blob>`); supply the host through the inventory and "
            "the type through `key_type`, and give only the blob here."
        )
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise InventoryValidationError(
            f"a host public key must be base64; this value is not ({exc})."
        ) from exc
    # The wire format: a 4-byte big-endian length, then that many bytes of
    # algorithm name. Anything shorter is not a key at all.
    if len(raw) < 4:
        raise InventoryValidationError(
            "a host public key decoded to fewer than four bytes, so it carries no "
            "algorithm name and is not an SSH public key."
        )
    (name_len,) = struct.unpack(">I", raw[:4])
    if name_len <= 0 or len(raw) < 4 + name_len:
        raise InventoryValidationError(
            "a host public key's leading length field does not match its contents, so "
            "it is not an SSH public key."
        )
    embedded = raw[4 : 4 + name_len].decode("ascii", errors="replace")
    expected = _BLOB_NAME_ALIASES.get(key_type, key_type)
    if embedded != expected:
        raise InventoryValidationError(
            f"this public key is a {embedded!r} key but it is declared as "
            f"{key_type!r}. The blob itself names its algorithm, so one of the two is "
            f"wrong -- a key declared under the wrong type is stored happily and then "
            f"never matches the host."
        )
    return value


def validate_host_key_marker(value: Any) -> str | None:
    """Return `value` if it is a known_hosts marker or None, else raise.

    `None` and `""` both mean "an ordinary entry" and both normalise to None, so a
    caller clearing the field does not have to know which one the surface uses.
    """
    from terrapod.db.models import SSH_HOST_KEY_MARKERS

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InventoryValidationError(f"a host key marker must be a string or null; got {value!r}")
    if value not in SSH_HOST_KEY_MARKERS:
        raise InventoryValidationError(
            f"host key marker {value!r} is not one OpenSSH defines. Expected one of "
            f"{', '.join(sorted(SSH_HOST_KEY_MARKERS))}, without the leading '@' -- "
            f"Terrapod adds that when it renders the line."
        )
    return value
