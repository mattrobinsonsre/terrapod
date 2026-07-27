"""AI architecture critic (#1036 Part 2 / #963).

The state-based, whole-system critic. It reconstructs a workspace's architecture
from its current Terraform **state** (+ the resource dependency graph, the
deterministic cost estimate, and the deterministic security-scan findings) and
critiques it across resilience / security / cost / well-architected — reviewing
the system **as it exists**, distinct from the per-run plan summary (which
reviews a *change*).

Runs in the API (never the runner — no model creds on Job pods), keyed to the
independent ``ai_architecture`` config (its own model, endpoint, auth secret,
and Redis token budget), reusing the summariser's generic LiteLLM helpers.

**Grounding is the whole point** (AI does judgment; deterministic tools do
facts): the security dimension is anchored to the scanner's findings, the cost
dimension to the cost engine's figures, and the model is told to defer anything
it cannot see in state rather than invent it. Prompt validated in the Part 2
spike (10/10 recall, 0 hallucinations on a ground-truth set; confirmed on a real
204-resource prod workspace, incl. reading count/for_each ``instances`` as
redundancy rather than false SPOFs).

**RBAC (hard):** the critique is derived from the secret-bearing state blob, so
the API surface gates read on ``state:read`` — the same trust as downloading the
state. This module runs server-side after that gate; it loads state directly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from typing import Any

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import ArchitectureCritique, Run, StateVersion, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.state_graph_service import (
    _resource_address,
    build_graph_from_state,
)
from terrapod.services.summariser import (
    _apply_anthropic_cache_markers,
    _parse_model_json,
    _parse_tool_call_arguments,
    _supports_anthropic_cache_control,
)
from terrapod.services.summariser_prompt import (
    ARCHITECTURE_CRITIQUE_TOOL,
    render_architecture_prompt,
)

logger = get_logger(__name__)

_LLM_NUM_RETRIES = 2  # → up to 3 attempts; a 4xx is final and not retried.

# Cap the compacted resource set fed to the model. The state graph tolerates up
# to 2000 nodes for the WebGL view, but a full attribute payload for that many
# would blow the token budget; 400 keeps the prompt bounded while covering the
# architecture signal of all but the largest workspaces. Truncation is reported
# honestly (``deferred`` note) rather than silently dropping resources.
MAX_CRITIQUE_RESOURCES = 400

# Architecture-relevant, NON-SECRET scalar attribute keys. An allowlist (not a
# denylist) is the safe choice: state carries sensitive values, so we only ever
# forward keys known to be architecture signal and never secret-bearing. Values
# are additionally constrained to scalars / short scalar lists at extraction.
_CURATED_ATTR_KEYS: frozenset[str] = frozenset(
    {
        # availability / HA / DR
        "availability_zone",
        "availability_zones",
        "zone",
        "zones",
        "multi_az",
        "multi_az_enabled",
        "automatic_failover_enabled",
        "backup_retention_period",
        "deletion_protection",
        "skip_final_snapshot",
        "point_in_time_recovery",
        "auto_minor_version_upgrade",
        "min_size",
        "max_size",
        "desired_capacity",
        "min_capacity",
        "max_capacity",
        "num_cache_nodes",
        "num_node_groups",
        "replicas_per_node_group",
        "replication_group_id",
        # sizing / cost
        "instance_type",
        "instance_class",
        "node_type",
        "size",
        "sku_name",
        "tier",
        "capacity_type",
        "allocated_storage",
        "max_allocated_storage",
        "storage_type",
        "iops",
        "throughput",
        # engine / version
        "engine",
        "engine_version",
        "version",
        "node_version",
        # exposure / networking (structure only; security detail is the scanner's)
        "internal",
        "publicly_accessible",
        "map_public_ip_on_launch",
        "associate_public_ip_address",
        "load_balancer_type",
        "scheme",
        "cidr_block",
        "connectivity_type",
        # encryption presence (booleans only)
        "storage_encrypted",
        "encrypted",
        "enable_key_rotation",
        # ops / retention
        "retention_in_days",
        "retention_period",
        "performance_insights_enabled",
        "monitoring_interval",
        "deletion_window_in_days",
    }
)


# --- Daily budget (independent of ai_summary / ai_onboarding) ----------------


def _budget_key() -> str:
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return f"tp:ai_architecture:budget:{today}"


async def _budget_remaining() -> int | None:
    cfg = settings.ai_architecture
    if cfg.daily_token_budget <= 0:
        return None
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    spent_raw = await r.get(_budget_key())
    spent = int(spent_raw) if spent_raw else 0
    return max(0, cfg.daily_token_budget - spent)


async def _budget_charge(tokens: int) -> None:
    cfg = settings.ai_architecture
    if cfg.daily_token_budget <= 0 or tokens <= 0:
        return
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    pipe = r.pipeline(transaction=False)
    pipe.incrby(_budget_key(), tokens)
    pipe.expire(_budget_key(), 60 * 60 * 36)
    await pipe.execute()


# --- Model call --------------------------------------------------------------


def _build_litellm_kwargs(
    *, system_message: str, user_message: str, max_output_tokens: int
) -> dict:
    """``litellm.acompletion`` kwargs — reads ``ai_architecture``, forces the tool."""
    cfg = settings.ai_architecture
    auth = cfg.auth
    messages: list[dict] = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]
    if _supports_anthropic_cache_control(cfg.model):
        messages = _apply_anthropic_cache_markers(messages)
    tool_name = ARCHITECTURE_CRITIQUE_TOOL["function"]["name"]
    kwargs: dict = {
        "model": cfg.model,
        "max_tokens": max_output_tokens,
        "messages": messages,
        "timeout": cfg.request_timeout_seconds,
        "num_retries": _LLM_NUM_RETRIES,
        "retry_strategy": "exponential_backoff_retry",
        "tools": [ARCHITECTURE_CRITIQUE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": tool_name}},
    }
    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base
    if auth.api_key:
        kwargs["api_key"] = auth.api_key
    if auth.aws_region:
        kwargs["aws_region_name"] = auth.aws_region
    if auth.aws_role_arn:
        kwargs["aws_role_name"] = auth.aws_role_arn
        kwargs["aws_session_name"] = auth.aws_session_name
        if auth.aws_external_id:
            kwargs["aws_external_id"] = auth.aws_external_id
    return kwargs


async def _call_model(
    *, system_message: str, user_message: str, max_output_tokens: int
) -> tuple[dict, int, int]:
    cfg = settings.ai_architecture
    if not cfg.model:
        raise RuntimeError("ai_architecture.model must be set")
    resp = await litellm.acompletion(
        **_build_litellm_kwargs(
            system_message=system_message,
            user_message=user_message,
            max_output_tokens=max_output_tokens,
        )
    )
    if not resp.choices:
        raise RuntimeError("model response had no choices")
    choice = resp.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise RuntimeError(
            f"model response truncated at max_output_tokens={max_output_tokens} "
            f"(finish_reason=length); raise ai_architecture.max_output_tokens"
        )
    tool_calls = getattr(choice.message, "tool_calls", None) or []
    if tool_calls:
        parsed = _parse_tool_call_arguments(tool_calls[0])
    else:
        text = choice.message.content or ""
        logger.warning(
            "architecture critic: no tool_calls despite tool_choice; parsing body",
            finish_reason=finish_reason,
            response_length=len(text),
        )
        parsed = _parse_model_json(text)
    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return parsed, in_tok, out_tok


# --- State compaction (the new bit vs the spike) -----------------------------


def _curate_attrs(raw: object) -> dict[str, Any]:
    """Keep only architecture-relevant, non-secret scalar attributes.

    Values are constrained to scalars (bool/int/float/None), short strings
    (<=200 chars), or short lists of scalars — nested blocks and long/opaque
    values are dropped. The key allowlist guarantees no secret-bearing field
    (password, private_key, …) is ever forwarded to the model.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in _CURATED_ATTR_KEYS:
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            if len(v) <= 200:
                out[k] = v
        elif isinstance(v, list) and len(v) <= 8:
            scalars = [x for x in v if isinstance(x, (str, int, float, bool))]
            if len(scalars) == len(v):
                out[k] = [x for x in scalars if not (isinstance(x, str) and len(x) > 200)]
    return out


