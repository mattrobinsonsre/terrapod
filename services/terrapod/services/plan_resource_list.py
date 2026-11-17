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
update, `MAX_VALUE_LENGTH` characters per value and `MAX_NAME_LENGTH` per address
or attribute path, with a count of what was left out. The payload therefore
cannot be large and then truncated, whatever the plan's size.

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
import math
import re
from collections.abc import Iterable
from typing import Any

from terrapod.logging_config import get_logger
from terrapod.services.summary_redaction import (
    PLACEHOLDER,
    _redact_by_markers,
    marked_values,
)

logger = get_logger(__name__)

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

#: Characters kept of a resource address or an attribute path.
MAX_NAME_LENGTH = 200

#: Scan steps one build may spend hiding secrets; past it everything is hidden.
MAX_SCRUB_STEPS = 1_000_000

#: Most registered secrets and escaped forms in one build; past it everything is hidden.
MAX_LITERALS = 100_000

#: Rounds of JSON escaping registered per secret. A deeper round leaves 2**depth backslashes in a row;
#: while any secret is registered, a value, key, path or address holding such a run is hidden whole.
MAX_ESCAPE_DEPTH = 4

#: A secret longer than this gets no escaped forms, so one that JSON would escape hides everything.
MAX_FORM_LENGTH = 64 * 1024

#: Most escaped forms registered for one secret; past it everything is hidden.
MAX_FORMS = 1024

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
    if isinstance(a, dict) and isinstance(b, list) or isinstance(a, list) and isinstance(b, dict):
        return True  # The sides disagree on shape, so hide the whole node.
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


#: A position in a change: None at the top, else `(parent, dict key or list index)`.
_Node = tuple[Any, str | int] | None


def _segments(node: _Node) -> list[str | int]:
    segments: list[str | int] = []
    while node is not None:
        node, segment = node
        segments.append(segment)
    segments.reverse()
    return segments


def _path(node: _Node) -> str:
    """The dotted path to `node`, built only for a leaf that is recorded."""
    parts: list[str] = []
    written = False
    for segment in _segments(node):
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
            written = True
        else:
            key = str(segment)
            if written:
                parts.append(".")
            parts.append(key)
            written = written or bool(key)
    return "".join(parts) or "(value)"


def _is_unknown(unknown: Any, node: _Node) -> bool:
    """Whether `after_unknown` flags the value at `node` as known only after apply."""
    for segment in _segments(node):
        if isinstance(segment, int):
            unknown = (
                unknown[segment] if isinstance(unknown, list) and segment < len(unknown) else None
            )
        else:
            unknown = unknown.get(segment) if isinstance(unknown, dict) else None
    return unknown is True


