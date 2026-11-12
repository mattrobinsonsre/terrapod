"""A bounded, redacted list of the resources a plan changes, for the PR comment.

The status comment reports THAT three resources change. A reviewer typing
`terrapod apply` in the pull request also needs to know WHICH three, and what
changes about them, without leaving the PR. This builds that answer once, when
the run records its plan JSON, and the comment renders it later.

**Why a structured list and not the log.** The run page already serves the log
whole, with authentication and RBAC, so a comment can only ever be a lossy copy
of it. And the log is deliberately not masked (see `summary_redaction`), which
is safe only while it stays inside the deployment. A pull request is the second
place a run's artifacts leave it, to an audience that on a public repository is
everyone. A structured list can honour Terraform's own sensitive markers, which
a log dump cannot.

**Bounded by construction.** The list is capped when it is recorded, not when it
is rendered: at most `MAX_ENTRIES` resources, `MAX_ATTRIBUTES` attributes per
update, and `MAX_VALUE_LENGTH` characters per value, with a count of what was
left out. The payload therefore cannot be large and then truncated, whatever the
plan's size.

**Redaction.** Every value passes through the same two passes the AI summariser
uses, for the same reason. Terraform marks sensitive attributes in
`before_sensitive` / `after_sensitive`, which comes from the provider schema and
is authoritative, so a marked value is hidden wherever the marker points, with no
length floor. Markers are not sufficient on their own: a value copied from a
sensitive input into an attribute the provider never marked carries no marker,
so the values the plan marks anywhere are also matched wherever they appear, and
so are the workspace's own sensitive variable values. Redaction runs on the
values AND the attribute paths, since a map key can be a secret too.

Terraform and OpenTofu only. A Pulumi preview digest has no resource addresses,
so `build_resource_list` returns None for it and the comment shows no list.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from terrapod.services.summary_redaction import (
    PLACEHOLDER,
    _redact_by_markers,
    marked_values,
)

#: What stands in for an attribute that does not exist yet.
KNOWN_AFTER_APPLY = "(known after apply)"

#: The floor `summary_redaction.collect_literals` applies, and the reason this
#: module does not use it. 8 characters is a judgement made for a prompt that
#: stays inside a deployment; on a pull request a short secret is not acceptable,
#: so every non-empty literal is hidden, however short. The cost is occasionally
#: hiding a harmless value that equals a short secret, which is the right way to
#: be wrong here.


#: How many resources one run records. The comment shows them under the workspace
#: it belongs to, so this is also what a PR with many workspaces multiplies.
MAX_ENTRIES = 25

#: How many changed attributes one updated resource records.
MAX_ATTRIBUTES = 8

#: Characters kept of one value. Long values (a policy document, a user-data
#: script) are what make a plan unreadable in a comment.
MAX_VALUE_LENGTH = 120

_TRUNCATED = "…"


def _action(actions: Any) -> str | None:
    """`create`, `update`, `delete`, `replace`, or None for no-op / read / unknown."""
    if not isinstance(actions, list):
        return None
    action_set = {a for a in actions if isinstance(a, str)}
    if len(action_set) == 2 and {"create", "delete"} <= action_set:
        return "replace"
    if action_set == {"create"}:
        return "create"
    if action_set == {"update"}:
        return "update"
    if action_set == {"delete"}:
        return "delete"
    return None


def _union_markers(a: Any, b: Any) -> Any:
    """The markers of two sides merged, so a value is hidden if EITHER marks it.

    Terraform normally marks both sides, but a marker on one side only is
    possible (an attribute that became sensitive, or stopped being), and the
    side with no marker would then show its value in the clear.
    """
    if a is True or b is True:
        return True
    if isinstance(a, dict) or isinstance(b, dict):
        da = a if isinstance(a, dict) else {}
        db = b if isinstance(b, dict) else {}
        return {k: _union_markers(da.get(k), db.get(k)) for k in set(da) | set(db)}
    if isinstance(a, list) or isinstance(b, list):
        la = a if isinstance(a, list) else []
        lb = b if isinstance(b, list) else []
        return [
            _union_markers(la[i] if i < len(la) else None, lb[i] if i < len(lb) else None)
            for i in range(max(len(la), len(lb)))
        ]
    return None


def _redacted(value: Any, markers: Any) -> Any:
    """`value` with everything `markers` flags as sensitive replaced.

    Applied to EACH SIDE before the two are compared, which is what makes a
    change of shape safe. Comparing first and redacting after meant that a block
    added where there was none (`before: []`, `after: [{...}]`), or a dict added
    where there was `null`, was emitted as one whole container and the markers
    were never consulted. Redacting first leaves the placeholder in the
    container, so whatever the diff reports can only contain placeholders where
    Terraform marked a value. The markers passed in are the UNION of both sides'.
    """
    return _redact_by_markers(value, markers) if markers is not None else value


def _unknown_paths(unknown: Any, prefix: str, out: set[str]) -> None:
    """The dotted paths `after_unknown` flags as known only after apply."""
    if unknown is True:
        out.add(prefix)
    elif isinstance(unknown, dict):
        for key, sub in unknown.items():
            _unknown_paths(sub, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(unknown, list):
        for i, sub in enumerate(unknown):
            _unknown_paths(sub, f"{prefix}[{i}]", out)


def _walk(
    before: Any,
    after: Any,
    markers: Any,
    prefix: str,
    out: list[tuple[str, Any, Any]],
) -> None:
    """Collect one `(path, before, after)` per leaf that differs.

    The comparison runs on the REAL values, and `markers` (the union of both
    sides', see `_union_markers`) decides only what is DISPLAYED. A node marked
    sensitive is reported as the placeholder on both sides when its value
    differs, and is not descended into: that it CHANGED is worth a reviewer's
    attention and says nothing about the value, whereas descending would put a
    child's name or a fragment of its value into the output. Nothing under a
    marked node is ever read after the comparison.

    A change of shape (a block added where there was none) is handled the same
    way, because the markers are consulted before anything is emitted. Emitting
    the container first and redacting afterwards is what used to let an added
    block's sensitive attribute through.
    """
    if before == after:
        return
    if markers is True:
        out.append((prefix or "(value)", PLACEHOLDER, PLACEHOLDER))
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            _walk(
                before.get(key),
                after.get(key),
                markers.get(key) if isinstance(markers, dict) else None,
                f"{prefix}.{key}" if prefix else str(key),
                out,
            )
        return
    if isinstance(before, list) and isinstance(after, list):
        for i in range(max(len(before), len(after))):
            _walk(
                before[i] if i < len(before) else None,
                after[i] if i < len(after) else None,
                markers[i] if isinstance(markers, list) and i < len(markers) else None,
                f"{prefix}[{i}]",
                out,
            )
        return
    out.append((prefix or "(value)", _redacted(before, markers), _redacted(after, markers)))


class _Scrubber:
    """Removes known secrets from short strings, quickly, however many there are.

    A plan whose attributes are mostly marked sensitive yields a literal for every
    marked value, so tens of thousands on a large one. Two obvious ways to apply
    them both fail at that size: a `str.replace` per literal per string is
    O(strings x literals) and held the interpreter for seconds, and compiling one
    alternation of all of them costs more than the scrubbing it replaces.

    What is rendered is small and bounded (25 entries, 8 attributes, 120
    characters), so the cheap question is the reverse one: which literals occur
    in THIS string? Every substring a literal could match is at most
    `max_len` long, so the candidates are found by looking substrings up in a set,
    and only those are replaced. The result is identical to replacing every
    literal, longest first.
    """

    def __init__(self, literals: list[str]) -> None:
        self.literals = set(literals)
        self.lengths = sorted({len(x) for x in literals}, reverse=True)

    def __bool__(self) -> bool:
        return bool(self.literals)

    def hits(self, text: str) -> list[str]:
        found: set[str] = set()
        n = len(text)
        for length in self.lengths:
            if length > n:
                continue
            for i in range(n - length + 1):
                piece = text[i : i + length]
                if piece in self.literals:
                    found.add(piece)
        return sorted(found, key=len, reverse=True)


def _compile(literals: list[str]) -> _Scrubber | None:
    return _Scrubber(literals) if literals else None


def _scrub(text: str, scrubber: _Scrubber | None) -> str:
    """Remove every known secret from `text`, longest first, sparing placeholders.

    Longest first matters: when one secret contains another, replacing the
    shorter one first leaves the longer one's remaining characters exposed
    beside a placeholder, which looks handled and is not.

    **The placeholder is split out before matching and rejoined after.** With no
    length floor, a one-character secret is a literal, and matching it inside the
    text already redacted would rewrite the placeholder itself (`(sensitive
    value)` has a `v` in it) and leave a fragment of the marker where the secret
    was. Whatever is already the placeholder is not a place a secret can be.
    """
    if not scrubber:
        return text
    pieces = text.split(PLACEHOLDER)
    for i, piece in enumerate(pieces):
        for secret in scrubber.hits(piece):
            piece = piece.replace(secret, PLACEHOLDER)
        pieces[i] = piece
    return PLACEHOLDER.join(pieces)


def _render_value(value: Any, literals: _Scrubber | None, unknown: bool = False) -> str:
    """A compact one-line form of a leaf value, scrubbed and THEN capped.

    The order is the point. Capping first cut a secret that straddled the limit
    in half, and the half no longer matched the literal, so its first characters
    reached the comment. Scrubbing the whole value first means the cap can only
    ever cut the placeholder.
    """
    if unknown:
        return KNOWN_AFTER_APPLY
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = _scrub(text, literals)
    if len(text) > MAX_VALUE_LENGTH:
        return text[:MAX_VALUE_LENGTH] + _TRUNCATED
    return text


def _literals(values: Iterable[str]) -> list[str]:
    """The secrets to match anywhere, longest first, with no length floor."""
    return sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True)


async def strict_sensitive_literals(db: Any, run: Any) -> list[str]:
    """Every value that must never appear in a comment, or RAISE.

    A strict sibling of `summariser._sensitive_literals`, which is best-effort on
    purpose: it backs a redaction of the prompt for a summary that is better late
    or partial than absent, so it logs a failure and returns `[]`. That is the
    wrong behaviour here. The list carries values, a pull request makes them
    public, and "could not read the secrets" must mean "show nothing", not "show
    everything the plan did not mark". So this lets a failure propagate, and the
    caller records no list.

    Like the summariser's, it is deliberately PHASE-BLIND (#2189): an apply-only
    secret must still be hidden if it ever reaches an attribute. It matches that
    resolver's inputs exactly, the workspace's sensitive variables and this run's
    own `-var` values (which carry no `sensitive` flag, since `go-tfe` has none,
    and so are scrubbed unconditionally), and differs only in not swallowing.
    """
    from terrapod.services import variable_service

    resolved = await variable_service.resolve_variables(db, run.workspace_id)
    literals = [v.value for v in resolved if v.sensitive and v.value]
    raw = getattr(run, "run_variables", None)
    if raw:
        entries = variable_service.decoded_run_variables(raw)
        # `decoded_run_variables` returns [] for an unreadable envelope. For
        # delivery that is safe (deliver nothing); here it would mean redacting
        # nothing, so a non-empty column that decodes to nothing is a failure.
        if not entries:
            raise ValueError("this run's -var snapshot could not be decoded")
        literals.extend(e["value"] for e in entries if e["value"])
    return literals


def build_resource_list(raw: bytes, extra_secrets: Iterable[str] = ()) -> dict | None:
    """The list to record for a run, or None when the payload is not a Terraform plan.

    None means "unknown", which the caller records as nothing, so the comment
    never claims a plan changes nothing when it simply could not be read. A plan
    with no changes is an empty list, which is a different and true answer.

    `extra_secrets` are literal values to hide wherever they appear, in addition
    to the ones the plan itself marks: the workspace's own sensitive variables.
    """
    try:
        plan = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(plan, dict):
        return None
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        return None

    literals = _compile(_literals([*marked_values(raw), *extra_secrets]))

    entries: list[dict[str, Any]] = []
    total = 0
    for rc in changes:
        if not isinstance(rc, dict):
            continue
        address = rc.get("address")
        change = rc.get("change")
        if not isinstance(address, str) or not address or not isinstance(change, dict):
            continue
        action = _action(change.get("actions"))
        if action is None:
            continue
        total += 1
        if len(entries) >= MAX_ENTRIES:
            continue

        attributes: list[dict[str, Any]] = []
        omitted_attributes = 0
        if action == "update":
            markers = _union_markers(change.get("before_sensitive"), change.get("after_sensitive"))
            leaves: list[tuple[str, Any, Any]] = []
            _walk(change.get("before"), change.get("after"), markers, "", leaves)
            unknown: set[str] = set()
            _unknown_paths(change.get("after_unknown"), "", unknown)
            omitted_attributes = max(0, len(leaves) - MAX_ATTRIBUTES)
            for path, b, a in leaves[:MAX_ATTRIBUTES]:
                attributes.append(
                    {
                        "path": _scrub(path, literals),
                        "before": _render_value(b, literals),
                        "after": _render_value(a, literals, unknown=path in unknown),
                    }
                )

        importing = change.get("importing")
        entries.append(
            {
                "address": _scrub(address, literals),
                "action": action,
                "attributes": attributes,
                "attributes_omitted": omitted_attributes,
                "imported": isinstance(importing, dict) and bool(importing.get("id")),
            }
        )

    return {"entries": entries, "omitted": total - len(entries)}
