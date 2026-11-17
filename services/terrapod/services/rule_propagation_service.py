"""Which autodiscovery-rule fields propagate to the workspaces the rule created."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import workspace_bulk
from terrapod.db.models import (
    AutodiscoveryRule,
    NotificationConfiguration,
    RunTask,
    Workspace,
)
from terrapod.services import pool_set, workspace_settings
from terrapod.services.audit_service import add_audit_events
from terrapod.services.workspace_settings import reconcile_default_run_kind

#: Rule attribute (API kebab-case) -> the bulk-update `update` key that applies it.
PROPAGATED_FIELDS: dict[str, str] = {
    "execution-mode": "execution-mode",
    "execution-backend": "execution-backend",
    "engine-version": "engine-version",
    "terraform-version": "engine-version",
    "agent-pool-id": "agent-pool-ids",
    "resource-cpu": "resource-cpu",
    "resource-memory": "resource-memory",
    "ee-resource-cpu": "ee-resource-cpu",
    "ee-resource-memory": "ee-resource-memory",
    "parallelism": "parallelism",
    "configure-parallelism": "configure-parallelism",
    "ansible-version": "ansible-version",
    "ansible-execution-environment": "ansible-execution-environment",
    "ansible-collections": "ansible-collections",
    "plan-apply-enabled": "plan-apply-enabled",
    "configure-enabled": "configure-enabled",
    "default-run-kind": "default-run-kind",
    "auto-configure-enabled": "auto-configure-enabled",
    "configure-all-hosts": "configure-all-hosts",
    "configure-after-apply": "configure-after-apply",
    "ansible-host-key-checking": "ansible-host-key-checking",
    "dynamic-inventory-enabled": "dynamic-inventory-enabled",
    "inventory-scripts-enabled": "inventory-scripts-enabled",
    "config-directory": "config-directory",
    "inventory-paths": "inventory-paths",
    "default-playbook": "default-playbook",
    "default-limit": "default-limit",
    "playbooks": "playbooks",
    "hard-limit": "hard-limit",
    "oidc-audiences": "oidc-audiences",
    "auto-apply-mode": "auto-apply-mode",
    "auto-apply": "auto-apply-mode",
    "labels": "labels",
    "var-files": "var-files",
    "run-task-templates": "run-tasks",
    "notification-templates": "notification-configurations",
    "security-scan-enforcement": "security-scan-enforcement",
    "security-scan-engine": "security-scan-engine",
    "security-scan-severity-threshold": "security-scan-severity-threshold",
    "security-scan-skip-rules": "security-scan-skip-rules",
    "ai-summary-mode": "ai-summary-mode",
    "ai-summary-context": "ai-summary-context",
    "ai-policy-mode": "ai-policy-mode",
    "terragrunt-enabled": "terragrunt-enabled",
    "terragrunt-version": "terragrunt-version",
    "vcs-workflow": "vcs-workflow",
    "auto-merge": "auto-merge",
    "auto-merge-strategy": "auto-merge-strategy",
    "drift-detection-enabled": "drift-detection-enabled",
    "drift-detection-interval-seconds": "drift-detection-interval-seconds",
    "drift-ignore-rules": "drift-ignore-rules",
    "scheduled-configure-enabled": "scheduled-configure-enabled",
    "scheduled-configure-cron": "scheduled-configure-cron",
    "scheduled-configure-timezone": "scheduled-configure-timezone",
    "plan-expiry-seconds": "plan-expiry-seconds",
    "slack-channel": "slack-channel",
    "debug-mode": "debug-mode",
    "allow-fork-pr-plans": "allow-fork-pr-plans",
    "pulumi-bind-plan": "pulumi-bind-plan",
}

#: Rule attribute (API kebab-case) -> why a rule edit does not touch existing workspaces.
NOT_PROPAGATED: dict[str, str] = {
    "id": "the rule's own primary key",
    "created-at": "the rule's own audit timestamp",
    "updated-at": "the rule's own audit timestamp",
    "name": "the rule's own name; each workspace's name was derived at creation",
    "name-template": "applies only to names derived at creation; existing workspaces keep theirs",
    "vcs-connection-id": "the rule's own connection; changing it does not re-home a workspace",
    "repo-url": "the rule's own repository; changing it does not re-home a workspace",
    "branch": "the rule's own branch; changing it does not re-home a workspace",
    "pattern": "decides which directories are discovered, not a workspace setting",
    "ignore-patterns": "decides which directories are discovered, not a workspace setting",
    "enabled": "turns the rule's discovery on or off, not a workspace setting",
    "on-directory-delete": "the rule's own lifecycle policy, read at discovery time",
    "first-scan-at": "the rule's own scan bookkeeping",
    "engine": "pins what the rule discovers; an existing workspace's engine is never changed",
    "owner-email": "never bulk-settable: setting an owner can grant admin on the workspace",
    "storage-location": "fixed when a workspace is created: its objects live there, so it is never changed",
    "execution-hook-templates": "no bulk-update support yet",
}


_POOL_FIELD = "agent-pool-id"
_TEMPLATE_FIELDS = frozenset({"run-task-templates", "notification-templates"})

#: Judged as one unit: a zone moves a cron's fire time and a flag switches it on.
_SCHEDULE_FIELDS = (
    "scheduled-configure-enabled",
    "scheduled-configure-cron",
    "scheduled-configure-timezone",
)

#: Rule attribute -> workspace column, where it is not the attribute with underscores.
_COLUMN_OVERRIDES = {"terraform-version": "engine_version", "auto-apply": "auto_apply_mode"}

#: Unset is None or "" on either side, so these compare as strings with None read as "".
_STRING_FIELDS = frozenset(
    {
        "ansible-execution-environment",
        "ee-resource-cpu",
        "ee-resource-memory",
        "config-directory",
        "scheduled-configure-cron",
        "scheduled-configure-timezone",
        "default-playbook",
        "default-limit",
        "hard-limit",
        "default-run-kind",
        "ai-summary-context",
        "slack-channel",
    }
)
_LIST_FIELDS = frozenset(
    {
        "ansible-collections",
        "inventory-paths",
        "playbooks",
        "var-files",
        "security-scan-skip-rules",
        "drift-ignore-rules",
    }
)
_DICT_FIELDS = frozenset({"labels", "oidc-audiences"})

#: Template spec keys holding a secret; verdicts show only whether one is set.
_SECRET_SPEC_KEYS = frozenset({"hmac_key", "hmac-key", "token"})

Verdict = Literal["apply", "unchanged", "customised"]


@dataclass(frozen=True)
class FieldVerdict:
    """What propagation would do to one field; template fields hold name -> spec dicts."""

    field: str
    current: Any
    previous: Any
    new: Any
    verdict: Verdict


def _normalise(field: str, value: Any) -> Any:
    if field == _POOL_FIELD:
        return pool_set.normalise([value])
    if field in _STRING_FIELDS:
        return value or ""
    if field in _LIST_FIELDS:
        return list(value or [])
    if field in _DICT_FIELDS:
        return dict(value or {})
    return value


def is_changed(field: str, previous: Any, new: Any) -> bool:
    """Whether an edit changes a field's effective value, by the classifier's normalisation."""
    if field not in PROPAGATED_FIELDS:
        return previous != new
    return _normalise(field, previous) != _normalise(field, new)


def _verdict(current: Any, previous: Any, new: Any) -> Verdict:
    if current == new:
        return "unchanged"
    return "apply" if current == previous else "customised"


def _spec_value(value: Any) -> Any:
    return None if value in (None, "", []) else value


def _same_spec(row: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Whether a workspace row carries every value the rule spec does."""
    return all(_spec_value(row.get(k)) == _spec_value(v) for k, v in spec.items())


