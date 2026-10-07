"""Merging inventory sources into one host/group set (#1967).

Pure functions over plain values: no database, no network, no ansible. The
database side is `inventory_service`; this module is the part that decides what
the resolved inventory *is*, so it can be tested against the measurements
recorded on #1967 without standing anything up.

**Terrapod implements no merge scheme of its own.** The rules below are
ansible's own multiple-`-i` behaviour, measured on ansible-core 2.18.3 over a
YAML file, an INI file and an executable script simultaneously:

* hosts **union** across sources;
* group membership **unions** -- a group named by two sources holds the hosts
  from both;
* a conflicting host variable goes to the **later** source, proven in both
  orders (`-i a -i b` gave `from_B`, `-i b -i a` gave `from_A`);
* a non-conflicting variable from an earlier source survives alongside.

So an operator's existing mental model transfers exactly, and the ordered source
list on an inventory is an `-i` ordering and nothing more. Implementing the
precedence rule now, while only one source kind exists and cannot exercise it,
is deliberate: the second source kind (#1929, #1969) must not require rewriting
this.

## The normalised shape, and why it is not ansible's

`ResolvedInventory` holds **every** host explicitly, including hosts with no
variables at all. `ansible-inventory --list` does not: its `_meta.hostvars`
**omits a host that has no vars** -- measured, a host appearing only in a group
was absent from `_meta.hostvars` entirely. Code that enumerates the host set
from `_meta.hostvars` therefore loses hosts silently, which is the shape of bug
that empties a target set.

Storing the explicit map means that trap cannot apply to anything downstream of
here, and `to_ansible_inventory` renders ansible's shape on demand instead. One
source of truth, rendered two ways, rather than two representations to keep in
step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Group names ansible derives rather than accepts. `all` holds every host and
#: `ungrouped` holds the hosts in no other group, so both are computed by
#: `to_ansible_inventory` and refused as declared names -- a source declaring
#: `all` would be asking for a group whose membership it does not control.
DERIVED_GROUPS = frozenset({"all", "ungrouped"})

#: What ansible will accept as a group name without warning. Ansible is laxer
#: than this in practice and warns about the rest, but a name it warns about is
#: one an operator cannot reliably use in a `--limit` pattern or a `group_vars`
#: filename, so it is refused at the boundary instead of stored and regretted.
_GROUP_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Characters that make a host name unusable as a target rather than merely
#: ugly. `,` and `:` separate patterns in `--limit`; `!`, `&` and `~` are the
#: exclusion, intersection and regex operators. A host called `!web` can never
#: be selected and, worse, silently *excludes* `web` from any pattern naming it.
#: Whitespace is refused for the same reason -- `--limit` splits on it.
_HOST_NAME_FORBIDDEN = frozenset(",:!&~")


class InventoryValidationError(ValueError):
    """A host, group or variable name cannot be used in an ansible inventory.

    Raised by `validate_*` below and translated to HTTP 422 by the router. The
    message names the offending value and the reason, because "invalid group
    name" on its own sends an operator to the wrong place.
    """


@dataclass(frozen=True)
class HostEntry:
    """One host as a single source describes it."""

    name: str
    #: Ansible host variables. `ansible_host`, `ansible_user` and friends live
    #: here like any other -- Terrapod gives them no special meaning, because
    #: ansible does not either.
    vars: dict[str, Any] = field(default_factory=dict)
    #: Explicit group membership. Never `all` or `ungrouped`; see DERIVED_GROUPS.
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceResolution:
    """What one source contributed, before merging.

    `label` is for diagnostics only -- which source a host or a winning
    variable came from, so a surprising resolved value can be traced back to the
    source that set it rather than guessed at.
    """

    label: str
    hosts: tuple[HostEntry, ...] = ()


@dataclass(frozen=True)
class ResolvedInventory:
    """The merged result. Every host appears in `hosts`, vars or not."""

    #: host name -> merged variables. Exhaustive: this is the host set.
    hosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: group name -> sorted member host names. Declared groups only.
    groups: dict[str, list[str]] = field(default_factory=dict)
    #: host name -> the labels of the sources that named it, in order. Kept so
    #: a UI can answer "where did this host come from"; not part of the
    #: targeting basis.
    provenance: dict[str, list[str]] = field(default_factory=dict)

    @property
    def host_count(self) -> int:
        return len(self.hosts)

    @property
    def group_count(self) -> int:
        return len(self.groups)


def validate_group_name(name: str) -> str:
    """Return `name` if ansible can use it as a group, else raise."""
    if not isinstance(name, str) or not name:
        raise InventoryValidationError("a group name must be a non-empty string")
    if name in DERIVED_GROUPS:
        raise InventoryValidationError(
            f"{name!r} is derived by ansible, not declared: 'all' holds every host and "
            f"'ungrouped' holds the hosts in no other group, so a source cannot control "
            f"either one's membership. Name a group of your own instead."
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


def validate_declared_vars(host_vars: dict[str, Any]) -> dict[str, Any]:
    """Return `host_vars` if every key and value is usable, else raise.

    **Names are deliberately laxer than the group rule.** Ansible stores a
    variable whose name is not an identifier and only warns that it is
    unreachable as `{{ name }}` -- it is still readable via
    `hostvars['h']['odd-name']`, which some roles do on purpose. So the key only
    has to be a non-empty string; refusing more would block working
    configurations to prevent a warning.

    **Values on this path must be strings**, and that is a narrower rule than
    ansible's own, taken on purpose:

    * a declared item is the surface the managing Terraform owns, and a
      Terraform map is `map(string)` -- the provider has no shape for a nested
      object;
    * every Go consumer decodes vars into `map[string]string`, and
      `encoding/json` fails the **whole** unmarshal on one non-string value. So
      storing `{"role": "frontend", "port": 8080}` would make the SDK, the
      provider and the MCP tools report **no variables at all** for that host --
      not the one odd value, all of them -- with nothing anywhere saying why.

    Refusing at the write turns that into a 422 naming the key. Richer values
    are not lost to the platform: a resolved **snapshot** carries whatever
    ansible produced, and a future file-based source carries `group_vars` and
    `host_vars` natively. This rule is only about the flat surface Terraform
    declares, which is why the snapshot path deliberately does not call it.
    """
    if not isinstance(host_vars, dict):
        raise InventoryValidationError("host variables must be a mapping")
    for key, value in host_vars.items():
        if not isinstance(key, str) or not key:
            raise InventoryValidationError(
                f"host variable names must be non-empty strings; got {key!r}"
            )
        if not isinstance(value, str):
            raise InventoryValidationError(
                f"the declared value for host variable {key!r} must be a string; got "
                f"{type(value).__name__}. A declared item is the flat surface Terraform "
                f"owns -- every client reads these as strings, and one non-string value "
                f"hides the whole set rather than the one odd entry. Put a list, number "
                f"or nested object in a group_vars or host_vars source instead."
            )
    return host_vars


def merge(resolutions: list[SourceResolution]) -> ResolvedInventory:
    """Merge per-source resolutions in `-i` order into one host/group set.

    `resolutions` is ordered: index 0 is the first `-i`, so a host variable set
    by a later entry wins. See the module docstring for the measurements.
    """
    hosts: dict[str, dict[str, Any]] = {}
    group_members: dict[str, set[str]] = {}
    provenance: dict[str, list[str]] = {}

    for resolution in resolutions:
        for host in resolution.hosts:
            # `update` rather than replace: a variable only one source sets
            # survives a later source that sets different ones, which is the
            # measured "only_in_A survives alongside from_B" case.
            hosts.setdefault(host.name, {}).update(host.vars)

            labels = provenance.setdefault(host.name, [])
            if resolution.label not in labels:
                labels.append(resolution.label)

            for group in host.groups:
                group_members.setdefault(group, set()).add(host.name)

    return ResolvedInventory(
        hosts=hosts,
        # Sorted so the snapshot is byte-stable for the same inputs: an
        # unordered set would make two identical resolutions compare unequal and
        # a diff between snapshots unreadable.
        groups={name: sorted(members) for name, members in sorted(group_members.items())},
        provenance=provenance,
    )


def to_ansible_inventory(resolved: ResolvedInventory) -> dict[str, Any]:
    """Render `resolved` in the shape `ansible-inventory --list` produces.

    This is what a configure hands to ansible (#1972), so it has to be the real
    shape rather than a convenient one: `_meta.hostvars`, an `all` group whose
    `children` name every other group, and `ungrouped` for the hosts no declared
    group claims.

    Unlike ansible's own output this includes **every** host in
    `_meta.hostvars`, with an empty mapping where a host has no variables. That
    is valid inventory JSON and it removes the omission trap described in the
    module docstring for anything reading our output.
    """
    grouped: set[str] = {host for members in resolved.groups.values() for host in members}
    ungrouped = sorted(set(resolved.hosts) - grouped)

    out: dict[str, Any] = {
        "_meta": {"hostvars": {name: dict(v) for name, v in sorted(resolved.hosts.items())}}
    }

    children = sorted(resolved.groups)
    if ungrouped:
        children.append("ungrouped")
        out["ungrouped"] = {"hosts": ungrouped}

    # `all` is emitted even when empty: ansible's own output always carries it,
    # and a consumer that reads `all.children` should not have to handle it
    # being absent on an inventory that happens to have no hosts yet.
    out["all"] = {"children": children}

    for name, members in resolved.groups.items():
        out[name] = {"hosts": list(members)}

    return out


def limit_matches(resolved: ResolvedInventory, pattern: str) -> list[str]:
    """Which hosts a `--limit` pattern selects, for preview only.

    **Not a reimplementation of ansible's pattern language, and not the
    targeting basis.** It covers the forms an operator writes in a limit --
    names, groups, `all`/`*`, comma or colon separated terms, `!` exclusion and
    `&` intersection -- so the UI can answer "what would this configure target"
    before a Job exists. Anything it is unsure of, it declines rather than
    guesses: a `~regex` term raises, because quietly matching nothing would
    show an empty target set for a limit that ansible would have expanded.

    The authoritative expansion is always `ansible-inventory --list --limit`
    taken in the runner at the start of a configure, which is what the
    `InventoryVersion` snapshot records (#1967).
    """
    if not pattern or not pattern.strip():
        return sorted(resolved.hosts)

    selected: set[str] = set()
    first_term = True

    for raw in re.split(r"[,:]", pattern):
        term = raw.strip()
        if not term:
            continue

        if term.startswith("~"):
            raise InventoryValidationError(
                f"the limit term {term!r} is a regular expression, which this preview does "
                f"not expand. Ansible will apply it at run time; showing an empty target "
                f"set here would misrepresent that."
            )

        mode = "add"
        if term[0] == "!":
            mode, term = "exclude", term[1:]
        elif term[0] == "&":
            mode, term = "intersect", term[1:]

        if term in ("all", "*"):
            matched = set(resolved.hosts)
        elif term in resolved.groups:
            matched = set(resolved.groups[term])
        elif term in resolved.hosts:
            matched = {term}
        elif "*" in term:
            # Ansible's glob, which is fnmatch over host and group names.
            import fnmatch

            matched = {h for h in resolved.hosts if fnmatch.fnmatchcase(h, term)}
            for group, members in resolved.groups.items():
                if fnmatch.fnmatchcase(group, term):
                    matched |= set(members)
        else:
            matched = set()

        if mode == "exclude":
            selected -= matched
        elif mode == "intersect":
            selected &= matched
        else:
            # An inclusion is a union, except that the first term starts from
            # nothing rather than from the whole inventory.
            selected = matched if first_term else selected | matched

        first_term = False

    return sorted(selected)