def _walk(
    before: Any,
    after: Any,
    markers: Any,
    node: _Node,
    out: list[tuple[_Node, Any, Any]],
) -> None:
    """Collect one `(node, before, after)` per leaf that differs, comparing each leaf once.

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
    if markers is True:
        if before != after:
            out.append((node, PLACEHOLDER, PLACEHOLDER))
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            _walk(
                before.get(key),
                after.get(key),
                markers.get(key) if isinstance(markers, dict) else None,
                (node, key),
                out,
            )
        return
    if isinstance(before, list) and isinstance(after, list):
        for i in range(max(len(before), len(after))):
            _walk(
                before[i] if i < len(before) else None,
                after[i] if i < len(after) else None,
                markers[i] if isinstance(markers, list) and i < len(markers) else None,
                (node, i),
                out,
            )
        return
    if before != after:
        out.append((node, _redacted(before, markers), _redacted(after, markers)))


class _Budget:
    """Scan steps left for one build; a spent budget hides everything after it."""

    def __init__(self, remaining: float) -> None:
        self.remaining = remaining
        self.exhausted = False
        self.reason: str | None = None

    def fail(self, reason: str) -> None:
        """Spend what is left, recording the first cap that ran out."""
        if not self.exhausted:
            self.reason = reason
        self.remaining = min(self.remaining, 0)
        self.exhausted = True


def _shared(label: str, text: str, pos: int) -> int:
    """Length of the common prefix of `label` and `text[pos:]`."""
    if text.startswith(label, pos):
        return len(label)
    lo, hi = 0, min(len(label), len(text) - pos)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text.startswith(label[:mid], pos):
            lo = mid
        else:
            hi = mid - 1
    return lo


class _Scrubber:
    """Finds known secrets by walking a radix trie: first char -> (label, child or None)."""

    def __init__(self, literals: list[str], budget: _Budget | None = None) -> None:
        self.size = len(literals)
        self.trie: dict[str, Any] = {}
        self.too_deep = "\\" * 2**MAX_ESCAPE_DEPTH
        budget = budget or _Budget(math.inf)
        for literal in literals:
            if budget.remaining <= 0:
                break
            budget.remaining -= 1 + self._insert(literal) // 128
        if budget.remaining <= 0:
            budget.fail("steps")
        self.failed = budget.exhausted

    def _insert(self, literal: str) -> int:
        """Add `literal`, splitting an edge once at the common prefix; returns the chars handled."""
        node = self.trie
        pos = 0
        handled = len(literal)
        while pos < len(literal):
            edge = node.get(literal[pos])
            if edge is None:
                node[literal[pos]] = (literal[pos:], None)
                break
            label, child = edge
            shared = _shared(label, literal, pos)
            if shared < len(label):
                handled += len(label)
                child = {label[shared]: (label[shared:], child)}
                node[literal[pos]] = (label[:shared], child)
            elif child is None:
                child = {"": True}
                node[literal[pos]] = (label, child)
            node = child
            pos += shared
        else:
            node[""] = True
        return handled

    def __bool__(self) -> bool:
        return self.size > 0 or self.failed

    def _longest(self, text: str, i: int, left: float) -> tuple[int, int]:
        """The longest literal length at `i` and the steps taken, walking at most `left`."""
        node = self.trie
        best = 0
        k = i
        steps = 1
        while steps <= left and k < len(text):
            edge = node.get(text[k])
            if edge is None:
                break
            label, child = edge
            if k + len(label) > len(text):
                steps += 1
                break
            # the C-speed label compare is charged one step per 128 characters
            steps += 1 + len(label) // 128
            if not text.startswith(label, k):
                break
            k += len(label)
            if child is None:
                best = k - i
                break
            if "" in child:
                best = k - i
            node = child
        return best, steps

    def _extend(self, text: str, i: int, end: int, left: float) -> tuple[int, float]:
        """End of the run of overlapping secrets that starts at `i` and reaches `end`."""
        j = i + 1
        while j < end:
            if left <= 0:
                return -1, left
            if text[j] in self.trie:
                length, steps = self._longest(text, j, left)
                left -= steps
                if left <= 0:
                    return -1, left
                end = max(end, j + length)
            else:
                left -= 1
            j += 1
        return end, left

    def mask(self, text: str, limit: int | None, budget: _Budget) -> tuple[str, bool]:
        """Hide runs of overlapping secrets; stop past `limit`; False if the budget ran out."""
        parts: list[str] = []
        produced = 0
        plain = i = 0
        n = len(text)
        left = budget.remaining
        ok = True
        while i < n and (limit is None or produced + i - plain <= limit):
            if left <= 0:
                ok = False
                break
            if text[i] not in self.trie:
                i += 1
                left -= 1
                continue
            length, steps = self._longest(text, i, left)
            left -= steps
            if left <= 0:
                ok = False
                break
            if not length:
                i += 1
                continue
            head = i - plain + len(PLACEHOLDER)
            if limit is not None and produced + head > limit:
                parts += [text[plain:i], PLACEHOLDER]
                plain = i = n
                break
            end, left = self._extend(text, i, i + length, left)
            if end < 0:
                ok = False
                break
            parts += [text[plain:i], PLACEHOLDER]
            produced += head
            i = plain = end
        budget.remaining = left
        parts.append(text[plain:i])
        if not ok:
            budget.fail("steps")
            parts.append(PLACEHOLDER)
        return "".join(parts), ok


def _compile(literals: list[str], budget: _Budget | None = None) -> _Scrubber | None:
    if not literals and not (budget and budget.exhausted):
        return None
    return _Scrubber(literals, budget)


def _scrub(
    text: str,
    scrubber: _Scrubber | None,
    limit: int | None = None,
    budget: _Budget | None = None,
) -> str:
    """Hide every occurrence of every known secret in `text`, overlaps included.

    Existing placeholders are split out first, so no secret can match inside one.
    """
    if not scrubber:
        return text
    budget = budget or _Budget(math.inf)
    out: list[str] = []
    produced = 0
    for index, piece in enumerate(text.split(PLACEHOLDER)):
        if index:
            out.append(PLACEHOLDER)
            produced += len(PLACEHOLDER)
        if limit is not None and produced > limit:
            break
        masked, ok = scrubber.mask(piece, None if limit is None else limit - produced, budget)
        out.append(masked)
        produced += len(masked)
        if not ok:
            break
    return "".join(out)


def _cap(text: str, scrubber: _Scrubber | None, limit: int, budget: _Budget | None = None) -> str:
    """`text` scrubbed and THEN cut to `limit` characters plus a marker.

    Cutting first would split a secret at the limit and let its first characters through.
    """
    text = _scrub(text, scrubber, limit, budget)
    return text[:limit] + _TRUNCATED if len(text) > limit else text


def _shallow(text: str, scrubber: _Scrubber | None) -> str:
    """`text`, or the placeholder when it is escaped more deeply than the registered forms."""
    return PLACEHOLDER if scrubber and scrubber.too_deep in text else text


def _scrubbed_tree(value: Any, scrubber: _Scrubber, limit: int, budget: _Budget) -> Any:
    """`value` with every string and dict key scrubbed, one step per node; the placeholder once spent."""
    budget.remaining -= 1
    if budget.remaining <= 0:
        budget.fail("steps")
        return PLACEHOLDER
    if isinstance(value, str):
        if scrubber.too_deep in value:
            return PLACEHOLDER
        return _scrub(value, scrubber, limit, budget) if value else value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = _scrubbed_tree(str(k), scrubber, limit, budget)
            out[key] = _scrubbed_tree(v, scrubber, limit, budget)
            if budget.exhausted:
                return PLACEHOLDER
        return out
    if isinstance(value, list):
        items = []
        for v in value:
            items.append(_scrubbed_tree(v, scrubber, limit, budget))
            if budget.exhausted:
                return PLACEHOLDER
        return items
    return value


def _render_value(
    value: Any, literals: _Scrubber | None, budget: _Budget, unknown: bool = False
) -> str:
    """A compact one-line form of a leaf value, scrubbed and capped."""
    if unknown:
        return KNOWN_AFTER_APPLY
    if isinstance(value, str):
        return _cap(_shallow(value, literals), literals, MAX_VALUE_LENGTH, budget)
    if literals:
        value = _scrubbed_tree(value, literals, MAX_VALUE_LENGTH, budget)
        if budget.exhausted:
            return PLACEHOLDER
    text = json.dumps(value, default=str, ensure_ascii=False)
    return _cap(text, literals, MAX_VALUE_LENGTH, budget)


#: Characters whose JSON or Go-JSON escaped form differs from the raw one (ASCII JSON writes DEL too).
_ENCODABLE = re.compile(r'["\\\x00-\x1f/<>&\u2028\u2029]|[^\x00-\x7e]')

# Matched from the start of a backslash run, so an escaped backslash before `u` is not an escape.
_HEX_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\u([0-9a-f]{4})")

_GO_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    ("\u2028", "\\u2028"),
    ("\u2029", "\\u2029"),
)

_CONTROL = re.compile("([\b\f])")

_HTML = re.compile("[<>&]")

_HEX_ESCAPED = re.compile("[^\b\t\n\f\r -~]")

# JSON never writes a raw NUL, so it can separate pieces encoded together in one call.
_PIECES = (('"\x00"\\b"\x00"', "\\u0008"), ('"\x00"\\f"\x00"', "\\u000c"))


class _OutOfBudget(Exception):
    """Raised while generating literals once the budget or a cap is spent; carries the cause."""


def _json(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


def _json_ascii(text: str) -> str:
    return json.dumps(text)[1:-1]


def _html(text: str) -> str:
    for raw, escaped in _GO_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _go(text: str) -> str:
    """Terraform's and OpenTofu's jsonencode (Go 1.22 on): JSON that also escapes < > &."""
    return _html(_json(text))


def _go_old(text: str) -> str:
    """Older Go also wrote backspace and form feed as \\u0008 and \\u000c."""
    out = json.dumps(_CONTROL.split(text), ensure_ascii=False, separators=("\x00", ":"))[2:-2]
    for piece, escaped in _PIECES:
        out = out.replace(piece, escaped)
    return _html(out)


def _go_ascii(text: str) -> str:
    return _html(_json_ascii(text))


def _slash(text: str) -> str:
    return _json(text).replace("/", "\\/")


def _upper_hex(text: str) -> str:
    return _HEX_ESCAPE.sub(lambda m: m[1] + "\\u" + m[2].upper(), _json_ascii(text))


def _flat(form: str) -> int:
    return 1 + len(form) // 32


def _per_control(form: str) -> int:
    return _flat(form) + form.count("\b") + form.count("\f")


def _per_escape(form: str) -> int:
    return _flat(form) + 4 * _json_ascii(form).count("\\u")


#: (encoder, cost in steps, pattern without which it repeats another encoder's output).
#: A step is 32 characters of C-speed work or one control piece; each upper-hex callback costs 4.
_ENCODERS = (
    (_json, _flat, None),
    (_json_ascii, _flat, None),
    (_go, _flat, None),
    (_go_old, _per_control, _CONTROL),
    (_go_ascii, _flat, _HTML),
    (_slash, _flat, None),
    (_upper_hex, _per_escape, _HEX_ESCAPED),
)


def _add_forms(secret: str, found: set[str], budget: _Budget) -> None:
    """Add `secret` and its escaped forms to `found`, charging each encoder run before it runs."""
    found.add(secret)
    if len(secret) > MAX_FORM_LENGTH:
        if _ENCODABLE.search(secret):
            raise _OutOfBudget("forms")
        return
    # A character grows less than 2**(depth + 3)-fold, so no real form is longer; one that is fails closed.
    bound = MAX_FORM_LENGTH << (MAX_ESCAPE_DEPTH + 3)
    seen = {secret}
    level = {secret}
    for _ in range(MAX_ESCAPE_DEPTH):
        deeper: set[str] = set()
        for form in sorted(level):
            if not _ENCODABLE.search(form):
                continue
            for encode, cost, needs in _ENCODERS:
                if needs and not needs.search(form):
                    continue
                budget.remaining -= cost(form)
                if budget.remaining <= 0:
                    raise _OutOfBudget("steps")
                out = encode(form)
                if out in seen:
                    continue
                if len(out) > bound:
                    raise _OutOfBudget("forms")
                seen.add(out)
                deeper.add(out)
                if len(seen) > MAX_FORMS + 1:
                    raise _OutOfBudget("forms")
                if len(found) + len(seen) > MAX_LITERALS:
                    raise _OutOfBudget("literals")
        level = deeper
    found |= seen


def _literals(values: Iterable[str], budget: _Budget | None = None) -> list[str]:
    """The secrets to match anywhere, in every form they can take, with no length floor."""
    budget = budget or _Budget(math.inf)
    found: set[str] = set()
    try:
        for value in values:
            if not isinstance(value, str) or not value:
                continue
            parts = {value}
            if PLACEHOLDER in value:
                parts |= {f for f in value.split(PLACEHOLDER) if f}
            for part in parts:
                _add_forms(part, found, budget)
            if len(found) > MAX_LITERALS:
                raise _OutOfBudget("literals")
    except _OutOfBudget as stop:
        budget.fail(stop.args[0])
    return sorted(found, key=lambda x: (-len(x), x))


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

    budget = _Budget(MAX_SCRUB_STEPS)
    literals = _compile(_literals([*marked_values(raw), *extra_secrets], budget), budget)

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
            leaves: list[tuple[_Node, Any, Any]] = []
            _walk(change.get("before"), change.get("after"), markers, None, leaves)
            after_unknown = change.get("after_unknown")
            omitted_attributes = max(0, len(leaves) - MAX_ATTRIBUTES)
            for node, b, a in leaves[:MAX_ATTRIBUTES]:
                path = _path(node)
                attributes.append(
                    {
                        "path": _cap(_shallow(path, literals), literals, MAX_NAME_LENGTH, budget),
                        "before": _render_value(b, literals, budget),
                        "after": _render_value(
                            a, literals, budget, unknown=_is_unknown(after_unknown, node)
                        ),
                    }
                )

        importing = change.get("importing")
        entries.append(
            {
                "address": _cap(_shallow(address, literals), literals, MAX_NAME_LENGTH, budget),
                "action": action,
                "attributes": attributes,
                "attributes_omitted": omitted_attributes,
                "imported": isinstance(importing, dict) and bool(importing.get("id")),
            }
        )

    if budget.exhausted:
        logger.warning("plan_resource_list.scrub_budget_exhausted", reason=budget.reason)
    return {"entries": entries, "omitted": total - len(entries)}
