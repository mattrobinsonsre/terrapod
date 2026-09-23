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

`validate_scan_enforcement` is the one that takes an engine, and it is the
reason bulk-update cannot validate this setting from the payload alone — see
`routers/workspace_bulk._reject_scan_enforcement_on_unscannable_engines`.
"""

from __future__ import annotations

SCAN_ENFORCEMENTS = frozenset({"off", "advisory", "enforced"})
SCAN_ENGINES = frozenset({"checkov", "trivy", "both"})
SCAN_SEVERITY_THRESHOLDS = frozenset({"critical", "high", "medium", "low"})
AI_SUMMARY_MODES = frozenset({"default", "enabled", "disabled"})

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


def validate_scan_enforcement(raw: object, engine: str, default: str = "advisory") -> str:
    """`security-scan-enforcement`, refused where the engine is never scanned (#1567).

    Checkov and Trivy read Terraform plan JSON, so a Pulumi run has nothing to
    scan. `enforced` there held every apply for a result that never came, and
    `advisory` would record a setting that does nothing while saying it did
    something. Such a workspace defaults to, and accepts only, `off`.
    """
    from terrapod.engines import evaluates_security_scans

    scannable = evaluates_security_scans(engine)
    if not scannable:
        default = "off"
    value = _enum(raw, SCAN_ENFORCEMENTS, "security-scan-enforcement", default)
    if value != "off" and not scannable:
        raise ValueError(
            f"security-scan-enforcement must be off for a {engine} workspace: "
            "security scanning reads Terraform plan JSON and is not available "
            f"for {engine} runs yet"
        )
    return value


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


def validate_ai_summary_context(raw: object) -> str:
    ctx = "" if raw is None else raw
    if not isinstance(ctx, str):
        raise ValueError("ai-summary-context must be a string")
    if len(ctx) > MAX_AI_SUMMARY_CONTEXT:
        raise ValueError(f"ai-summary-context max length is {MAX_AI_SUMMARY_CONTEXT} characters")
    return ctx