def _redact(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("<set>" if v else "<unset>") if k in _SECRET_SPEC_KEYS else v for k, v in spec.items()
    }


def _name_verdict(row: dict[str, Any] | None, old: dict[str, Any] | None, spec: dict) -> Verdict:
    if row is None:
        # A name the rule already had and the workspace lacks was deleted on purpose.
        return "apply" if old is None else "customised"
    if old is not None and old != spec and _same_spec(row, old):
        return "apply"
    return "unchanged" if _same_spec(row, spec) else "customised"


def _classify_templates(
    field: str,
    rows: dict[str, dict[str, Any]],
    previous: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> FieldVerdict:
    """One verdict per template field: any customised name skips the whole field."""
    old_specs = {s["name"]: s for s in previous or []}
    new_specs = {s["name"]: s for s in new or []}
    verdicts = [_name_verdict(rows.get(n), old_specs.get(n), s) for n, s in new_specs.items()]
    if "customised" in verdicts:
        overall: Verdict = "customised"
    else:
        overall = "apply" if "apply" in verdicts else "unchanged"
    return FieldVerdict(
        field,
        {
            n: _redact({k: rows[n].get(k) for k in spec})
            for n, spec in new_specs.items()
            if n in rows
        },
        {n: _redact(old_specs[n]) for n in new_specs if n in old_specs},
        {n: _redact(spec) for n, spec in new_specs.items()},
        overall,
    )


def classify(
    ws: Any,
    previous: dict[str, Any],
    new: dict[str, Any],
    templates: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[FieldVerdict]:
    """Classify changed rule fields; `templates` holds the workspace's rows by field, then name."""
    unknown = sorted(new.keys() - PROPAGATED_FIELDS.keys())
    if unknown:
        raise ValueError(f"not propagated rule fields: {', '.join(unknown)}")
    out: list[FieldVerdict] = []
    for field in PROPAGATED_FIELDS:
        if field not in new:
            continue
        if field in _TEMPLATE_FIELDS:
            if field not in (templates or {}):
                raise ValueError(f"{field}: the workspace's existing rows are required")
            out.append(_classify_templates(field, templates[field], previous[field], new[field]))
            continue
        if field == _POOL_FIELD:
            current = pool_set.workspace_pool_ids(ws)
        else:
            current = _normalise(
                field, getattr(ws, _COLUMN_OVERRIDES.get(field, field.replace("-", "_")))
            )
        before, after = _normalise(field, previous[field]), _normalise(field, new[field])
        out.append(FieldVerdict(field, current, before, after, _verdict(current, before, after)))
    group = [i for i, v in enumerate(out) if v.field in _SCHEDULE_FIELDS]
    if any(out[i].verdict == "customised" for i in group):
        for i in group:
            if out[i].verdict == "apply":
                out[i] = replace(out[i], verdict="customised")
    return out


PROPAGATE_ACTION = "workspace.rule_propagate"

#: Second spellings of a stored field; passing one would judge one setting twice.
_ALIASES = frozenset({"terraform-version", "auto-apply"})

_TEMPLATE_MODELS = {
    "run-task-templates": RunTask,
    "notification-templates": NotificationConfiguration,
}


@dataclass
class PropagationResult:
    """What one rule update did, or would do, to the workspaces the rule created."""

    dry_run: bool
    matched: int
    would_change: list[dict]
    customised: list[dict]
    unchanged: list[dict]
    skipped_lifecycle: int


def _ws_ref(ws: Any) -> dict[str, Any]:
    return {"id": f"ws-{ws.id}", "name": ws.name}


def _shown(field: str, value: Any) -> Any:
    return [f"apool-{p}" for p in value] if field == _POOL_FIELD else value


def _names_to_apply(rows: dict[str, dict], previous: list[dict], new: list[dict]) -> tuple:
    old = {s["name"]: s for s in previous or []}
    return tuple(
        s["name"]
        for s in new
        if _name_verdict(rows.get(s["name"]), old.get(s["name"]), s) == "apply"
    )


def _bulk_update(new: dict[str, Any], apply: dict[str, tuple | None]) -> dict[str, Any]:
    """The bulk `update` for the applied fields; template specs keep their real secrets."""
    update: dict[str, Any] = {}
    for name, names in apply.items():
        value = new[name]
        if name == _POOL_FIELD:
            value = [str(p) for p in pool_set.normalise([value])]
        elif name in _TEMPLATE_FIELDS:
            # The stored spec keys are bulk's input keys with underscores.
            value = [
                {k.replace("_", "-"): v for k, v in s.items()} for s in value if s["name"] in names
            ]
        update[PROPAGATED_FIELDS[name]] = value
    return update


async def _load_templates(
    db: AsyncSession, workspaces: list, fields: list[str], *, lock: bool
) -> dict[Any, dict[str, dict[str, dict]]]:
    """Every matched workspace's template rows, one query per template field; `lock` holds them."""
    out: dict[Any, dict[str, dict[str, dict]]] = {
        ws.id: {f: {} for f in fields} for ws in workspaces
    }
    if not out:
        return out
    for name in fields:
        model = _TEMPLATE_MODELS[name]
        columns = [a.key for a in sa_inspect(model).column_attrs]
        query = select(model).where(model.workspace_id.in_(list(out)))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        for row in (await db.execute(query)).scalars().all():
            out[row.workspace_id][name][row.name] = {c: getattr(row, c) for c in columns}
    return out


def _kind_after(ws: Any, new: dict[str, Any], apply: dict) -> str:
    """The run kind bulk's reconcile leaves when the update moves flags but names no kind."""

    def flag(name: str) -> bool:
        return new[name] if name in apply else getattr(ws, name.replace("-", "_"))

    return reconcile_default_run_kind(
        sent=None,
        stored=ws.default_run_kind,
        plan_apply=flag("plan-apply-enabled"),
        configure=flag("configure-enabled"),
    )


def _refuse_cleared_kinds(names: list[str]) -> None:
    if not names:
        return
    shown = ", ".join(sorted(names)[:10])
    more = f" (and {len(names) - 10} more)" if len(names) > 10 else ""
    raise HTTPException(
        status_code=422,
        detail=(
            f"the operation flags in this rule update would change the customised "
            f"default-run-kind of {len(names)} workspace(s): {shown}{more}. Set their "
            "default-run-kind first, or leave the operation flags unchanged."
        ),
    )


async def propagate_rule_update(
    db: AsyncSession,
    rule: AutodiscoveryRule,
    previous: dict[str, Any],
    changed: dict[str, Any],
    user: AuthenticatedUser,
    request_id: str,
    *,
    dry_run: bool,
) -> PropagationResult:
    """Apply a rule's changed propagated fields to its active workspaces; never commits or rolls back."""
    aliases = sorted(_ALIASES & changed.keys())
    if aliases:
        raise ValueError(f"pass the stored spelling, not: {', '.join(aliases)}")
    new = {k: v for k, v in changed.items() if k in PROPAGATED_FIELDS}
    if new.keys() & set(_SCHEDULE_FIELDS):
        # An unedited member is the same before and after, so the rule's own value is both.
        unedited = {n: getattr(rule, n.replace("-", "_")) for n in _SCHEDULE_FIELDS if n not in new}
        new.update(unedited)
        previous = {**previous, **unedited}
    if "oidc-audiences" in new:
        # Audiences pick the cloud trust a run token is minted for, so judge them here too.
        try:
            new["oidc-audiences"] = workspace_settings.validate_oidc_audiences(
                new["oidc-audiences"]
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    missing = sorted(new.keys() - previous.keys())
    if missing:
        raise ValueError(f"no previous value for: {', '.join(missing)}")
    before = {k: previous[k] for k in new}

    scope = Workspace.autodiscovery_rule_id == rule.id
    query = select(Workspace).where(scope, Workspace.lifecycle_state == "active")
    if not dry_run:
        # Lock the judged rows, refreshed so a concurrent edit is neither judged stale nor overwritten.
        query = query.with_for_update(of=Workspace).execution_options(populate_existing=True)
    active = list((await db.execute(query.order_by(Workspace.name))).scalars().all())
    others = select(func.count()).select_from(Workspace)
    skipped_lifecycle = await db.scalar(others.where(scope, Workspace.lifecycle_state != "active"))
    templates = await _load_templates(
        db, active, sorted(_TEMPLATE_FIELDS & new.keys()), lock=not dry_run
    )
    result = PropagationResult(dry_run, len(active), [], [], [], skipped_lifecycle or 0)

    groups: dict[tuple, list[Workspace]] = {}
    customised_ids: set[str] = set()
    cleared_kinds: list[str] = []
    for ws in active:
        found = templates[ws.id]
        verdicts = classify(ws, before, new, found)
        skipped = {
            v.field: {
                "current": _shown(v.field, v.current),
                "previous": _shown(v.field, v.previous),
                "new": _shown(v.field, v.new),
            }
            for v in verdicts
            if v.verdict == "customised"
        }
        apply = {
            v.field: (
                _names_to_apply(found[v.field], before[v.field], new[v.field])
                if v.field in _TEMPLATE_FIELDS
                else None
            )
            for v in verdicts
            if v.verdict == "apply"
        }
        if skipped:
            result.customised.append({**_ws_ref(ws), "fields": skipped})
            customised_ids.add(f"ws-{ws.id}")
        if apply:
            groups.setdefault(tuple(apply.items()), []).append(ws)
            if "default-run-kind" in skipped and _kind_after(ws, new, apply) != (
                ws.default_run_kind or ""
            ):
                cleared_kinds.append(ws.name)
        elif not skipped:
            result.unchanged.append(_ws_ref(ws))
    _refuse_cleared_kinds(cleared_kinds)

    # One bulk call per identical field set; a refusal raises, and a dry run is the caller's rollback.
    for key, members in groups.items():
        kinds = {f"ws-{ws.id}": (ws, ws.default_run_kind or "") for ws in members}
        applied, same = await workspace_bulk.apply_update_to_workspaces(
            db, members, _bulk_update(new, dict(key)), user, write_audit=False
        )
        for entry in applied + same:
            ws, old = kinds[entry["id"]]
            if (ws.default_run_kind or "") != old:
                kind = {"from": old, "to": ws.default_run_kind or ""}
                entry.setdefault("diff", {}).setdefault("default_run_kind", kind)
        result.would_change += [e for e in applied + same if e.get("diff")]
        result.unchanged += [e for e in same if not e.get("diff") and e["id"] not in customised_ids]

    add_audit_events(
        db,
        (
            {
                "actor_email": user.email,
                "action": PROPAGATE_ACTION,
                "resource_type": "workspace",
                "resource_id": w["id"],
                "status_code": 200,
                "request_id": request_id,
                "detail": f"autodiscovery-rule {rule.id}: {sorted(w['diff'])}",
            }
            for w in result.would_change
        ),
    )
    return result
