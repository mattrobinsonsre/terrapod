"""Where each of an inventory variable's values came from (#2413 F8).

The Resolved view flattens hierarchical data: a name, then every variable in
one monospace run, space-joined as `k=v` pairs — which is **ambiguous** the
moment a value contains a space, and `ansible_ssh_common_args` routinely does.
Merged variables carry no provenance at all, which is the whole reason a
resolved view exists.

This module supplies the provenance half. The structure half needs nothing: the
resolved payload already carries `hosts`, `groups` and `group-children`.

## Ansible attributes the variables, not us

`ansible-inventory --graph --vars` reports **the level each variable is set at**
— measured against core 2.21.6 in the shipped image:

    @all:
      |--@prod:
      |  |--web1
      |  |  |--{shared = host-wins}     <- the host line is EFFECTIVE
      |  |--{shared = group-wins}       <- set AT prod
      |--{shared = all-loses}           <- set AT all

So the chain is visible in full and the winner is the deepest level carrying the
key, **by ansible's own precedence**. Nothing here re-derives a merge, which
matters more than it sounds: a second precedence implementation would diverge
from ansible silently, and the resolved view exists precisely to be trusted.

Two measurements decided this shape rather than the two obvious alternatives:

* **`--list` carries NO group vars.** Its output has `children` and `hosts` per
  group and no `vars` key at all, so the JSON cannot attribute anything. That is
  why a text parser is unavoidable rather than lazy.
* **`--graph` ignores `--yaml` and `--toml`.** Both emit the same text tree, so
  there is no structured form to ask for.

## What the parser is and is not for

Its job is **structural**: read which level each key sits on. It never decides a
winner, so a value it cannot compare costs an attribution rather than producing
a wrong one — `unattributable` is a real answer and the view says so.
"""

from __future__ import annotations

import re
from typing import Any

#: A line in `--graph --vars` output: a run of whitespace and pipes, then
#: `|--`, then the content. Depth is the number of pipes in that leading run,
#: so `@all:` is 0, `  |--@prod:` is 1 and `  |  |--web1` is 2.
#:
#: Anchored, and the leading class excludes `-` deliberately: a value containing
#: `|--` would otherwise be read as a new level. A content line is matched
#: separately below, so a stray match here produces no attribution rather than a
#: wrong one.
_LINE = re.compile(r"^((?:[ \t|])*)\|--(.*)$")

#: `{key = value}`. Non-greedy on the key so a value containing ` = ` keeps the
#: whole remainder, which is what ansible prints.
_VAR = re.compile(r"^\{(.+?) = (.*)\}$")

#: What an inline-vaulted value reads as in the levels view (#2506).
#:
#: The API holds no vault secret, and `--graph --vars` cannot print a value it
#: cannot decrypt -- so `inventory_vault` substitutes this string into the
#: scratch copy before the attribution pass. It lives HERE, with the comparison
#: that has to recognise it, because a second copy of the literal is how the
#: substitution and the comparison come to disagree and every vaulted variable
#: silently loses its attribution.
VAULTED_PLACEHOLDER = "<vaulted>"

#: How `--list` reports a value it did not decrypt: a one-key mapping holding
#: the ciphertext. Ansible's own wire shape, not ours.
_VAULT_MARKER = "__ansible_vault"


def _is_vault_ciphertext(value: Any) -> bool:
    """Is this `--list` value an undecrypted vault scalar?"""
    return isinstance(value, dict) and set(value) == {_VAULT_MARKER}


def parse_graph(text: str) -> dict[str, dict[str, str]]:
    """Map each entity to the variables set **at** that entity.

    Groups are keyed `@name`, hosts by their bare name — the spellings ansible
    itself uses, so a host and a group of the same name cannot collide.

    A host appears once per group it belongs to and its variable lines are its
    **effective** set every time, so a host's entry here is its effective
    variables rather than what is set on the host. `attribute()` is what turns
    that into provenance; this function only reads the tree.
    """
    at: dict[str, dict[str, str]] = {}
    # The entity owning each depth, so a variable line at depth d belongs to the
    # entity at d - 1.
    owner: dict[int, str] = {}

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        m = _LINE.match(line)
        if m is None:
            # The root, `@all:`, carries no `|--`.
            if line.strip().startswith("@") and line.strip().endswith(":"):
                owner[0] = line.strip()[:-1]
                at.setdefault(owner[0], {})
            continue

        depth = m.group(1).count("|") + 1
        content = m.group(2).strip()

        var = _VAR.match(content)
        if var is not None:
            holder = owner.get(depth - 1)
            if holder is not None:
                at.setdefault(holder, {})[var.group(1)] = var.group(2)
            continue

        name = content[:-1] if content.endswith(":") else content
        owner[depth] = name
        at.setdefault(name, {})
        # Anything deeper belonged to a sibling we have left.
        for d in list(owner):
            if d > depth:
                del owner[d]

    return at


