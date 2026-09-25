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
and an entry leaving it is the work being finished. It is currently **empty** —
every genuine per-workspace setting is reachable — and the right way to keep it
that way is to wire a new setting up rather than add a line.
"""

from __future__ import annotations

import pathlib

import pytest

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

#: Genuine settings bulk update cannot reach yet — a debt, not an exemption.
#: #1763 closed the last of them, so this is empty and should stay that way:
#: an entry here means an operator cannot apply a real setting across a fleet.
NOT_YET_BULK_SETTABLE: dict[str, str] = {
    # EMPTY, and that is the goal state. Every genuine per-workspace setting is
    # reachable from bulk update.
    #
    # This exists so a gap can be recorded deliberately rather than discovered
    # later -- but an entry here is a debt, not a decision, and it must name the
    # issue that will clear it. Prefer wiring the setting up to adding a line.
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


# ── the other three surfaces #1763 named (#1763, second pass) ─────────
#
# The gate above covers bulk update SERVER-side only. Its own failure message
# tells you to wire a new setting into "the autodiscovery rule template and the
# GUI" too -- but nothing checked that, so the advice was a convention rather
# than a rule. `ai_policy_mode` then shipped in the same release as the gate,
# reachable from the API and the provider and absent from both admin pages: the
# exact defect class #1763 was filed to end, recurring inside the release that
# claimed to close it.
#
# These read the real surfaces and require each bulk-settable attribute to be
# present or deliberately excused, in the same shape as the ledgers above.

# The test image lays the tree out differently from a checkout: tests live at
# `/app/tests` with the surfaces beside them, not under `services/`. Try both,
# and fail loudly rather than silently passing if a surface is missing — a
# gate that cannot read the file it checks proves nothing, and this one exists
# because an unchecked convention already failed once.
_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _surface(*relative: str) -> pathlib.Path:
    for rel in relative:
        for base in (_TESTS_DIR.parents[2], _TESTS_DIR.parents[1]):  # local, docker
            candidate = base / rel
            if candidate.exists():
                return candidate
    raise FileNotFoundError(
        f"Cannot find {relative[0]} from {_TESTS_DIR}. If this is the test image, "
        "the file needs a COPY line in docker/Dockerfile.test — the parity gate "
        "reads it, so without it the gate would pass vacuously."
    )


_BULK_GUI = _surface("web/src/app/admin/bulk-update/page.tsx")
_AD_GUI = _surface("web/src/app/admin/autodiscovery/page.tsx")
_AD_API = _surface(
    "services/terrapod/api/routers/autodiscovery_rules.py",
    "terrapod/api/routers/autodiscovery_rules.py",
)

#: Attributes a surface deliberately does not offer, and why. Each is a
#: judgement that the control costs more than it is worth THERE -- never a
#: record that somebody forgot. An entry is cheap to add and must say why.
_SURFACE_EXEMPT: dict[str, dict[str, str]] = {
    "bulk-update GUI": {
        "labels": "handled by the dedicated label editor, not a named field",
        "agent-pool-ids": "pool assignment needs per-pool RBAC; the API takes it, the fleet form does not",
        "auto-apply": "deliberately not fleet-settable from a form: it arms unattended applies",
        "drift-ignore-rules": "a rule list needs per-workspace context a fleet form cannot show",
        "trigger-prefixes": "path lists are per-repository; a fleet-wide value is rarely meaningful",
        "security-scan-skip-rules": "a skip list is per-workspace reasoning, not a fleet default",
    },
    "autodiscovery GUI": {
        "labels": "handled by the dedicated label editor, not a named field",
        "agent-pool-ids": "the rule form sets a single pool; the list form is API-only",
        "trigger-prefixes": "the rule's own path patterns already scope what it matches",
        "security-scan-skip-rules": "a skip list is per-workspace reasoning, not a rule default",
    },
    "autodiscovery API": {
        "agent-pool-ids": "the rule template carries a single agent-pool id",
        "trigger-prefixes": "the rule's own path patterns already scope what it matches",
    },
}


#: Genuine gaps -- a debt, not a decision, and kept in a SEPARATE ledger from
#: `_SURFACE_EXEMPT` for the same reason `NOT_YET_BULK_SETTABLE` is separate
#: above: collapsing them would let a real gap hide behind the word "exempt",
#: which is the failure this gate exists to prevent. Every entry names the
#: issue that will clear it, and clearing one means wiring the setting up and
#: deleting the line. This should end empty.
_SURFACE_DEBT: dict[str, dict[str, str]] = {
    # `pulumi-bind-plan` is blocked on #1570, not merely unfinished, and the
    # distinction decides what "clearing" it means. An `AutodiscoveryRule` has
    # no `engine` column -- the model says so in its own comment -- so every
    # workspace a rule materialises is Terraform/OpenTofu. Templating a
    # Pulumi-only setting there would record a value that can never apply to
    # anything the rule creates, which is precisely what the bulk path answers
    # 422 for (`_reject_engine_specific_updates`). Wiring it up to empty this
    # ledger would satisfy the gate by adding the defect the gate exists to
    # catch. It clears when #1570 gives a rule an engine, and not before.
    "autodiscovery GUI": {
        "pulumi-bind-plan": "#1570 -- a rule has no engine, so this could never apply",
    },
    "autodiscovery API": {
        "pulumi-bind-plan": "#1570 -- a rule has no engine, so this could never apply",
    },
}


def _surface_sources() -> dict[str, str]:
    return {
        "bulk-update GUI": _BULK_GUI.read_text(),
        "autodiscovery GUI": _AD_GUI.read_text(),
        "autodiscovery API": _AD_API.read_text(),
    }


class TestEverySettableAttributeReachesEverySurface:
    def test_no_surface_is_silently_missing_a_setting(self):
        """The rule #1763 asked for, enforced rather than advised."""
        attrs = set(_FIELD_MAP) | set(_FIELDS_HANDLED_SEPARATELY)
        problems: list[str] = []
        for surface, src in _surface_sources().items():
            excused = {**_SURFACE_EXEMPT.get(surface, {}), **_SURFACE_DEBT.get(surface, {})}
            for a in sorted(attrs):
                if a in excused:
                    continue
                if f"'{a}'" not in src and f'"{a}"' not in src:
                    problems.append(f"{surface}: {a}")
        assert not problems, (
            "Settable per-workspace attribute(s) unreachable from a surface that "
            f"manages workspaces: {problems}. Wire each one up, or record it in "
            "_SURFACE_EXEMPT with the reason it does not belong there. #1763 asked "
            "for exactly this rule; leaving it to convention is how ai-policy-mode "
            "shipped on three surfaces out of four."
        )

    def test_the_ai_gate_override_reaches_all_of_them(self):
        """Pinned by name: this is the one that regressed, and the general gate
        above would pass again if a future change excused it instead."""
        for surface, src in _surface_sources().items():
            assert "'ai-policy-mode'" in src or '"ai-policy-mode"' in src, (
                f"ai-policy-mode is unreachable from {surface}"
            )

    def test_no_exemption_names_an_attribute_that_is_not_settable(self):
        """A stale excuse hides the setting it used to describe."""
        attrs = set(_FIELD_MAP) | set(_FIELDS_HANDLED_SEPARATELY)
        for surface, excused in _SURFACE_EXEMPT.items():
            stale = set(excused) - attrs
            assert not stale, f"{surface} excuses non-settable attribute(s): {sorted(stale)}"

    def test_no_debt_entry_names_an_attribute_that_is_not_settable(self):
        """A debt entry for something no longer settable is a line nobody will
        ever clear, and it hides the next real gap behind a stale name."""
        attrs = set(_FIELD_MAP) | set(_FIELDS_HANDLED_SEPARATELY)
        for surface, owed in _SURFACE_DEBT.items():
            stale = set(owed) - attrs
            assert not stale, f"{surface} owes work on non-settable attribute(s): {sorted(stale)}"

    def test_debt_and_exemptions_do_not_overlap(self):
        """A setting is either deliberately absent or owed. Both at once means
        one of the two ledgers is lying about it."""
        for surface in set(_SURFACE_EXEMPT) | set(_SURFACE_DEBT):
            both = set(_SURFACE_EXEMPT.get(surface, {})) & set(_SURFACE_DEBT.get(surface, {}))
            assert not both, f"{surface} both excuses and owes: {sorted(both)}"


