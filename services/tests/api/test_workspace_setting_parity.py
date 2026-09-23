"""Every per-workspace setting is reachable from bulk update, or says why not (#1763).

Security scanning and the AI plan summary shipped settable on the workspace API
and the Terraform provider and on nothing else. A rule covering hundreds of
workspaces could not opt them in at creation, and there was no apply-to-existing
path either — and between those two gaps there was no scalable way to set them
at all. Neither omission was a decision; both were simply forgotten, because
nothing failed when they were.

This is what fails. It reads the `Workspace` model and requires every column to
be accounted for in exactly one of three places:

  * reachable from bulk update — `_FIELD_MAP` or `_FIELDS_HANDLED_SEPARATELY`;
  * `NEVER_BULK_SETTABLE` — a permanent exemption with its reason;
  * `NOT_YET_BULK_SETTABLE` — a pre-existing gap, with the issue tracking it.

A new column can therefore be added, but it cannot be *silently* absent: someone
has to write down which of the three it is. The split matters — collapsing the
last two into one list would let a genuine gap hide behind the word "exempt",
which is the failure this test exists to prevent. `NOT_YET_BULK_SETTABLE` is a
ratchet in the same shape as the migration-contraction ledger: it may shrink,
and an entry leaving it is the work being finished.
"""

from __future__ import annotations

from terrapod.api.routers.workspace_bulk import _FIELD_MAP, _FIELDS_HANDLED_SEPARATELY
from terrapod.db.models import Workspace

#: Columns a bulk update must never write, and why. These are not settings: they
#: are identity, state owned by another protocol, or values whose whole meaning
#: is per-workspace, so writing one value across a match set is incoherent
#: rather than merely unimplemented.
NEVER_BULK_SETTABLE: dict[str, str] = {
    # Identity and addressing.
    "id": "primary key",
    "name": "unique per workspace — one value across a match set cannot be written",
    "created_at": "audit timestamp, set once",
    "updated_at": "audit timestamp, maintained by the ORM",
    "engine": (
        "identity, not a setting — a workspace holds state produced by its engine, "
        "so changing it is a migration rather than a settings write"
    ),
    "owner_email": "ownership grants workspace admin; platform-admin only, via the workspace API",
    # Where this workspace's code lives. Necessarily per-workspace: pointing a
    # match set at one repo, branch or subdirectory would make every one of them
    # manage the same directory.
    "vcs_connection_id": "per-workspace source of truth for where the code lives",
    "vcs_repo_url": "per-workspace source of truth for where the code lives",
    "vcs_branch": "per-workspace source of truth for where the code lives",
    "working_directory": "per-workspace source of truth for where the code lives",
    # Poller bookkeeping, written by the VCS poll cycle.
    "vcs_last_commit_sha": "poller state",
    "vcs_last_polled_at": "poller state",
    "vcs_last_attempted_at": "poller state",
    "vcs_last_error": "poller state",
    "vcs_last_error_at": "poller state",
    # The manual/CLI state lock, owned by the lock endpoints. Bulk-writing it
    # would forge locks the lock protocol never issued.
    "locked": "owned by the lock/unlock endpoints",
    "lock_id": "owned by the lock/unlock endpoints",
    "lock_reason": "owned by the lock/unlock endpoints",
    "locked_by": "owned by the lock/unlock endpoints",
    # Drift *results*, as distinct from the drift settings below.
    "drift_last_checked_at": "drift run result, not a setting",
    "drift_status": "drift run result, not a setting",
    "drift_latest_run_id": "drift run result, not a setting",
    "state_diverged": "set by the runner when a state upload fails; cleared by resolving it",
    # Lifecycle and provenance, owned by the autodiscovery and catalog services.
    # Hand-writing these is how a workspace ends up claiming an origin it does
    # not have, or a lifecycle state nothing will act on.
    "lifecycle_state": "owned by the autodiscovery lifecycle service",
    "lifecycle_reason": "owned by the autodiscovery lifecycle service",
    "autodiscovery_pr_number": "provenance, written at materialisation",
    "autodiscovery_rule_id": "provenance, written at materialisation",
    "catalog_item_id": "provenance — marks the workspace catalog-managed and clamps its RBAC",
    "catalog_version_pin": "owned by the catalog service's reconfigure path",
    "catalog_input_values": "owned by the catalog service's reconfigure path",
}