def compact_state_for_critique(state: dict) -> tuple[list[dict], list[dict], bool]:
    """Terraform state v4 -> (resources, edges, truncated) for the critic prompt.

    Reuses ``build_graph_from_state`` for the validated address / ``instances`` /
    depends-on logic, then layers a curated attribute subset onto each MANAGED
    resource node (data sources are dropped — they are not architecture). Pure
    and CPU-only, so callers run it in a worker thread (rules 13/14).
    """
    graph = build_graph_from_state(state)
    nodes = [n for n in graph["nodes"] if n.get("mode") != "data"]

    # address -> curated attrs (first instance's recorded attributes).
    attrs_by_addr: dict[str, dict] = {}
    for res in state.get("resources") or []:
        if not isinstance(res, dict) or res.get("mode") == "data":
            continue
        rtype = res.get("type") or ""
        name = res.get("name") or ""
        if not rtype or not name:
            continue
        addr = _resource_address(res.get("module") or "", "managed", rtype, name)
        instances = res.get("instances") or []
        first = instances[0] if instances and isinstance(instances[0], dict) else {}
        attrs_by_addr[addr] = _curate_attrs(first.get("attributes"))

    truncated = False
    resources: list[dict] = []
    for n in nodes:
        if len(resources) >= MAX_CRITIQUE_RESOURCES:
            truncated = True
            break
        resources.append(
            {
                "address": n["id"],
                "type": n["type"],
                "instances": n.get("instances", 1),
                "attrs": attrs_by_addr.get(n["id"], {}),
            }
        )
    kept = {r["address"] for r in resources}
    edges = [e for e in graph["edges"] if e["source"] in kept and e["target"] in kept]
    return resources, edges, truncated


