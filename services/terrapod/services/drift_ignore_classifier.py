"""Drift-result classifier for per-workspace ignore rules (#482).

A drift run reports `has_changes=True` when `tofu plan` produced any
non-empty diff. Historically that flips `drift_status` to `drifted`
directly. Some workspaces have legitimate persistent noise — managed
policy versions, IAM trust-policy ordering, attributes co-managed by
external systems (HPA-driven `replicas`, autoscaler-driven
`desired_capacity`). The HCL-level `lifecycle { ignore_changes }`
workaround changes apply semantics globally, not just drift.

`drift_ignore_rules` on a workspace lets the operator silence those
attributes for drift purposes only. This module turns a list of rule
strings + a plan JSON document into a "should this still count as
drift?" decision.

Rule grammar
============

Each rule is a single string of the form::

    <terraform-address>[.<attribute-path>]

`*` matches one segment (non-empty, non-`.`, non-`[`/`]`). `[*]`
matches any bracketed index — `[0]`, `["foo"]`, `[\"bar\"]`. The
resource address and attribute path live in one dotted/bracketed
namespace so callers don't have to know where the address ends and
the attribute begins; the matcher just regex-tests against
``address + "." + attribute_path``.

Examples::

    aws_iam_role.foo.tags.Environment
    aws_autoscaling_group.workers[*].desired_capacity
    module.eks*.argocd_cluster.*.config.tls_client_config.ca_data
    aws_iam_role.foo              # any change → whole resource ignored

Match semantics
===============

Two sources are evaluated: `resource_changes` (actionable planned
actions) and `resource_drift` (out-of-band changes detected during
refresh). For a drift-detection run the drift usually lives in
`resource_drift` — computed/read-only attribute drift (e.g. an EKS
`platform_version` AWS bumps out-of-band) plans no action, so
`resource_changes` are all `no-op`.

For each entry:

1. Compute the set of attribute paths whose value differs between
   `before` and `after` (recursive walk; lists indexed by `[i]`,
   dicts joined by `.`).
2. For `resource_drift` only, keep just the paths OpenTofu deems
   **relevant** (`relevant_attributes`) — the rest is refresh noise
   OpenTofu itself hides from the plan (computed timestamps,
   server-managed metadata) and must never flag drift or need a rule.
3. Build a candidate string for each diff path as ``<address>.<path>``.
   The address alone (no `.path` suffix) is a candidate for the "whole
   resource" rule shape.
4. If every counted diff path matches at least one rule, drop the
   entry. Otherwise the resource is still considered drifted.
5. Entries whose `change.actions` is `["no-op"]` or `["read"]` are
   never considered drift to begin with.

If every counted entry is dropped → drift was fully ignored; caller
sets `drift_status=no_drift`. Otherwise → `drifted`.

The classifier is deliberately stateless and pure: caller is
responsible for fetching the plan JSON (typically via the
`run_artifacts` storage helper) and persisting the resulting
`drift_status` change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Rule shape limits — applied at validation time, not here. The
# classifier trusts that its caller already enforced
# `validate_drift_ignore_rules` (in `tfe_v2.py`).

# Actions that constitute "actual" drift. `no-op` is plan's way of
# saying "no change at all" (it shouldn't appear in a has_changes
# plan, but be defensive). `read` is a data-source refresh, never
# drift.
_DRIFT_ACTIONS = frozenset({"create", "update", "delete", "replace"})

# Matches a numeric block index like `[0]`, `[12]`. Used to produce an
# index-tolerant candidate so a natural attribute-path rule matches the
# plan-JSON block-list shape. String-key indices (`["prod"]`) are NOT
# matched by this — they stay in the candidate.
_NUMERIC_INDEX_RE = re.compile(r"\[\d+\]")


def _rule_to_regex(rule: str) -> re.Pattern[str]:
    """Translate a glob rule into a compiled regex.

    Rules look like dotted/bracketed Terraform addresses extended with
    attribute paths. We anchor with `^…$` so a rule
    `aws_iam_role.foo` does not silently match `aws_iam_role.foo.tags`
    — the latter is a strict superset and the caller has to opt in by
    extending the rule. The reverse — bare address matches "any change
    to this resource" — is handled at the match site (see
    `_path_is_ignored`), not by the regex.

    Glob semantics:

    * `*` matches zero or more characters that are NOT `.` — so a
      single `*` can span across `[N]` index suffixes (the common
      case where `module.eks*` should match both `module.eks` and
      `module.eks_legacy[0]`) but cannot leak across a segment
      boundary into the next module/resource label.
    * `[*]` (inside literal brackets) is a special compound that
      matches any bracketed index expression — `[0]`, `["foo"]`,
      `["bar/baz"]`. This is the safe way to say "any one index"
      without accidentally matching surrounding text.

    `fnmatch.translate` would over-match: it treats `.` as a literal
    but accepts `*` as `.*` (cross-segment). The hand-rolled
    translation here is short enough to keep in source.
    """
    out: list[str] = ["^"]
    i = 0
    while i < len(rule):
        c = rule[i]
        if c == "*":
            # `[*]` is a special compound — matches any single bracketed
            # index expression. Handle it before the bare `*` case.
            if i > 0 and rule[i - 1] == "[" and i + 1 < len(rule) and rule[i + 1] == "]":
                # Already inside brackets. Emit a permissive
                # bracketed-content matcher and let the `]` literal be
                # emitted naturally on the next iteration. The leading
                # `[` was emitted as `\[` already.
                out.append(r"[^\]]+")
                i += 1
                continue
            # Bare `*` — zero or more characters that aren't `.`.
            # Matches across `[N]` index suffixes but never crosses a
            # segment boundary.
            out.append(r"[^.]*")
        elif c in ".[]":
            out.append(re.escape(c))
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _compile_rules(rules: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Compile rule strings into `(rule, regex)` pairs.

    Compiling once per drift-run-completed call is cheap (workspaces
    rarely hit double-digit rules); duplicating across plan resources
    inside the same call would be wasteful.

    Returns the original rule string alongside the regex so log lines
    can name the rule that matched without re-deriving it.
    """
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for rule in rules:
        rule = rule.strip()
        if not rule:
            continue
        try:
            compiled.append((rule, _rule_to_regex(rule)))
        except re.error as e:
            # Defensive: validation in tfe_v2 should catch this, but
            # logging here is the failsafe — the rule just won't match
            # anything rather than ablating drift detection entirely.
            logger.warning("Skipping invalid drift_ignore rule", rule=rule, error=str(e))
    return compiled


