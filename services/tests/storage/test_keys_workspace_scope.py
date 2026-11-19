"""Workspace key classification is derived from `keys.py`; hand-built keys elsewhere are ledgered.
The scanner only sees literals that START with a workspace prefix (not `PREFIX + ws`, `f"{base}{ws}/"`)."""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from terrapod.storage import keys as keys_mod
from terrapod.storage.keys import (
    WORKSPACE_KEY_PREFIXES,
    prefix_may_hold_workspace_keys,
    workspace_id_of_key,
)

DUMMY = "ws-dummy123"

#: Builders taking a workspace parameter that are NOT workspace-scoped keys.
NOT_WORKSPACE_SCOPED: dict[str, str] = {
    "deleted_workspace_marker_key": (
        "flat marker under state/deleted/, deliberately outside the workspace's own "
        "prefix so it stays on the default location"
    ),
}

#: Functions in keys.py that are not key builders.
NOT_KEY_BUILDERS: dict[str, str] = {
    "oci_upload_prefix": "a prefix builder for cleanup, not a key",
    "workspace_id_of_key": "the classifier under test",
    "prefix_may_hold_workspace_keys": "a listing-prefix classifier, not a key",
}

_KEYS_FILE = pathlib.Path(keys_mod.__file__)
_PACKAGE_ROOT = _KEYS_FILE.parents[1]


def _builders() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(_KEYS_FILE.read_text())
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _workspace_param(node: ast.FunctionDef) -> int | None:
    """Position of the parameter naming a workspace, or None."""
    for i, arg in enumerate(node.args.args):
        if "workspace" in arg.arg or arg.arg in ("ws", "ws_id"):
            return i
    return None


def _workspace_builders() -> set[str]:
    return {
        name
        for name, node in _builders().items()
        if _workspace_param(node) is not None and name not in NOT_WORKSPACE_SCOPED
    }


def _shared_builders() -> set[str]:
    return (
        set(_builders()) - _workspace_builders() - set(NOT_WORKSPACE_SCOPED) - set(NOT_KEY_BUILDERS)
    )


def _build(name: str, workspace_id: str | None) -> str:
    """Call a builder with dummy args; `workspace_id` fills its workspace parameter."""
    fn = getattr(keys_mod, name)
    at = _workspace_param(_builders()[name]) if workspace_id is not None else None
    args: list[object] = []
    for i, p in enumerate(inspect.signature(fn).parameters.values()):
        if p.default is not inspect.Parameter.empty:
            continue
        if i == at:
            args.append(workspace_id)
        else:
            args.append(1 if p.annotation is int else f"seg{i}")
    return fn(*args)


class TestDerivedFromKeysModule:
    def test_the_subject_was_actually_found(self):
        """The floor: a matcher that finds nothing would pass everything below."""
        found = _workspace_builders()
        assert len(found) >= 15, f"only found {sorted(found)}; the matcher has drifted"
        assert {"state_key", "plan_log_key", "inventory_baseline_key"} <= found

    @pytest.mark.parametrize(
        ("source", "position"),
        [
            ("def foo_key(run_id, workspace_id): ...", 1),
            ("def foo_key(ws_id): ...", 0),
            ("def foo_key(a, b, ws): ...", 2),
            ("def foo_key(run_id, version): ...", None),
        ],
    )
    def test_the_detector_finds_a_workspace_parameter_anywhere(self, source, position):
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.FunctionDef)
        assert _workspace_param(node) == position

    def test_every_workspace_builder_round_trips(self):
        for name in sorted(_workspace_builders()):
            key = _build(name, DUMMY)
            assert workspace_id_of_key(key) == DUMMY, f"{name} -> {key}"

    def test_every_workspace_builder_is_under_a_listed_prefix(self):
        for name in sorted(_workspace_builders()):
            key = _build(name, DUMMY)
            assert key.startswith(WORKSPACE_KEY_PREFIXES), f"{name} -> {key}"

    def test_every_shared_builder_is_not_workspace_scoped(self):
        shared = _shared_builders()
        assert len(shared) >= 15, f"only found {sorted(shared)}"
        for name in sorted(shared):
            key = _build(name, None)
            assert workspace_id_of_key(key) is None, f"{name} -> {key}"

    def test_the_exempt_marker_is_not_workspace_scoped(self):
        for name in NOT_WORKSPACE_SCOPED:
            assert workspace_id_of_key(_build(name, DUMMY)) is None, name

    def test_the_exemptions_are_real_and_carry_reasons(self):
        names = set(_builders())
        for ledger in (NOT_WORKSPACE_SCOPED, NOT_KEY_BUILDERS):
            assert set(ledger) <= names, f"stale entries: {sorted(set(ledger) - names)}"
            for name, reason in ledger.items():
                assert reason.strip(), f"{name} is exempt with no reason given"