# --- Grounding-input loaders (all best-effort except state) ------------------


async def _load_state_compact(sv: StateVersion) -> tuple[list[dict], list[dict], bool] | None:
    """Load + decrypt the state blob and compact it. None when unavailable."""
    if sv.state_size == 0:
        return None
    from terrapod.crypto.state import decrypt_state_bytes
    from terrapod.storage import get_storage
    from terrapod.storage.keys import state_key

    storage = get_storage()
    try:
        data = await storage.get(state_key(str(sv.workspace_id), str(sv.id)))
    except Exception:
        logger.warning("architecture critic: state blob missing", state_version_id=str(sv.id))
        return None
    data = await decrypt_state_bytes(data)

    def _parse(raw: bytes) -> tuple[list[dict], list[dict], bool]:
        return compact_state_for_critique(json.loads(raw))

    try:
        return await asyncio.to_thread(_parse, data)
    except (ValueError, TypeError):
        logger.warning("architecture critic: state parse failed", state_version_id=str(sv.id))
        return None


def _internal_reader() -> Any:
    """A server-side admin principal for the internal cost sub-read.

    The critique itself is gated on ``state:read`` at the API layer before this
    server-side handler ever runs; the cost engine's own RBAC just needs a
    principal, so we hand it an internal admin one. Used only for the read-only
    cost estimate.
    """
    from terrapod.api.dependencies import AuthenticatedUser

    return AuthenticatedUser(
        email="system@terrapod.internal",
        display_name="architecture-critic",
        roles=["admin"],
        provider_name="internal",
        auth_method="session",
    )