def _diff_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Recursive walk emitting one dotted path per leaf-level diff.

    Lists become `prefix[i]`; dicts become `prefix.k`. Equal values
    produce no path. `None` on one side and a structure on the other
    is treated as a value-level diff at `prefix`.

    Plan JSON's `before`/`after` can each be `null` (resource creation
    has `before=null`; deletion has `after=null`). For those whole-
    resource cases the caller treats the address as the diff "path",
    rather than walking — see `_resource_change_diff_paths`.
    """
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for k in set(before) | set(after):
            sub_prefix = f"{prefix}.{k}" if prefix else k
            paths.extend(_diff_paths(before.get(k), after.get(k), sub_prefix))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        paths = []
        n = max(len(before), len(after))
        for i in range(n):
            sub_prefix = f"{prefix}[{i}]"
            b = before[i] if i < len(before) else None
            a = after[i] if i < len(after) else None
            paths.extend(_diff_paths(b, a, sub_prefix))
        return paths
    # Type mismatch or leaf-level inequality — emit one path.
    return [prefix] if prefix else []


def _resource_change_diff_paths(rc: dict[str, Any]) -> tuple[list[str], bool]:
    """Return (diff_paths, is_whole_resource_action).

    For an update, walk before/after to enumerate the changed fields.
    For create/delete/replace, no granular path makes sense — the
    whole resource is the unit of change, so return an empty paths
    list with `is_whole_resource_action=True`. The match step then
    checks the bare address against the rule set.
    """
    change = rc.get("change") or {}
    actions = change.get("actions") or []

    # Defensive: plan JSON could carry novel action strings in future
    # tofu releases. Any action NOT in _DRIFT_ACTIONS is treated as
    # "not drift" — the safer default is to ignore than to spuriously
    # flag.
    if not any(a in _DRIFT_ACTIONS for a in actions):
        return [], False

    # create / delete / replace: whole resource is the change.
    if "create" in actions or "delete" in actions or "replace" in actions:
        return [], True

    # Update — diff before vs after.
    before = change.get("before")
    after = change.get("after")
    return _diff_paths(before, after), False


def _path_is_ignored(
    address: str,
    diff_path: str,
    is_whole_resource: bool,
    rules: list[tuple[str, re.Pattern[str]]],
) -> tuple[bool, str | None]:
    """Test a single (address, diff_path) tuple against compiled rules.

    Returns `(ignored, matching_rule)`. The matching rule string is
    returned for logging — the caller can attribute each suppressed
    change to a specific rule.

    For whole-resource actions (create / delete / replace) we match
    the bare address; rules with attribute suffixes never silence
    a delete because the operator probably wants to know if their
    workspace's resources are being recreated under them.
    """
    if is_whole_resource:
        # Only allow whole-resource silencing via bare-address rules.
        # A rule like "aws_iam_role.foo.tags.Environment" must NOT
        # silence a "delete" of aws_iam_role.foo — that would be a
        # genuinely surprising semantic. Operators can opt in by
        # adding the bare address as a rule.
        for rule_str, regex in rules:
            if regex.fullmatch(address):
                return True, rule_str
        return False, None

    # Per-attribute action: match against `<address>.<diff_path>` or
    # the bare address (for "whole resource" rule shape).
    candidate = f"{address}.{diff_path}" if diff_path else address
    # HCL nested blocks serialize as single-element lists in `tofu show
    # -json` output, so a block path like `config.tls_client_config.
    # ca_data` arrives as `config[0].tls_client_config[0].ca_data`. No
    # operator thinks in those terms — they write the attribute path the
    # way it reads in HCL. So we ALSO test the rule against a variant of
    # the candidate with numeric block indices stripped, letting a bare
    # `config.tls_client_config.ca_data` rule match the indexed path.
    # Only NUMERIC `[N]` indices are stripped; string keys (`["prod"]`,
    # for_each / map indices) stay because they're semantically
    # meaningful — a rule shouldn't accidentally span every for_each
    # instance.
    deindexed = _NUMERIC_INDEX_RE.sub("", candidate)
    for rule_str, regex in rules:
        if regex.fullmatch(candidate) or regex.fullmatch(deindexed) or regex.fullmatch(address):
            return True, rule_str
    return False, None


def _strip_indices(addr: str) -> str:
    """Drop numeric `[N]` indices so a `module.eks[0]...` drift address
    matches the un-indexed form OpenTofu uses in `relevant_attributes`."""
    return _NUMERIC_INDEX_RE.sub("", addr)


def _path_segments(path: str) -> list[str]:
    """Split a diff path (`metadata[0].resource_version`) into normalised
    segments (`["metadata", "resource_version"]`) — dotted keys split, numeric
    block indices dropped (they carry no meaning for relevance matching)."""
    segs: list[str] = []
    for part in path.split("."):
        part = _NUMERIC_INDEX_RE.sub("", part)
        if part:
            segs.append(part)
    return segs


def _relevant_attribute_index(plan_json: dict[str, Any]) -> dict[str, list[list[str]]]:
    """Index OpenTofu's `relevant_attributes` by (index-stripped) resource
    address → list of attribute segment-lists.

    `relevant_attributes` is OpenTofu's own record of which attributes are
    referenced by the configuration (outputs, other resources). It is exactly
    what OpenTofu uses to decide which out-of-band drift to *surface* in the
    plan versus hide as irrelevant refresh noise (computed timestamps,
    server-managed metadata, …). We reuse it as the relevance signal (#753)."""
    index: dict[str, list[list[str]]] = {}
    for ra in plan_json.get("relevant_attributes") or []:
        res = ra.get("resource")
        attr = ra.get("attribute")
        if not res or not attr:
            continue
        index.setdefault(_strip_indices(str(res)), []).append([str(x) for x in attr])
    return index


def _drift_path_is_relevant(address: str, path: str, relevant: dict[str, list[list[str]]]) -> bool:
    """True if a drifted attribute is one OpenTofu deems relevant.

    A drift path counts if it shares a common prefix with any relevant
    attribute of the same resource (bidirectional: the drift is at, above, or
    below a referenced attribute). Non-relevant drift — the refresh noise
    OpenTofu hides from the plan — is excluded before rule-matching, so it
    never flags drift and never needs an ignore rule (#753)."""
    rel_attrs = relevant.get(_strip_indices(address))
    if not rel_attrs:
        return False
    segs = _path_segments(path)
    if not segs:
        return False
    for rel_segs in rel_attrs:
        n = min(len(rel_segs), len(segs))
        if n and rel_segs[:n] == segs[:n]:
            return True
    return False


def _evaluate_entry(
    address: str,
    diff_paths: list[str],
    is_whole: bool,
    compiled: list[tuple[str, re.Pattern[str]]],
    suppressed: list[dict[str, Any]],
) -> bool:
    """Match one resource's diff against the rules. Appends to `suppressed`
    when fully ignored; returns True if the resource still counts as drift."""
    if is_whole:
        ignored, matching_rule = _path_is_ignored(address, "", True, compiled)
        if ignored:
            suppressed.append({"address": address, "paths": [], "rule": matching_rule})
            return False
        return True

    unsuppressed: list[str] = []
    matched_paths: list[str] = []
    matched_rule_for_resource: str | None = None
    for p in diff_paths:
        ignored, matching_rule = _path_is_ignored(address, p, False, compiled)
        if ignored:
            matched_paths.append(p)
            if matched_rule_for_resource is None:
                matched_rule_for_resource = matching_rule
        else:
            unsuppressed.append(p)

    if not unsuppressed:
        suppressed.append(
            {"address": address, "paths": matched_paths, "rule": matched_rule_for_resource}
        )
        return False
    return True


def classify_drift(
    plan_json: dict[str, Any],
    rules: Iterable[str],
) -> tuple[bool, list[dict[str, Any]]]:
    """Decide whether a drift run's plan still constitutes drift.

    Args:
        plan_json: parsed `tofu show -json` output for the drift run's
            plan. Expected shape includes `resource_changes: list[...]`
            per the OpenTofu plan-format spec.
        rules: workspace's `drift_ignore_rules` list.

    Returns:
        `(still_drifted, suppressed_changes)` where:
          * `still_drifted` is True if at least one change/drift has a
            diff that no rule matches.
          * `suppressed_changes` is a list of `{address, paths, rule}`
            dicts describing what got silenced. Useful for surfacing
            "ignored by policy" sections in the UI and for runbook
            tracing.

    Two sources of change are evaluated (#753):

      * `resource_changes` — actionable planned actions. Every diff path
        must be ignored for the resource to be suppressed.
      * `resource_drift` — out-of-band changes OpenTofu detected during
        refresh. For a drift-detection run this is where the drift lives
        (its `resource_changes` are typically all `no-op`, since computed/
        read-only attribute drift plans no action). Only drift on attributes
        OpenTofu deems **relevant** (`relevant_attributes`) is considered —
        the rest is refresh noise OpenTofu hides from the plan (computed
        timestamps, server-managed metadata) and must never flag drift or
        require an ignore rule.

    A workspace with no rules → `still_drifted=True` if any counted change/
    drift exists. Callers may short-circuit on empty rules; it's safe but
    wasteful.
    """
    rule_list = list(rules)
    compiled = _compile_rules(rule_list) if rule_list else []

    resource_changes = plan_json.get("resource_changes") or []
    resource_drift = plan_json.get("resource_drift") or []
    if not resource_changes and not resource_drift:
        return False, []

    relevant = _relevant_attribute_index(plan_json)
    suppressed: list[dict[str, Any]] = []
    still_drifted = False

    # Actionable planned changes: every diff path counts (OpenTofu would
    # apply them, so they are real regardless of relevance).
    for rc in resource_changes:
        address = rc.get("address") or ""
        if not address:
            continue
        diff_paths, is_whole = _resource_change_diff_paths(rc)
        if not diff_paths and not is_whole:
            # Resolves to nothing changing (no-op / before==after). Not drift.
            continue
        if _evaluate_entry(address, diff_paths, is_whole, compiled, suppressed):
            still_drifted = True

    # Out-of-band drift: keep only the attributes OpenTofu deems relevant;
    # the rest is refresh noise it hides from the plan.
    for rd in resource_drift:
        address = rd.get("address") or ""
        if not address:
            continue
        diff_paths, is_whole = _resource_change_diff_paths(rd)
        if is_whole:
            # A whole-resource drift (created/deleted outside) is always
            # material — never noise-filtered.
            if _evaluate_entry(address, [], True, compiled, suppressed):
                still_drifted = True
            continue
        rel_paths = [p for p in diff_paths if _drift_path_is_relevant(address, p, relevant)]
        if not rel_paths:
            # Only irrelevant refresh noise drifted → not drift.
            continue
        if _evaluate_entry(address, rel_paths, False, compiled, suppressed):
            still_drifted = True

    return still_drifted, suppressed
