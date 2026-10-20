"""Rendering an inventory as a YAML source ansible can read.

Its own module because BOTH sides need it and a second copy is the defect: the
API renders the declared rows to feed `ansible-inventory`, and the runner
renders the resolved artifact back to a source to feed `ansible-playbook`
(#1972). Two implementations of a precedence-bearing document would agree
until one of them was edited.

Pure, and deliberately so -- **it imports nothing at all beyond `typing`**. That
is what lets the runner image carry it as one file next to `http_retry.py`
rather than pulling in the resolver's dependencies, and the bar is stricter than
"nothing from Terrapod": `requirements-runner.txt` is a deliberately minimal 21
packages and does NOT include PyYAML, so a module-level `import yaml` here makes
`job_entrypoint` unimportable and **every run of every kind fails at startup**
with `ModuleNotFoundError: No module named 'yaml'`. It shipped exactly that once.

So this module builds the DOCUMENT and serialises nothing. Each side serialises
its own way, which costs nothing because the structure is where every
precedence-bearing decision lives -- the `all:` root, group nesting, membership,
and host-level variables:

* the API renders YAML (`inventory_resolve.render_inventory_yaml`), because it
  feeds `ansible-inventory` and is the file a human debugging the resolver reads;
* the runner renders JSON (`configure_inventory.document_to_source`), because
  **JSON is valid YAML** -- measured against ansible-core 2.21.5, which accepts a
  JSON-serialised inventory on `.yml`, `.yaml` and `.json` alike, resolves the
  same hosts, and expands `--limit` through the same nesting.
"""

from typing import Any


def build_inventory_document(
    *,
    hosts: list[tuple[str, dict[str, Any]]],
    groups: list[tuple[str, dict[str, Any]]],
    memberships: list[tuple[str, str]],
    children: list[tuple[str, str]],
    global_vars: dict[str, Any],
) -> dict[str, Any]:
    """The declared rows as one YAML-inventory DOCUMENT, ready to serialise.

    Rooted at `all:`, which is YAML inventory's own structure -- and exactly why
    `all` cannot also be a declared group name. The `group_vars/all` rows land
    at that root's `vars:`, so they apply to every host without a group row to
    collide with.

    Group membership and nesting are emitted as ansible's `hosts:` and
    `children:` maps and nothing more. Precedence among `all`, a more specific
    group and a host variable is ansible's to apply; emitting the structure
    faithfully is the whole job here.
    """
    members: dict[str, list[str]] = {}
    for group_name, host_name in memberships:
        members.setdefault(group_name, []).append(host_name)

    nested: dict[str, list[str]] = {}
    for parent, child in children:
        nested.setdefault(parent, []).append(child)

    group_block: dict[str, Any] = {}
    for name, variables in groups:
        entry: dict[str, Any] = {}
        if name in members:
            # A mapping with null values, not a list: that is the shape
            # ansible's YAML plugin expects for a group's hosts.
            entry["hosts"] = dict.fromkeys(sorted(members[name]))
        if name in nested:
            entry["children"] = dict.fromkeys(sorted(nested[name]))
        if variables:
            entry["vars"] = variables
        # An empty group is still a group -- ansible will report it, and a
        # reader asking "did my group get created" needs to see it.
        group_block[name] = entry or None

    root: dict[str, Any] = {}
    if global_vars:
        root["vars"] = global_vars
    # Every host at the root too, so a host in no group is still in the
    # inventory. Without this a group-less host would exist only if some group
    # happened to name it.
    if hosts:
        root["hosts"] = {name: (variables or None) for name, variables in sorted(hosts)}
    if group_block:
        root["children"] = group_block

    # Rooted at `all`, which is YAML inventory's own structure. Returned rather
    # than serialised: see the module docstring for why each side does its own.
    return {"all": root or None}
