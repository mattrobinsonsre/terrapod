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


def validate_machine_scope(name: str) -> str:
    """Return `name` if a machine credential (#1933) can be scoped to it, else raise.

    Three levels, because a real estate needs all three: a credential may belong
    to **one host**, to a **group** of them, or to the **whole inventory** as its
    default. So this is the union of the two name rules plus `all`, sharing both
    rather than restating either.

    `all` is admitted here and refused by `validate_group_name` for the same
    reason in both directions -- it is derived, so nothing may declare its
    MEMBERSHIP, while variables on it are ansible's own `group_vars/all`, which
    is exactly what an inventory-wide default is.

    **The name is validated for SHAPE here and for existence on the runner**,
    deliberately. A credential is commonly written before the host it names --
    Terraform declares both in one apply and the order is the practitioner's --
    so refusing an unknown name at the write boundary would make the two
    resources order-dependent. The runner refuses a scope that matches nothing
    in the run's resolved inventory, where the host set is known.
    """
    if not isinstance(name, str) or not name:
        raise InventoryValidationError(
            "a machine credential's scope must be a non-empty string: a host, a group, "
            "or 'all' for the whole inventory"
        )
    if name == "all":
        return name
    if name == "ungrouped":
        # Admitting it would promise scoping to a set the operator cannot see or
        # control, and whose membership changes whenever any group changes.
        raise InventoryValidationError(
            "'ungrouped' cannot scope a machine credential: it is derived from which "
            "hosts are in no other group, so the set changes whenever any group does. "
            "Scope the credential to 'all', to a group, or to the host itself."
        )
    # A group name is the stricter rule; a host name allows dots and dashes. The
    # scope is whichever it turns out to be in the inventory, so accepting the
    # union is correct and the runner resolves which.
    try:
        return validate_group_name(name)
    except InventoryValidationError:
        pass
    try:
        return validate_host_name(name)
    except InventoryValidationError as exc:
        raise InventoryValidationError(
            f"machine credential scope {name!r} is not usable as a host name, a group "
            f"name, or 'all'. {exc}"
        ) from exc


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