#: Genuine settings that bulk update cannot reach yet. Each is an ordinary
#: per-workspace setting an operator could reasonably want to apply across a
#: fleet — these are the backlog #1763 describes, not exemptions. Entries leave
#: this dict as the work lands; nothing may be added without a tracking issue.
NOT_YET_BULK_SETTABLE: dict[str, str] = {
    "terragrunt_enabled": "#1763",
    "terragrunt_version": "#1763",
    "trigger_prefixes": "#1763",
    "vcs_workflow": "#1763 — pairs with auto_apply, see _reject_auto_apply_on_apply_then_merge",
    "auto_merge": "#1763",
    "auto_merge_strategy": "#1763",
    "plan_expiry_seconds": "#1763",
    "drift_detection_enabled": "#1763",
    "drift_detection_interval_seconds": "#1763",
    "drift_ignore_rules": "#1763",
    "slack_channel": "#1763",
    "pulumi_bind_plan": "#1763 — engine-dependent, like security_scan_enforcement",
}


def _columns() -> set[str]:
    return {c.key for c in Workspace.__table__.columns}


def _reachable() -> set[str]:
    return set(_FIELD_MAP.values()) | set(_FIELDS_HANDLED_SEPARATELY.values())


class TestEveryWorkspaceSettingIsAccountedFor:
    def test_no_column_is_silently_absent_from_bulk_update(self):
        """The gate. A new setting must be wired up or written down."""
        unaccounted = (
            _columns() - _reachable() - set(NEVER_BULK_SETTABLE) - set(NOT_YET_BULK_SETTABLE)
        )
        assert not unaccounted, (
            f"Workspace column(s) {sorted(unaccounted)} are settable on the workspace "
            "but unreachable from bulk update, and unlisted. Either add them to "
            "`_FIELD_MAP` in routers/workspace_bulk.py (and to the autodiscovery "
            "rule template and the GUI — see #1763), or record them in "
            "NEVER_BULK_SETTABLE with the reason, or in NOT_YET_BULK_SETTABLE "
            "with the issue tracking the gap."
        )

    def test_the_security_scan_and_ai_summary_settings_are_reachable(self):
        """The two feature sets #1763 was filed about, pinned by name.

        The general gate above would pass if a future change dropped these back
        out and added them to a ledger, so the specific regression is pinned
        separately: these were the reported defect.
        """
        for column in (
            "security_scan_enforcement",
            "security_scan_engine",
            "security_scan_severity_threshold",
            "security_scan_skip_rules",
            "ai_summary_mode",
            "ai_summary_context",
        ):
            assert column in _reachable(), f"{column} is no longer settable in bulk"

    def test_neither_ledger_names_a_column_that_no_longer_exists(self):
        """A ledger nobody prunes stops describing the code and starts hiding it."""
        columns = _columns()
        for name, ledger in (
            ("NEVER_BULK_SETTABLE", NEVER_BULK_SETTABLE),
            ("NOT_YET_BULK_SETTABLE", NOT_YET_BULK_SETTABLE),
        ):
            stale = set(ledger) - columns
            assert not stale, f"{name} names dropped column(s) {sorted(stale)} — remove them"

    def test_a_column_is_never_both_exempt_and_reachable(self):
        """Contradictory bookkeeping: the entry says one thing, the map another."""
        for name, ledger in (
            ("NEVER_BULK_SETTABLE", NEVER_BULK_SETTABLE),
            ("NOT_YET_BULK_SETTABLE", NOT_YET_BULK_SETTABLE),
        ):
            both = set(ledger) & _reachable()
            assert not both, f"{sorted(both)} is reachable from bulk update but listed in {name}"

        overlap = set(NEVER_BULK_SETTABLE) & set(NOT_YET_BULK_SETTABLE)
        assert not overlap, f"{sorted(overlap)} is in both ledgers — it is one or the other"

    def test_every_reason_is_written_down(self):
        """An empty reason is an entry that will be read as settled when it isn't."""
        for name, ledger in (
            ("NEVER_BULK_SETTABLE", NEVER_BULK_SETTABLE),
            ("NOT_YET_BULK_SETTABLE", NOT_YET_BULK_SETTABLE),
        ):
            blank = sorted(k for k, v in ledger.items() if not v.strip())
            assert not blank, f"{name} entries {blank} have no reason"

    def test_the_field_map_only_names_real_columns(self):
        """Catches a typo'd ORM attribute, which would otherwise 500 at apply time."""
        columns = _columns()
        bogus = sorted(a for a in _FIELD_MAP.values() if a not in columns)
        assert not bogus, f"_FIELD_MAP maps to non-existent Workspace column(s): {bogus}"
