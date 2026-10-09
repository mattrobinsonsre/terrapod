"""Resolve which audiences a run mints for, and whether that answer moved (#1901).

Two deployments-worth of configuration meet here: the operator's catalogue in
`api.config.auth.oidc_issuer.audiences`, and a workspace's own override. The
workspace is merged **over** the catalogue, so removing a workspace override
falls back to the catalogue rather than removing the target — which is the
defined way to stop overriding, and why an explicitly empty list is refused at
validation.

**The merge is per key, and replaces rather than appends.** A workspace that
overrides `vault` supplies that target's whole audience list. Appending would
make it impossible to *narrow* a target, which is the main reason a workspace
would override one at all.

**Lookup is specific-then-general**, so an aliased provider configuration can
have its own audiences without every workspace having to name every alias:
`vault.eu` is answered by an entry for `vault.eu` if there is one, and by
`vault` otherwise. The runner sends `provider[.alias]` because that is what it
can discover from `tofu graph` after `init`; it does not know or care which of
the two answered.

Nothing here is cloud-specific, deliberately. Any provider may be mapped to any
audience; the cloud-side trust policy is the gate, and a wrong mapping fails at
the cloud rather than being second-guessed here.
"""

from __future__ import annotations

from typing import Any

#: Separates a provider name from one alias, matching the `provider.alias` form
#: a configuration writes and `tofu graph` reports.
ALIAS_SEP = "."

#: Characters that are path-significant, because a target name becomes a
#: DIRECTORY NAME: the runner writes `<token dir>/<target>/token`. The sibling
#: checks in `workspace_settings.validate_oidc_audiences` are deliberately
#: conservative rather than a grammar -- a provider name is whatever the
#: configuration calls it, so inventing a pattern risks refusing a legitimate
#: key -- and that reasoning is right for everything except this. A name
#: carrying a separator or a parent reference is not a provider name under any
#: reading, and left alone `aws./../vault` resolves through the `aws` entry
#: (the lookup splits on the FIRST dot) and then lands an AWS-audienced token
#: at the path the operator's `vault` block reads. Enforced here, at the mint
#: request, and again in the runner before the join, because the runner is a
#: separate image that may be older or newer than the API.
_PATH_SIGNIFICANT = ("/", "\\", "\x00")


def unsafe_target_reason(name: str) -> str | None:
    """Why `name` cannot be used as a target, or None when it can.

    Returns a reason rather than raising so each caller can phrase its own
    error -- a 422 on a workspace write and a 400 on a mint read differently.
    """
    for ch in _PATH_SIGNIFICANT:
        if ch in name:
            return f"cannot contain {ch!r}"
    # `name in (".", "..")` alone: the dot-split test that used to sit beside it
    # could not fire. Splitting ON the separator means no element it yields can
    # contain one, so `"..".split(".")` is `["", "", ""]` and the clause was
    # unconditionally false for every input. The runner's twin carried the same
    # dead clause and dropped it; this copy is the other half of that pair.
    if name in (".", ".."):
        return "cannot be or contain a parent reference"
    return None


def _clean(raw: Any) -> dict[str, list[str]]:
    """Tolerate what the database and config can hold, without transforming it.

    Both sides are JSONB or operator-supplied YAML, so a malformed value is
    reachable without going through `validate_oidc_audiences` — a row written by
    an earlier shape, or a hand-edited `values.yaml`. Dropping a malformed entry
    rather than raising is deliberate here and only here: this runs on the mint
    path and inside run creation, and failing a run because some *other*
    provider's entry is a string would be a worse outcome than ignoring it. The
    write paths reject the same input loudly, which is where an operator finds
    out.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, list):
            continue
        entries = [v for v in value if isinstance(v, str) and v.strip()]
        if entries:
            out[key] = entries
    return out


def merge(catalogue: Any, override: Any) -> dict[str, list[str]]:
    """The effective mapping: the workspace's override over the deployment's.

    Per-key replacement. A key present in both takes the override's audiences
    whole; a key only in the catalogue is inherited; a key only in the override
    is added, because a workspace may name a target the catalogue does not
    (full flexibility is the decision — the trust policy is the gate).
    """
    resolved = _clean(catalogue)
    resolved.update(_clean(override))
    return resolved


def resolve_for_workspace(workspace: Any, *, settings: Any) -> dict[str, list[str]]:
    """The effective mapping for one workspace, from live configuration.

    Used at run creation to build the snapshot, and on the mint path to check
    that snapshot still holds. Not used to mint from directly — see
    `Run.oidc_audiences`.
    """
    catalogue = getattr(settings.auth.oidc_issuer, "audiences", None)
    return merge(catalogue, getattr(workspace, "oidc_audiences", None))


def audiences_for_target(resolved: Any, target: str) -> list[str] | None:
    """The audiences for one `provider[.alias]`, specific before general.

    `None` — not `[]` — when nothing answers, because the caller has to tell
    "this deployment mints nothing for that provider" (a 204, and the run falls
    through to the pool identity) from "it mints an empty set", which is not a
    state the write paths can produce.
    """
    table = _clean(resolved)
    if not target:
        return None
    if target in table:
        return list(table[target])
    if ALIAS_SEP in target:
        general = target.split(ALIAS_SEP, 1)[0]
        if general in table:
            return list(table[general])
    return None


def target_changed(snapshot: Any, live: Any, target: str) -> bool:
    """Has the answer for this one target moved since the run was created?

    The mint-path check, kept deliberately cheap: the runner asks for one target
    at a time, so this is one lookup on each side and a comparison — no map
    diff, and no need to know which targets the plan used.

    Order matters in the comparison, not just membership: the entries are what
    goes into `aud`, and a cloud matching on the first value would see a
    different token. Treating a reorder as unchanged would be a judgement about
    a cloud's matching behaviour that we are in no position to make.

    True also when a target that previously resolved now resolves to nothing, or
    the reverse — both are changes the apply must not paper over.
    """
    return audiences_for_target(snapshot, target) != audiences_for_target(live, target)


def changed_targets(snapshot: Any, live: Any, targets: list[str]) -> list[str]:
    """Every target in `targets` whose answer has moved.

    The confirm-path check, where a richer answer is free: it names what changed
    so an operator re-plans knowing why, before a Job exists. Scoped to the
    targets the plan actually minted for, so an unrelated catalogue edit — a new
    provider added, or a changed entry for a provider this run never used — does
    not refuse an apply that nothing has invalidated.
    """
    return [t for t in targets if target_changed(snapshot, live, t)]