class TestBooleanSettingsAreCheckedNotCoerced:
    """`bool()` coerces; it does not validate. `bool("false")` is True.

    Every boolean the workspace write paths accept must go through
    `validate_bool`, which rejects a string outright. The create path read
    `auto-merge` with a bare `bool()` while using `validate_bool` on
    `debug-mode` forty lines below — so a client that stringifies booleans
    turned auto-merge ON while asking for it to be off, and got a 201.
    """

    def test_bool_would_have_accepted_the_string(self):
        """The premise, pinned — so this test cannot quietly stop meaning
        anything if someone decides the coercion was harmless."""
        assert bool("false") is True

    def test_the_shared_validator_refuses_a_stringified_boolean(self):
        from terrapod.services import workspace_settings

        for bad in ("false", "true", 0, 1, "0"):
            with pytest.raises(ValueError):
                workspace_settings.validate_bool(bad, "auto-merge")

        assert workspace_settings.validate_bool(False, "auto-merge") is False
        assert workspace_settings.validate_bool(True, "auto-merge") is True

    def test_no_workspace_write_path_coerces_a_boolean_attribute(self):
        """Source-introspection, because the failure is invisible at runtime:
        a coerced boolean produces a 201 and the wrong stored value."""
        import inspect
        import re

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        offenders = re.findall(r"=\s*bool\(attrs\.get\(\"([a-z0-9-]+)\"", src)
        offenders += re.findall(r"=\s*bool\(attrs\[\"([a-z0-9-]+)\"\]", src)
        assert offenders == [], (
            "these boolean attributes are coerced rather than validated, so a "
            f"stringified 'false' would enable them: {sorted(set(offenders))}"
        )
