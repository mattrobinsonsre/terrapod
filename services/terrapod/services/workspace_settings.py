"""Per-workspace security-scan and AI-summary settings, and their rules (#1763).

These settings shipped on the workspace API and the provider resource, but on
nothing else — so a rule covering hundreds of workspaces could not opt them in
at creation, and there was no apply-to-existing path either. Between those two
gaps there was no scalable way to set them at all.

The rules live here rather than in a router for the reason
`services.parallelism` already gives: four paths write a workspace setting —
workspace create, workspace update, the autodiscovery rule template, and bulk
update — and a validator only one caller uses is one the next caller forgets.
That is not a hypothetical here; it is the defect this module exists to close.

Everything raises `ValueError`. The HTTP surfaces wrap it into a 422; the
autodiscovery template and any future non-HTTP caller do not have to care.

On `main` `validate_scan_enforcement` takes an engine, because a Pulumi run
has nothing for the scanners to read (#1567). This line has no engine strategy
layer, so it takes none.
"""

from __future__ import annotations

import re

SCAN_ENFORCEMENTS = frozenset({"off", "advisory", "enforced"})
SCAN_ENGINES = frozenset({"checkov", "trivy", "both"})
SCAN_SEVERITY_THRESHOLDS = frozenset({"critical", "high", "medium", "low"})
AI_SUMMARY_MODES = frozenset({"default", "enabled", "disabled"})
VCS_WORKFLOWS = frozenset({"merge_then_apply", "apply_then_merge"})
AUTO_MERGE_STRATEGIES = frozenset({"merge", "squash", "rebase"})

#: A guard against a value that is certainly a mistake, not a limit the
#: scanners impose.
MAX_SCAN_SKIP_RULES = 200
MAX_SCAN_SKIP_RULE_LENGTH = 100

#: Workspace context is a hint, not a repo. Anything bigger belongs in
#: `fleet_context` or a doc the operator pastes into their `prompt_suffix`.
MAX_AI_SUMMARY_CONTEXT = 4000


def _enum(value: object, valid: frozenset[str], field: str, default: str) -> str:
    s = str(value if value is not None else default)
    if s not in valid:
        raise ValueError(f"{field} must be one of {sorted(valid)}")
    return s


def validate_scan_enforcement(raw: object, default: str = "advisory") -> str:
    """`security-scan-enforcement`.

    Takes no engine on this line, unlike `main`. There is no engine strategy
    layer here, so every workspace is a Terraform/OpenTofu one and therefore
    always scannable. On `main` this refuses a non-`off` value where the
    scanners have nothing to read (#1567); there is nothing here for that to
    guard, and inventing the check would reject values that are perfectly
    valid on this line.
    """
    return _enum(raw, SCAN_ENFORCEMENTS, "security-scan-enforcement", default)


def validate_scan_engine(raw: object, default: str = "checkov") -> str:
    return _enum(raw, SCAN_ENGINES, "security-scan-engine", default)


def validate_scan_severity_threshold(raw: object, default: str = "high") -> str:
    return _enum(raw, SCAN_SEVERITY_THRESHOLDS, "security-scan-severity-threshold", default)


