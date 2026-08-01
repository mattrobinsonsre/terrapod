"""No bulk Core statement may target a replicated model.

The outbox hook (`replication.install_outbox_hooks`) builds its events from
`session.new` / `session.dirty` / `session.deleted`. A Core `delete(Model)` or
`update(Model)` statement never populates any of those — SQLAlchemy issues the
SQL without materialising the rows — so the write happens locally and **no
replication event is emitted**.

That fails in the worst direction. The operator sees the change applied, and
the standby keeps the old rows indefinitely: `reconcile_deletions` only runs
from a from-scratch backfill, which a healthy follower never performs. Two
real cases were exactly this — `revoke_all_for_user` (an offboarded person's
API tokens stayed live on the peer) and `delete_user` (a deleted admin kept
their `admin` grant there). Failing back then re-inserted them onto the node
that had correctly deleted them.

Deleting through the ORM one row at a time is slower and correct. These tables
are small and the operations are rare, so there is no case where the bulk form
is worth a silent divergence.

`replication.py` itself is exempt: its `apply_delete` is the follower applying
an event it already received, and it targets `spec.model` — a value, not a
name — so it could not match this scan anyway.
"""

import ast
from pathlib import Path

from terrapod.services import replication

_SOURCE_ROOT = Path(replication.__file__).resolve().parents[1]
_EXEMPT = {"replication.py", "replication_registry.py"}

#: Bulk statements that are deliberate, each with the reason it is allowed.
#:
#: Keyed on "<module>::<function>::<callee>(<Model>)" — no line number, so the
#: entry survives edits above it, but the enclosing function pins it to ONE call
#: site rather than blanketing the module. Adding an entry is a conscious act: it
#: says "this field genuinely should not replicate, or cannot be written through
#: the ORM", and needs the reason in the value.
ALLOWED = {
    # A per-request telemetry timestamp, already rate-limited to one write per
    # LAST_USED_UPDATE_INTERVAL. Replicating it would put an outbox event on
    # the hot path of every authenticated request to carry a field nothing
    # depends on. The token row itself replicates; only this column does not.
    "terrapod/auth/api_tokens.py::validate_api_token::update(APIToken)": (
        "last_used_at is per-request telemetry, already rate-limited"
    ),
    # The poller cursor CAS. It has to stay a Core statement — the
    # compare-and-swap with RETURNING is what stops two replicas creating a run
    # for the same commit. The ORM assignment immediately after it is what
    # emits the event; see the comment there, which says so.
    "terrapod/services/vcs_poller.py::_poll_workspace_branch::update(Workspace)": (
        "compare-and-swap; the following ORM assignment emits the event"
    ),
    # Runs in a fresh session AFTER the failing one was rolled back, so it
    # cannot re-read through the ORM. vcs_last_error* are node-local
    # diagnostics about THIS node's polling, not shared state.
    # The claim-release CAS, same shape and same reason as the forward claim
    # above: Core statement for the compare-and-swap, ORM assignment right after
    # it to emit the event.
    "terrapod/services/vcs_poller.py::_release_commit_claim::update(Workspace)": (
        "compare-and-swap; the following ORM assignment emits the event"
    ),
    "terrapod/services/vcs_poller.py::_record_poll_failure::update(Workspace)": (
        "post-rollback diagnostic write; the columns are node-local"
    ),
}


def _replicated_model_names() -> set[str]:
    return {spec.model.__name__ for spec in replication.registered().values()}


def _call_sites(models: set[str]) -> list[tuple[str, int]]:
    """Every `delete(Model)` / `update(Model)` naming a replicated model.

    Returns (key, lineno) where key is "<module>::<function>::<callee>(<Model>)".
    """
    sites: list[tuple[str, int]] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        if path.name in _EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - source must parse
            continue
        rel = path.relative_to(_SOURCE_ROOT.parent)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in ("delete", "update"):
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in models:
                        key = f"{rel}::{func.name}::{node.func.id}({arg.id})"
                        sites.append((key, node.lineno))
    return sites


def _bulk_statements_against(models: set[str]) -> list[str]:
    """The call sites that are NOT explicitly allowed."""
    return sorted(
        f"{key} (line {lineno})" for key, lineno in _call_sites(models) if key not in ALLOWED
    )


class TestNoBulkWritesOnReplicatedModels:
    def test_no_bulk_delete_or_update_targets_a_replicated_model(self):
        offenders = _bulk_statements_against(_replicated_model_names())

        assert not offenders, (
            "bulk Core statements bypass the replication outbox — the row changes "
            "locally and the standby never hears about it. Delete/update through "
            "the ORM instead so `session.deleted` / `session.dirty` sees it:\n  "
            + "\n  ".join(offenders)
        )

    def test_every_exemption_still_matches_a_real_call_site(self):
        """A stale exemption is worse than none — it silently pre-approves the
        next bulk statement someone adds to that module for that model."""
        live = {key for key, _ in _call_sites(_replicated_model_names())}

        stale = set(ALLOWED) - live
        assert not stale, f"exemptions no longer matching any call site: {sorted(stale)}"

    def test_the_scanner_can_actually_find_something(self):
        """Guards the gate: if the AST walk silently matched nothing, the test
        above would pass forever regardless of what the source did."""
        # `User` is replicated, and users.py legitimately contains `select(User)`
        # — so a scan for a name we know IS present proves the walk reaches it.
        planted = _bulk_statements_against({"User"} | _replicated_model_names())
        # No production bulk statement should exist, but the machinery must at
        # least resolve real model names and real files.
        assert _replicated_model_names(), "no replicated classes registered — gate is inert"
        assert _SOURCE_ROOT.rglob("*.py"), "source tree not found — gate is inert"
        assert isinstance(planted, list)