class TestWorkspaceIdOfKey:
    @pytest.mark.parametrize(
        "key",
        [
            "state/index.yaml",
            "state/deleted/ws-1.json",
            "state/loose.json",
            "registry/modules/a/b/c/1.0.0.tar.gz",
            "cache/binaries/tofu/1.0.0/linux_amd64",
            "policies/ps-1/v1.tar.gz",
            "oci/blobs/sha256/abc",
            "",
            "state/",
            "state//x.tfstate",
            "unknown/ws-1/x",
        ],
    )
    def test_shared_or_malformed_keys_are_none(self, key):
        assert workspace_id_of_key(key) is None

    def test_a_pulumi_checkpoint_belongs_to_its_workspace_not_a_segment(self):
        key = keys_mod.pulumi_checkpoint_key("ws-1", "upd-1")
        assert workspace_id_of_key(key) == "ws-1"

    def test_a_workspace_prefix_resolves(self):
        assert workspace_id_of_key("state/ws-1/") == "ws-1"

    def test_prefixes_cover_the_documented_set(self):
        assert set(WORKSPACE_KEY_PREFIXES) == {
            "state/",
            "plans/",
            "logs/",
            "config/",
            "runs/",
            "inventory/",
            "state/pulumi-checkpoints/",
        }


class TestPrefixMayHoldWorkspaceKeys:
    @pytest.mark.parametrize(
        "prefix",
        [
            "",
            "s",
            "state",
            "state/",
            "plans/",
            "inv",
            keys_mod.PULUMI_CHECKPOINT_PREFIX,
            "state/ws-1/",
            "state/ws-1",
            "logs/ws-1/plans/",
            "state/deleted",
        ],
    )
    def test_a_prefix_reaching_workspace_keys_may(self, prefix):
        assert prefix_may_hold_workspace_keys(prefix)

    @pytest.mark.parametrize(
        "prefix",
        [
            "state/deleted/",
            "state/deleted/ws-1.json",
            "state/index.yaml",
            "vcs_archives/",
            "cache/",
            "cache/binaries/",
            "oci/uploads/s1/",
            "registry/",
            "policies/",
            "backups/",
        ],
    )
    def test_a_shared_prefix_does_not(self, prefix):
        assert not prefix_may_hold_workspace_keys(prefix)


#: Hand-built workspace key literals outside `keys.py`: (file, literal) -> (count, reason).
#: Keyed by literal and count, not line, so edits don't churn it; delete entries as sites move.
_WS_LIST = "lists one workspace's state objects by hand-built prefix"
_STATE_SCAN = "scans the whole state/ prefix and splits the workspace id out by hand"
HAND_BUILT_SITES: dict[tuple[str, str], tuple[int, str]] = {
    ("services/deleted_workspace_service.py", "f'state/{workspace_id}/'"): (2, _WS_LIST),
    ("services/deleted_workspace_service.py", "f'state/{ws_id}/'"): (1, _WS_LIST),
    ("services/artifact_retention_service.py", "f'state/{ws_id}/'"): (1, _WS_LIST),
    ("services/artifact_retention_service.py", "'state/'"): (3, _STATE_SCAN),
    ("cli/restore_verify.py", "'state/'"): (1, "scans the whole state/ prefix for state versions"),
    ("services/blob_classes.py", "'state/'"): (1, "blob class prefix table, not a workspace key"),
    ("services/blob_classes.py", "'state/index.yaml'"): (1, "blob class prefix table"),
    ("services/blob_classes.py", "'config/'"): (1, "blob class prefix table"),
    ("services/blob_classes.py", "'logs/'"): (1, "blob class prefix table"),
    ("services/blob_classes.py", "'plans/'"): (1, "blob class prefix table"),
    ("services/blob_classes.py", "'runs/'"): (1, "blob class prefix table"),
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _scan_hand_built() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if path == _KEYS_FILE:
            continue
        tree = ast.parse(path.read_text())
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                skip.update(id(v) for v in node.values)
            if id(node) in skip:
                continue
            literal: str | None = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literal = node.value
            elif (
                isinstance(node, ast.JoinedStr)
                and node.values
                and isinstance(node.values[0], ast.Constant)
            ):
                literal = node.values[0].value
            else:
                continue
            if literal.startswith(WORKSPACE_KEY_PREFIXES):
                label = (path.relative_to(_PACKAGE_ROOT).as_posix(), ast.unparse(node))
                found[label] = found.get(label, 0) + 1
    return found


class TestNoHandBuiltWorkspaceKeysOutsideKeysModule:
    def test_no_unledgered_hand_built_workspace_keys(self):
        found = _scan_hand_built()
        new = {k: n for k, n in found.items() if k not in HAND_BUILT_SITES}
        assert not new, (
            f"workspace-shaped key literals outside keys.py: {sorted(new)}. Build the key "
            f"in storage/keys.py (and classify it via workspace_id_of_key), or ledger it "
            f"in HAND_BUILT_SITES with a reason."
        )

    def test_the_ledger_is_exact(self):
        found = _scan_hand_built()
        drift = {
            k: (n, found.get(k, 0))
            for k, (n, _) in HAND_BUILT_SITES.items()
            if found.get(k, 0) != n
        }
        assert not drift, f"ledger out of step with the tree (ledgered, found): {drift}"

    def test_the_scanner_actually_finds_the_known_sites(self):
        """The floor: a scanner that matches nothing would pass the gate above."""
        found = _scan_hand_built()
        assert len(found) >= len(HAND_BUILT_SITES)

    def test_every_ledger_entry_carries_a_reason(self):
        for site, (count, reason) in HAND_BUILT_SITES.items():
            assert count > 0 and reason.strip(), site
