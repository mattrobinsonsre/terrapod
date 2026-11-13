"""API attribute contract gate (#550) — freeze JSON:API response attribute names.

The single most common silent break: dropping or renaming a `data.attributes`
key breaks every consumer that reads it (go-terrapod, the Terraform provider,
the web UI, third-party automation) even though the *route* is unchanged and no
error is raised — the field just vanishes. The existing `test_api_sdk_contract`
guards only the `workspace` resource; this gate covers **every** JSON:API
serializer.

It AST-parses each router, finds every serializer's `"attributes"` block, and
diffs the attribute-name set per serializer against a committed snapshot
(`api_attribute_contract.json`):

- a **removed / renamed** attribute is a **breaking change** — MAJOR bump or a
  documented deprecation window, NOT a snapshot regen;
- a **new** attribute is additive — accept it by regenerating
  (`UPDATE_API_CONTRACT=1 pytest tests/api/test_attribute_contract.py`).

Both forms of serializer are covered: the inline `"attributes": {...}` literal,
**and** the variable form `attrs = {...}; ...; "attributes": attrs` (very common
— e.g. the `authentication-tokens` and `plans` serializers build the dict in a
local first). For the variable form we resolve the local's dict-literal keys plus
any later `attrs["key"] = ...` subscript additions in the same function.

Nested objects are frozen leaf by leaf, as dotted paths: `permissions` *and*
`permissions.can-queue-apply`. Recording only the container was a real blind
spot (#1344) — a client reads `permissions.can-queue-apply` and
`actions.is-confirmable` to decide whether to render a control, so removing one
made a button silently vanish (or 403 on press) while this gate stayed green.
The container is kept alongside its leaves so the change was a pure addition.

Scope/limits: keys must be string literals reachable this way. A key computed at
runtime (a non-literal subscript, a `**spread`, or a dict built by a helper the
serializer calls) is not frozen — a known gap, never a false failure.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import terrapod.api.routers as _routers_pkg

_ROUTERS_DIR = Path(_routers_pkg.__file__).resolve().parent
_SNAPSHOT = Path(__file__).parent / "api_attribute_contract.json"


def _local_dict_nodes(fn: ast.AST) -> dict[str, list[ast.Dict]]:
    """Map local var name -> the dict literals assigned to it in the function."""
    nodes: dict[str, list[ast.Dict]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Dict):
            nodes.setdefault(target.id, []).append(value)
    return nodes


def _dict_literal_keys(
    d: ast.Dict,
    *,
    _prefix: str = "",
    _locals: dict[str, list[ast.Dict]] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> set[str]:
    """String keys of a dict literal, including dotted paths into nested literals.

    Both the container and its leaves are recorded — `permissions` *and*
    `permissions.can-queue-apply`. Keeping the container means regeneration is
    purely additive rather than trading one entry for several, and the leaves
    are what actually needed guarding: a client reads `permissions.can-queue-apply`
    and `actions.is-confirmable` to decide whether to render a control, so
    removing one makes a button silently vanish while a flat gate stays green
    (#1344).

    A nested value that is a bare name bound to a dict literal in the same
    function (``inner = {...}; {"x": inner}``) is followed through ``_locals``,
    so ``x.<leaf>`` is frozen too.
    """
    keys: set[str] = set()
    # A `**spread` contributes a None key; skip it rather than treating it as a name.
    for key, value in zip(d.keys, d.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        path = f"{_prefix}{key.value}"
        keys.add(path)
        if isinstance(value, ast.Dict):
            keys |= _dict_literal_keys(value, _prefix=f"{path}.", _locals=_locals, _seen=_seen)
        elif (
            isinstance(value, ast.Name)
            and _locals is not None
            and value.id in _locals
            and value.id not in _seen
        ):
            for inner in _locals[value.id]:
                keys |= _dict_literal_keys(
                    inner, _prefix=f"{path}.", _locals=_locals, _seen=_seen | {value.id}
                )
    return keys


def _local_dict_keys(fn: ast.AST) -> dict[str, set[str]]:
    """Map local var name -> string keys, from ``name = {literal}`` assignments and
    later ``name["key"] = ...`` subscript additions in the function."""
    var_keys: dict[str, set[str]] = {}
    local_nodes = _local_dict_nodes(fn)
    for node in ast.walk(fn):
        # name = {dict literal}  (plain and annotated: `attrs: dict = {...}`)
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if isinstance(value, ast.Dict) and len(targets) == 1 and isinstance(targets[0], ast.Name):
            var_keys.setdefault(targets[0].id, set()).update(
                _dict_literal_keys(value, _locals=local_nodes, _seen=frozenset({targets[0].id}))
            )
        # name["literal key"] = ...
        for tgt in targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and isinstance(tgt.slice, ast.Constant)
                and isinstance(tgt.slice.value, str)
            ):
                path = tgt.slice.value
                keys = var_keys.setdefault(tgt.value.id, set())
                keys.add(path)
                # `attrs["permissions"] = {...}` hides leaves too, same as the
                # inline form.
                if isinstance(value, ast.Dict):
                    keys |= _dict_literal_keys(value, _prefix=f"{path}.", _locals=local_nodes)
    return var_keys


def _chain_block_keys(fn: ast.AST, block: str) -> set[str]:
    """Keys written through a SUBSCRIPT CHAIN, e.g.
    ``document["data"]["attributes"]["position-in-queue"] = position``.

    `_local_dict_keys` follows one level (``name["key"] = ...``) because that is
    the shape it was written for. A chain whose target sits two or more levels
    deep has a Subscript — not a Name — as its `.value`, so it matched nothing,
    and an attribute added that way was pinned by this snapshot at all: the
    serializer's literal dict is complete and the injection happens after it.

    Measured when this was added: four attributes across three routers were
    invisible, and they are not minor ones. `agent_pools._token_json` returns
    the raw join `token` exactly once, on create, so a rename breaks every
    listener bootstrap; `runs.show_run` injects `last-output-at`, which is what
    the UI reads to tell a quiet configure from a stalled one; and
    `position-in-queue` is the single attribute the two run-queue routes exist
    to produce. #2425 section 4.6 reported one of the four, as "a worked example
    of a snapshot pinning less than it appears to" -- so the example is the
    point, and the fix is the chain rather than the instance.

    The rule is deliberately narrow: the segment BEFORE the final key must be
    the block's own name. That is what makes it a write into this block rather
    than a guess about any nested dict, and it needs no exemption ledger.
    """
    found: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Subscript) or isinstance(tgt.value, ast.Name):
                continue
            if not (isinstance(tgt.slice, ast.Constant) and isinstance(tgt.slice.value, str)):
                continue
            parent = tgt.value
            if not isinstance(parent, ast.Subscript):
                continue
            if not (isinstance(parent.slice, ast.Constant) and parent.slice.value == block):
                continue
            found.add(tgt.slice.value)
            if isinstance(node.value, ast.Dict):
                found |= _dict_literal_keys(node.value, _prefix=f"{tgt.slice.value}.")
    return found


def _block_of_function(fn: ast.AST, block: str) -> set[str] | None:
    """Return the string keys of the function's ``block`` dict — whether written
    inline (``"attributes": {...}``), via a local variable
    (``attrs = {...}; "attributes": attrs``), or through a subscript chain into
    an already-built document — or None if it has no such block."""
    var_keys = _local_dict_keys(fn)
    local_nodes = _local_dict_nodes(fn)
    chained = _chain_block_keys(fn, block)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == block):
                continue
            if isinstance(value, ast.Dict):
                return _dict_literal_keys(value, _locals=local_nodes) | chained
            if isinstance(value, ast.Name) and value.id in var_keys:
                return var_keys[value.id] | chained
    # A function that only INJECTS into a document another function built has
    # no block of its own, and its keys belong to that document all the same.
    return chained or None


def _attributes_of_function(fn: ast.AST) -> set[str] | None:
    return _block_of_function(fn, "attributes")


def _meta_of_function(fn: ast.AST) -> set[str] | None:
    """The response's ``meta`` keys.

    Gated for the same reason attributes are, and it was NOT. The AI policy
    endpoint puts its authoritative answer in `meta.blocking` — the web panel
    keys both its blocked banner and its override button on that — and
    `meta.not-evaluated-reason` is the only thing distinguishing "waiting for a
    verdict" from "no verdict is coming". Renaming either would have broken the
    one control that releases a held run, with ZERO CI signal.

    `meta.pagination` rides the same gate, which is the other load-bearing
    meta block in the API.
    """
    return _block_of_function(fn, "meta")


def extract_attribute_contract() -> dict[str, list[str]]:
    """`{"<router>.<serializer>": [sorted attribute names]}` for every JSON:API
    serializer across the routers — discovered by structure (a function whose
    body builds an ``"attributes"`` dict), not by name."""
    contract: dict[str, list[str]] = {}
    for path in sorted(_ROUTERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            attrs = _attributes_of_function(node)
            if attrs:
                contract[f"{path.stem}.{node.name}"] = sorted(attrs)
            meta = _meta_of_function(node)
            if meta:
                contract[f"{path.stem}.{node.name}:meta"] = sorted(meta)
    return contract


def test_attribute_contract_unchanged() -> None:
    current = extract_attribute_contract()

    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return

    assert _SNAPSHOT.exists(), (
        f"Attribute contract snapshot missing at {_SNAPSHOT}. Generate it with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/api/test_attribute_contract.py"
    )
    snapshot: dict[str, list[str]] = json.loads(_SNAPSHOT.read_text())

    breaking: list[str] = []
    additive: list[str] = []

    for serializer, snap_attrs in snapshot.items():
        if serializer not in current:
            breaking.append(f"{serializer}: entire serializer removed/renamed")
            continue
        removed = sorted(set(snap_attrs) - set(current[serializer]))
        if removed:
            breaking.append(f"{serializer}: removed/renamed attributes {removed}")
    for serializer, cur_attrs in current.items():
        added = sorted(set(cur_attrs) - set(snapshot.get(serializer, [])))
        if added:
            additive.append(f"{serializer}: added {added}")

    problems: list[str] = []
    if breaking:
        problems.append(
            "BREAKING: JSON:API response attributes were REMOVED or RENAMED. Every "
            "consumer that reads them (go-terrapod, the provider, the web UI, "
            "third-party automation) breaks silently. This requires a MAJOR bump or "
            "a documented deprecation window — do NOT just regenerate the snapshot:\n"
            "  " + "\n  ".join(breaking)
        )
    if additive:
        problems.append(
            "New attributes were added (additive, non-breaking). Accept them by "
            "regenerating the snapshot:\n"
            "  UPDATE_API_CONTRACT=1 pytest tests/api/test_attribute_contract.py\n"
            "  " + "\n  ".join(additive)
        )
    assert not problems, "\n\n".join(problems)


def test_extractor_finds_the_known_serializers() -> None:
    # Bite-check: the extractor must actually resolve real serializers (guards
    # against a silently-empty contract that would disable the gate).
    contract = extract_attribute_contract()
    assert len(contract) >= 25
    assert "variables._var_json" in contract
    assert "key" in contract["variables._var_json"]
    assert "runs._run_json" in contract
    assert "status" in contract["runs._run_json"]
    # Variable-form serializers (attrs = {...}; "attributes": attrs) must be
    # covered too — regression guard for the #550 blocker where they were missed.
    assert "tokens._token_to_jsonapi" in contract
    assert "bound-to" in contract["tokens._token_to_jsonapi"]
    assert "runs._plan_json" in contract
    assert "has-changes" in contract["runs._plan_json"]


def test_nested_objects_are_guarded_not_just_their_container() -> None:
    """Leaves inside `permissions` / `actions` are frozen individually (#1344).

    These are the fields a client reads to decide whether to render a control —
    the web UI gates the apply button on `permissions.can-queue-apply` and the
    run action row on `actions.is-confirmable`. While the extractor recorded
    only the container name, removing one of them left every gate green and the
    break surfaced as a control that silently vanished, or one that 403s on
    press. Guard the recursion itself, so a future refactor of the extractor
    cannot quietly drop it.
    """
    contract = extract_attribute_contract()

    ws = contract["tfe_v2._workspace_json"]
    assert "permissions" in ws, "the container must stay — dropping it would be a breaking regen"
    assert "permissions.can-queue-apply" in ws
    assert "permissions.can-queue-run" in ws
    assert "latest-run.status" in ws, "a nested local dict's leaves are frozen too"

    run = contract["runs._run_json"]
    assert "actions.is-confirmable" in run
    assert "permissions.can-apply" in run


def test_nested_extraction_handles_depth_and_spread() -> None:
    """Dotted paths at arbitrary depth; a `**spread` contributes no phantom key."""
    fn = ast.parse(
        "def s(x):\n"
        "    return {'attributes': {'top': 1, 'obj': {'leaf': 2, 'deep': {'inner': 3}},\n"
        "                           **extra}}\n"
    ).body[0]
    keys = _attributes_of_function(fn)
    assert keys == {"top", "obj", "obj.leaf", "obj.deep", "obj.deep.inner"}


def test_nested_extraction_covers_the_variable_form() -> None:
    """`attrs = {...}` and `attrs["k"] = {...}` recurse the same as the inline form."""
    fn = ast.parse(
        "def s(x):\n"
        "    attrs = {'a': 1, 'nested': {'leaf': 2}}\n"
        "    attrs['later'] = {'also': 3}\n"
        "    return {'attributes': attrs}\n"
    ).body[0]
    keys = _attributes_of_function(fn)
    assert keys == {"a", "nested", "nested.leaf", "later", "later.also"}


def test_a_nested_local_dict_is_followed_with_its_dotted_prefix() -> None:
    """``inner = {...}; {"x": inner}`` freezes ``x.a`` and deeper, not only ``x``."""
    fn = ast.parse(
        "def s(x):\n    inner = {'a': 1, 'b': {'c': 2}}\n    return {'attributes': {'x': inner}}\n"
    ).body[0]
    assert _attributes_of_function(fn) == {"x", "x.a", "x.b", "x.b.c"}


def test_a_nested_local_dict_is_followed_in_the_variable_form_and_when_defined_later() -> None:
    fn = ast.parse(
        "def s(x):\n"
        "    attrs = {'x': inner}\n"
        "    inner = {'a': 1}\n"
        "    return {'attributes': attrs}\n"
    ).body[0]
    assert _attributes_of_function(fn) == {"x", "x.a"}


def test_a_self_referencing_local_does_not_loop() -> None:
    fn = ast.parse("def s(x):\n    d = {'a': d}\n    return {'attributes': {'k': d}}\n").body[0]
    assert _attributes_of_function(fn) == {"k", "k.a"}


def test_a_subscript_chain_into_an_existing_document_is_extracted() -> None:
    """``document["data"]["attributes"]["k"] = v`` -- the shape that was invisible.

    A route that calls another serializer and then injects one attribute has no
    ``"attributes"`` literal of its own, so this is the only thing standing
    between such an attribute and no gate at all (#2425 section 4.6).
    """
    fn = ast.parse(
        "def s(x):\n"
        "    document = build(x)\n"
        "    document['data']['attributes']['position-in-queue'] = 3\n"
        "    document['data']['attributes']['extra'] = {'leaf': 1}\n"
        "    return document['data']\n"
    ).body[0]
    assert _attributes_of_function(fn) == {"position-in-queue", "extra", "extra.leaf"}


def test_a_chain_injection_unions_with_the_function_s_own_block() -> None:
    """A serializer that builds its block AND injects into it keeps both."""
    fn = ast.parse(
        "def s(x):\n"
        "    result = {'attributes': {'use-count': 1}}\n"
        "    result['attributes']['token'] = raw\n"
        "    return result\n"
    ).body[0]
    assert _attributes_of_function(fn) == {"use-count", "token"}


def test_a_chain_rule_reads_the_block_before_the_key_not_any_nesting() -> None:
    """The segment before the final key must BE the block.

    Without that clause the rule would claim any key written into any nested
    dict, which is a guess rather than a reading -- and it would put
    ``relationships`` and ``links`` keys into the attribute contract.
    """
    fn = ast.parse(
        "def s(x):\n"
        "    d = build(x)\n"
        "    d['data']['relationships']['workspace'] = ref\n"
        "    d['data']['links']['self'] = url\n"
        "    d['meta']['pagination'] = page\n"
        "    return d\n"
    ).body[0]
    assert _attributes_of_function(fn) is None
    assert _meta_of_function(fn) == {"pagination"}


def test_a_chain_rule_ignores_a_computed_key() -> None:
    """A key the extractor cannot read is not a key it may invent."""
    fn = ast.parse(
        "def s(x):\n"
        "    d = build(x)\n"
        "    d['data']['attributes'][name] = v\n"
        "    d['data']['attributes']['known'] = v\n"
        "    return d\n"
    ).body[0]
    assert _attributes_of_function(fn) == {"known"}
