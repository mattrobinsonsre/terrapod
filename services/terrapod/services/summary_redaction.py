"""Removing secrets from what the AI summariser sends to the model.

GHSA-5mpc-79pv-6mq7. The summariser is the one place Terrapod hands a run's
artifacts to a **third party** — whatever model endpoint the operator has
configured, which may be a vendor API outside their own network. Everything else
that touches these artifacts keeps them inside the deployment.

So redaction belongs **here, on the path to the model, and nowhere else.** The
stored artifacts stay whole on purpose:

* **The plan JSON artifact must not be redacted.** Terraform deliberately does
  not redact `-json`; HCP Terraform protects it with a short-lived authenticated
  URL instead, which is what capability URLs now do for us. Redacting it at
  upload would also silently break drift detection — `_diff_paths` opens with
  `if before == after: return []`, so two equal placeholders produce no diff and
  the run is reported as having no drift, with no path back to `"drifted"`.
* **The logs must not be redacted at rest** either; they are what an operator
  reads to debug their own run.

Two mechanisms, because the artifacts are not the same shape:

**Structural, for plan JSON.** Terraform states which attributes are sensitive,
in `before_sensitive` / `after_sensitive` (and `sensitive_values` on state-shaped
payloads), as a parallel structure of markers alongside the real values. Those
markers are the authoritative signal — they come from the provider schema, so
they cover attributes no one here knows are secret. `_clean_plan_json_bytes`
already strips `prior_state`, and a guard test used to conclude that this kept
sensitive values out of the prompt. It does not: measured, a changing resource
keeps its `change.after` value in the clear, which is the whole finding.

**Literal, for the logs and the code.** A log is text, and `code_diff` /
`code_context` are `*.tf` and `*.tfvars` source, so there is no marker to read.
The only thing to match on is the value itself, taken from the workspace's own
variables marked sensitive.

**Short values are deliberately not redacted.** A sensitive variable whose value
is `true`, `1` or `prod` would match everywhere, and a summary with half its
words blacked out is useless — the operator stops reading it, which costs more
than the value protects. The floor is a judgement, not a guarantee: literal
redaction is a backstop over the structural pass, not the primary control.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

#: What replaces a redacted value. Matches the phrasing terraform itself prints,
#: so the model reads it as "a value was withheld" rather than as data.
PLACEHOLDER = "(sensitive value)"

#: Below this length a secret is not redacted from free text — see the module
#: docstring. Structural redaction has no such floor and is unaffected.
MIN_LITERAL_LENGTH = 8


def _redact_by_markers(value: Any, markers: Any) -> Any:
    """Replace the parts of `value` that `markers` flags as sensitive.

    Terraform's marker structure mirrors the value: `True` marks the whole node,
    a dict marks named children, a list marks elements by position. Anything
    else means "not sensitive here" and the value passes through.
    """
    if markers is True:
        return PLACEHOLDER
    if isinstance(markers, dict) and isinstance(value, dict):
        return {k: _redact_by_markers(v, markers.get(k)) for k, v in value.items()}
    if isinstance(markers, list) and isinstance(value, list):
        return [
            _redact_by_markers(v, markers[i] if i < len(markers) else None)
            for i, v in enumerate(value)
        ]
    return value


def _redact_change(change: Any) -> None:
    """Redact one `change` block in place, both directions."""
    if not isinstance(change, dict):
        return
    for side in ("before", "after"):
        markers = change.get(f"{side}_sensitive")
        if markers is not None and side in change:
            change[side] = _redact_by_markers(change[side], markers)


def redact_plan_json(raw: bytes) -> bytes:
    """Redact every value terraform marked sensitive. Best-effort.

    Returns the input unchanged when it will not parse — matching the
    surrounding cleaner, which never fails the call. That is safe here only
    because an unparseable payload is also one the model can make nothing of;
    it is NOT a licence to swallow a parse failure on a payload we would still
    send.
    """
    try:
        plan = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw
    if not isinstance(plan, dict):
        return raw

    for key in ("resource_changes", "resource_drift", "drift_observed_no_apply_action"):
        entries = plan.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    _redact_change(entry.get("change"))

    # Output changes carry the same marker pair, and an output is exactly the
    # sort of thing that is sensitive (a generated password, a connection
    # string). Their shape is `{"name": {...change...}}`.
    outputs = plan.get("output_changes")
    if isinstance(outputs, dict):
        for change in outputs.values():
            _redact_change(change)

    # State-shaped payloads mark sensitivity as `sensitive_values` alongside
    # `values`, rather than the before/after pair.
    _redact_state_values(plan)

    return json.dumps(plan).encode()


def _redact_state_values(node: Any) -> None:
    """Walk any nested `values` / `sensitive_values` pairs, redacting in place."""
    if isinstance(node, dict):
        markers = node.get("sensitive_values")
        if markers is not None and "values" in node:
            node["values"] = _redact_by_markers(node["values"], markers)
        for value in node.values():
            _redact_state_values(value)
    elif isinstance(node, list):
        for item in node:
            _redact_state_values(item)


def collect_literals(values: Iterable[str]) -> list[str]:
    """The secrets worth matching on, longest first.

    Longest first matters: when one secret contains another, replacing the
    shorter one first leaves the longer one's remaining characters exposed
    beside a placeholder, which is arguably worse than not redacting at all
    because it looks handled.
    """
    usable = {v for v in values if v and len(v) >= MIN_LITERAL_LENGTH}
    return sorted(usable, key=len, reverse=True)


def redact_text(text: str, literals: Iterable[str]) -> str:
    """Replace each literal secret wherever it appears in `text`."""
    if not text:
        return text
    for secret in collect_literals(literals):
        text = text.replace(secret, PLACEHOLDER)
    return text