def validate_scan_skip_rules(raw: object) -> list[str]:
    """A list of non-empty rule-id strings (Checkov `CKV_*` / Trivy `AVD-*`)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("security-scan-skip-rules must be a list")
    if len(raw) > MAX_SCAN_SKIP_RULES:
        raise ValueError(f"security-scan-skip-rules: maximum {MAX_SCAN_SKIP_RULES} entries")
    out: list[str] = []
    for v in raw:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("security-scan-skip-rules entries must be non-empty strings")
        if len(v) > MAX_SCAN_SKIP_RULE_LENGTH:
            raise ValueError(
                f"security-scan-skip-rules entries must be ≤ {MAX_SCAN_SKIP_RULE_LENGTH} characters"
            )
        out.append(v.strip())
    return out


def validate_ai_summary_mode(raw: object) -> str:
    """A three-state enum, also constrained by a DB CHECK.

    Rejected here rather than at the constraint so the caller gets something
    actionable instead of a 500 from the integrity error.
    """
    if raw not in AI_SUMMARY_MODES:
        raise ValueError("ai-summary-mode must be 'default', 'enabled', or 'disabled'")
    return str(raw)


def validate_ai_policy_mode(raw: object) -> str:
    """The AI policy gate's per-workspace override (#1766).

    Same three states as `ai-summary-mode`, and validated the same way -- but
    accepting "disabled" here is not the same as honouring it. A MANDATORY gate
    ignores it by design (`ai_policy_service.effective_enforcement`), because a
    fleet-wide blocking control any workspace admin could switch off is not a
    control. Rejecting the value instead would be worse: it would stop an
    operator recording an advisory opt-out that is perfectly legitimate, and
    imply the gate is weaker than it is.
    """
    if raw not in AI_SUMMARY_MODES:
        raise ValueError("ai-policy-mode must be 'default', 'enabled', or 'disabled'")
    return str(raw)


def validate_ai_summary_context(raw: object) -> str:
    ctx = "" if raw is None else raw
    if not isinstance(ctx, str):
        raise ValueError("ai-summary-context must be a string")
    if len(ctx) > MAX_AI_SUMMARY_CONTEXT:
        raise ValueError(f"ai-summary-context max length is {MAX_AI_SUMMARY_CONTEXT} characters")
    return ctx


def validate_vcs_workflow(raw: object) -> str:
    if raw not in VCS_WORKFLOWS:
        raise ValueError("vcs-workflow must be 'merge_then_apply' or 'apply_then_merge'")
    return str(raw)


def validate_auto_merge_strategy(raw: object) -> str:
    if raw not in AUTO_MERGE_STRATEGIES:
        raise ValueError("auto-merge-strategy must be 'merge', 'squash', or 'rebase'")
    return str(raw)


def check_apply_then_merge_allowed(
    workflow: str, *, has_vcs_connection: bool, auto_apply: bool
) -> None:
    """The two invariants `apply_then_merge` carries, in one place.

    Under `apply_then_merge` the apply runs BEFORE the PR merges, so it needs a
    VCS connection to have a PR at all, and auto-applying would apply changes
    from a branch nobody has approved — which is the whole thing the workflow
    exists to prevent.

    Shared because three paths set the workflow — create, the workspace PATCH,
    and bulk update — and only the PATCH used to enforce this. Create ignored
    `vcs-workflow` entirely (#1763), so a configuration asking for
    `apply_then_merge` silently got the default and no one was told.
    """
    if workflow != "apply_then_merge":
        return
    if not has_vcs_connection:
        raise ValueError(
            "vcs-workflow 'apply_then_merge' requires a VCS connection — "
            "configure the workspace's VCS settings first"
        )
    if auto_apply:
        raise ValueError(
            "vcs-workflow 'apply_then_merge' is incompatible with auto-apply — "
            "set auto-apply to false in the same request"
        )


# ── Settings that three or more paths write (#1763) ──────────────────────
#
# These moved out of `routers/tfe_v2` when bulk update gained them. They were
# private helpers on the one router that happened to need them first, which is
# the shape `services.parallelism` warns about: the next caller re-implements
# the rule, or skips it.

MAX_TRIGGER_PREFIXES = 20
MAX_DRIFT_IGNORE_RULES = 50
MAX_DRIFT_IGNORE_RULE_LENGTH = 500
MAX_SLACK_CHANNEL = 128

_DRIFT_IGNORE_RULE_RE = re.compile(r"^[A-Za-z0-9_*.\-\[\]\"]+$")


def sanitize_working_directory(raw: str) -> str:
    """Strip leading/trailing slashes, reject traversal."""
    v = raw.strip().strip("/")
    if ".." in v:
        raise ValueError("working-directory: path traversal not allowed")
    return v


def validate_trigger_prefixes(raw: object) -> list[str]:
    """Each entry is normalised like a working directory; at most 20."""
    if not isinstance(raw, list):
        raise ValueError("trigger-prefixes must be a list of strings")
    if len(raw) > MAX_TRIGGER_PREFIXES:
        raise ValueError(f"trigger-prefixes: maximum {MAX_TRIGGER_PREFIXES} entries")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("trigger-prefixes entries must be strings")
        v = sanitize_working_directory(entry)
        if not v:
            raise ValueError("trigger-prefixes entries must be non-empty")
        result.append(v)
    return result


def validate_drift_ignore_rules(raw: object) -> list[str]:
    """Glob-aware address-plus-attribute-path strings (#482).

    The character set is deliberately narrow, and the length cap is what keeps
    a legitimate deep path expressible without allowing unbounded growth.
    """
    if not isinstance(raw, list):
        raise ValueError("drift-ignore-rules must be a list of strings")
    if len(raw) > MAX_DRIFT_IGNORE_RULES:
        raise ValueError(f"drift-ignore-rules: maximum {MAX_DRIFT_IGNORE_RULES} entries")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("drift-ignore-rules entries must be strings")
        v = entry.strip()
        if not v:
            raise ValueError("drift-ignore-rules entries must be non-empty")
        if len(v) > MAX_DRIFT_IGNORE_RULE_LENGTH:
            raise ValueError(
                f"drift-ignore-rules entries must be ≤ {MAX_DRIFT_IGNORE_RULE_LENGTH} characters"
            )
        if not _DRIFT_IGNORE_RULE_RE.match(v):
            raise ValueError(
                "drift-ignore-rules entries may only contain letters, digits, "
                "underscores, hyphens, dots, brackets, asterisks, and double quotes"
            )
        result.append(v)
    return result


def validate_plan_expiry_seconds(raw: object) -> int | None:
    """A plan-expiry TTL (#646). None / 0 disables it and stores NULL."""
    if raw is None:
        return None
    try:
        seconds = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("plan-expiry-seconds must be an integer") from None
    if seconds < 0:
        raise ValueError("plan-expiry-seconds must not be negative")
    return seconds or None


def clamp_drift_interval(raw: object) -> int:
    """Clamp to the deployment's configured minimum."""
    from terrapod.config import settings

    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("drift-detection-interval-seconds must be an integer") from None
    return max(value, settings.drift_detection.min_workspace_interval_seconds)


def validate_terragrunt_version(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("terragrunt-version must be a non-empty string")
    return raw.strip()


def validate_slack_channel(raw: object) -> str:
    """Empty means silent for this workspace (#556)."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("slack-channel must be a string")
    return raw.strip()[:MAX_SLACK_CHANNEL]


def validate_bool(raw: object, field: str) -> bool:
    """Type-check rather than coerce.

    `bool("false")` is True, so a JSON string sails through as an enable AND
    writes the string itself into a Boolean column. A caller who typed the
    value wrong gets told, instead of getting the opposite of what they asked
    for (#1301).
    """
    if not isinstance(raw, bool):
        raise ValueError(f"{field} must be true or false, not a string or number")
    return raw