async def _load_cost(db: AsyncSession, workspace_id: uuid.UUID) -> str:
    """Compacted deterministic cost estimate for the workspace's current state, or ""."""
    try:
        from terrapod.services.workspace_cost_service import estimate_workspace_cost

        est = await estimate_workspace_cost(db, _internal_reader(), str(workspace_id))
        resources = [
            {"address": r.get("address"), "type": r.get("type"), "monthly": r.get("monthly")}
            for r in (est.get("resources") or [])
            if r.get("monthly")
        ]
        if not resources and not est.get("unpriced"):
            return ""
        compact = {
            "currency": est.get("currency"),
            "monthly_total": est.get("total"),
            "resources": resources[:MAX_CRITIQUE_RESOURCES],
            "unpriced": [r.get("address") for r in (est.get("unpriced") or [])][:100],
        }
        return json.dumps(compact, default=str)
    except Exception as e:  # noqa: BLE001 — cost grounding is best-effort
        logger.debug("architecture critic: cost load skipped", error=str(e))
        return ""


async def _load_security(db: AsyncSession, workspace_id: uuid.UUID) -> str:
    """Latest available deterministic scan findings for the workspace, or "".

    The critic is state-based (not run-based), so there is no run to key on; we
    take the most recent run for the workspace that produced a scan result and
    forward its findings as the security ground truth (best-effort).
    """
    try:
        from terrapod.db.models import SecurityScanResult

        row = (
            await db.execute(
                select(SecurityScanResult)
                .join(Run, Run.id == SecurityScanResult.run_id)
                .where(Run.workspace_id == workspace_id)
                .order_by(SecurityScanResult.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None or not row.findings:
            return ""
        findings = [
            {
                "engine": f.get("engine"),
                "rule_id": f.get("rule_id"),
                "severity": f.get("severity"),
                "title": f.get("title"),
                "resource": f.get("resource"),
            }
            for f in row.findings
            if isinstance(f, dict)
        ][:200]
        return json.dumps({"findings": findings}, default=str)
    except Exception as e:  # noqa: BLE001 — security grounding is best-effort
        logger.debug("architecture critic: security load skipped", error=str(e))
        return ""


# --- Persist + SSE -----------------------------------------------------------


async def _emit_event(workspace_id: uuid.UUID, event: str, **extra: Any) -> None:
    try:
        from terrapod.redis.client import RUN_EVENTS_PREFIX, publish_event

        payload = {"event": event, "workspace_id": str(workspace_id)}
        payload.update(extra)
        await publish_event(f"{RUN_EVENTS_PREFIX}{workspace_id}", json.dumps(payload))
    except Exception as e:  # SSE is best-effort
        logger.debug("architecture critic: event publish failed", event=event, error=str(e))


async def _upsert(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    sv: StateVersion,
    status: str,
    architecture: dict | None = None,
    risk_level: str = "",
    findings: list | None = None,
    deferred: list | None = None,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str = "",
) -> ArchitectureCritique:
    """Idempotent upsert keyed on (state_version). A 'ready' row is never
    overwritten by a later errored attempt for the same state."""
    existing = (
        await db.execute(
            select(ArchitectureCritique).where(ArchitectureCritique.state_version_id == sv.id)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == "ready" and status != "ready":
        return existing
    row = existing or ArchitectureCritique(
        workspace_id=workspace_id, state_version_id=sv.id, state_serial=sv.serial
    )
    row.status = status
    row.architecture = architecture if architecture is not None else {}
    row.risk_level = risk_level
    row.findings = findings if findings is not None else []
    row.deferred = deferred if deferred is not None else []
    row.model = model
    row.input_tokens = input_tokens
    row.output_tokens = output_tokens
    row.error_message = error_message
    if existing is None:
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _coerce_result(parsed: dict) -> tuple[dict, str, list, list]:
    """Coerce tool args into (architecture, risk_level, findings, deferred)."""
    arch = parsed.get("architecture")
    architecture = arch if isinstance(arch, dict) else {}
    risk_level = str(parsed.get("risk_level") or "")
    findings = parsed.get("findings")
    findings = findings if isinstance(findings, list) else []
    deferred = parsed.get("deferred")
    deferred = [str(d) for d in deferred] if isinstance(deferred, list) else []
    return architecture, risk_level, findings, deferred


# --- Core generation + trigger handler ---------------------------------------


async def generate_critique(
    workspace_id: uuid.UUID, *, force: bool = False
) -> ArchitectureCritique | None:
    """Generate (or refresh) the architecture critique for a workspace's CURRENT
    state version. Returns the critique row, or None when there is no state /
    the feature is disabled / the budget is exhausted.

    Idempotent per state version: a ready critique for the current state is
    returned as-is unless ``force`` (an explicit regenerate).
    """
    cfg = settings.ai_architecture
    if not cfg.enabled:
        return None

    async with get_db_session() as db:
        ws = (
            await db.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if ws is None:
            return None
        sv = (
            await db.execute(
                select(StateVersion)
                .where(StateVersion.workspace_id == workspace_id)
                .order_by(StateVersion.serial.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sv is None or sv.state_size == 0:
            return None

        existing = (
            await db.execute(
                select(ArchitectureCritique).where(ArchitectureCritique.state_version_id == sv.id)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == "ready" and not force:
            return existing

        remaining = await _budget_remaining()
        if remaining is not None and remaining <= 0:
            logger.info("architecture critic: budget exhausted", workspace_id=str(workspace_id))
            await _upsert(db, workspace_id=workspace_id, sv=sv, status="skipped")
            await _emit_event(workspace_id, "architecture_critique_skipped")
            return None

        await _upsert(db, workspace_id=workspace_id, sv=sv, status="pending")
        await _emit_event(workspace_id, "architecture_critique_pending")

        compact = await _load_state_compact(sv)
        cost_json = await _load_cost(db, workspace_id)
        security_json = await _load_security(db, workspace_id)
        sv_serial = sv.serial
        sv_ref = sv

    if compact is None:
        async with get_db_session() as db:
            await _upsert(
                db,
                workspace_id=workspace_id,
                sv=sv_ref,
                status="errored",
                error_message="state unavailable or unparseable",
            )
        await _emit_event(workspace_id, "architecture_critique_errored")
        return None

    resources, edges, truncated = compact
    deferred_seed = (
        [
            f"Resource set truncated to {MAX_CRITIQUE_RESOURCES} of {sv_serial}+ — "
            "review the remainder separately."
        ]
        if truncated
        else []
    )

    system_message, user_message = render_architecture_prompt(
        resources_json=json.dumps(resources, default=str),
        edges_json=json.dumps(edges, default=str),
        security_findings=security_json,
        cost_estimate=cost_json,
        workspace_context=cfg.context,
    )

    try:
        parsed, in_tok, out_tok = await _call_model(
            system_message=system_message,
            user_message=user_message,
            max_output_tokens=cfg.max_output_tokens,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "architecture critic model call failed", workspace_id=str(workspace_id), error=str(e)
        )
        async with get_db_session() as db:
            await _upsert(
                db,
                workspace_id=workspace_id,
                sv=sv_ref,
                status="errored",
                model=cfg.model,
                error_message=str(e)[:500],
            )
        await _emit_event(workspace_id, "architecture_critique_errored")
        return None

    architecture, risk_level, findings, deferred = _coerce_result(parsed)
    async with get_db_session() as db:
        row = await _upsert(
            db,
            workspace_id=workspace_id,
            sv=sv_ref,
            status="ready",
            architecture=architecture,
            risk_level=risk_level,
            findings=findings,
            deferred=deferred_seed + deferred,
            model=cfg.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
    await _budget_charge(out_tok)
    await _emit_event(workspace_id, "architecture_critique_ready")
    logger.info(
        "architecture critique ready",
        workspace_id=str(workspace_id),
        risk_level=risk_level,
        findings=len(findings),
        in_tok=in_tok,
        out_tok=out_tok,
    )
    return row


async def handle_architecture_critique(payload: dict) -> None:
    """Triggered handler. Payload ``{"workspace_id": "<uuid>", "force": bool}``.

    Best-effort and idempotent — a ready critique for the current state version
    no-ops unless ``force``.
    """
    if not settings.ai_architecture.enabled:
        return
    try:
        workspace_id = uuid.UUID(str(payload["workspace_id"]))
    except (KeyError, ValueError):
        logger.warning("invalid architecture_critique payload", payload=payload)
        return
    await generate_critique(workspace_id, force=bool(payload.get("force")))