def _ancestry(group: str, children: dict[str, list[str]]) -> list[str]:
    """`group` and every group above it, **shallowest first**.

    Shallowest first is ansible's precedence order for inventory group
    variables, so the LAST entry of a chain built from this is the winner.
    """
    parents: dict[str, list[str]] = {}
    for parent, kids in children.items():
        for kid in kids:
            parents.setdefault(kid, []).append(parent)

    seen: list[str] = []
    frontier = [group]
    while frontier:
        node = frontier.pop(0)
        if node in seen:
            continue
        seen.append(node)
        frontier.extend(parents.get(node, []))

    # `all` is ROOTED IMPLICITLY, and it has to be: it is ansible's own root, so
    # it is deliberately absent from the declared structure -- the resolved
    # payload's `group-children` for a real two-level inventory is
    # `{"prod": ["prod_web"]}` and nothing above it.
    #
    # Without this, a variable from `group_vars/all` -- which is what the
    # workspace's own global vars render to -- finds no level in the chain and
    # reports `here`, crediting the host for a value it did not set. Measured on
    # a live instance: `region = eu` came back `origin=here chain=(none)` where
    # the honest answer is `all`.
    if "all" not in seen:
        seen.append("all")

    # `seen` is the group then upwards; reverse for shallowest-first.
    return list(reversed(seen))


def attribute(
    *,
    node: str,
    is_host: bool,
    effective: dict[str, Any],
    groups_of_node: list[str],
    at_level: dict[str, dict[str, str]],
    children: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """Per key, the chain of levels that set it and which one won.

    `effective` is the node's resolved variables from `--list` (real JSON
    types); `at_level` is `parse_graph`'s output (ansible's stringified forms).
    The comparison is between a stringified JSON value and ansible's own
    printing of the same value, which agrees for scalars and may not for a
    container — hence `unattributable` rather than a guess.

    Returns `{key: {"value": <json>, "chain": [{"level", "value"}], "origin":
    "<level>" | "here" | "unattributable"}}`.
    """
    # Every group above this node, shallowest first, then the node itself last
    # if it is a group: ansible's precedence order.
    #
    # `@`-prefixed for the lookup, because THE TWO SOURCES SPELL A GROUP
    # DIFFERENTLY -- `--graph` writes `@prod_web` and `--list` writes
    # `prod_web`. An unprefixed lookup misses every group and reports every
    # variable as set on the node itself, which is what driving ansible's real
    # output caught and no hand-written fixture would have: both spellings look
    # right in isolation, and the wrong answer is a plausible one.
    chain_levels: list[str] = []
    for g in groups_of_node:
        for a in _ancestry(g, children):
            lvl = f"@{a}"
            if lvl not in chain_levels:
                chain_levels.append(lvl)
    if not is_host and f"@{node}" not in chain_levels:
        chain_levels.append(f"@{node}")

    out: dict[str, dict[str, Any]] = {}
    for key, value in effective.items():
        chain = [
            # The sigil is stripped on the way out: it is how `--graph` spells
            # a group, not something a reader of the view should meet.
            {"level": lvl.lstrip("@"), "value": at_level[lvl][key]}
            for lvl in chain_levels
            if key in at_level.get(lvl, {})
        ]
        # A vaulted value is the one case where the two sources CANNOT print
        # the same text: `--list` carries the ciphertext mapping and the graph
        # carries the placeholder, because nothing here can decrypt (#2506).
        # Comparing them raw would leave every vaulted variable unmatched and so
        # credited to the wrong level -- `here` on a host, `unattributable` on a
        # group -- which is an attribution error in the view whose whole job is
        # attribution. Collapsing both sides to the placeholder is not a guess:
        # the winner is the DEEPEST level carrying the key, whatever the value,
        # so matching on "this level sets it, vaulted" is exactly right.
        shown = VAULTED_PLACEHOLDER if _is_vault_ciphertext(value) else str(value)
        # DEEPEST match wins, so walk the chain backwards.
        origin = ""
        for entry in reversed(chain):
            if entry["value"] == shown:
                origin = str(entry["level"])
                break

        if not origin:
            # Nothing in the ancestry supplies this value. For a HOST that means
            # the host itself set it, and it is the common case rather than an
            # edge: a host's own `host_vars` are invisible in the graph, because
            # the host line shows its EFFECTIVE set. So an unmatched key on a
            # host is `here`, including one that overrides a group -- which is
            # exactly the `shared = host-wins` shape, where the chain carries
            # `all` and `prod` and neither matches.
            #
            # For a GROUP the node itself is in the chain, so an unmatched key
            # has no explanation this module can give: say so rather than
            # picking the nearest level, because a wrong attribution in a view
            # whose purpose is attribution is worse than none.
            origin = "here" if is_host else "unattributable"
        out[key] = {"value": value, "chain": chain, "origin": origin}
    return out
