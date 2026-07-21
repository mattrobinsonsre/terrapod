"""AI *enhancement* of a run's cost estimate (#871).

This is the optional AI layer over the deterministic, oiq-derived cost estimate.
It rides the SAME switch as the plan summary (``ai_summary.enabled`` + the
per-workspace mode) and reuses the plan summariser's model plumbing — the
LiteLLM tool-calling call (:func:`summariser._call_model`), the shared daily
token budget, and the per-workspace run-events SSE channel.

**Its PRIMARY job is to price what oiq can't (#871 reframe).** The deterministic
engine prices what its pricesheet covers; this service uses the model's own cost
knowledge to ESTIMATE the resources oiq could NOT price — the ``unpriced`` bucket
(unmapped types, and providers oiq doesn't cover, e.g. Azure/GCP) plus obviously
usage-driven costs it omits. Those estimates are the ``estimated_resources``
primary output. A human-readable ``narrative`` + savings ``advisories`` are the
secondary bonus.

**Provenance is a hard invariant.** Every figure this service stores — each
estimated resource and each advisory — is tagged ``source: "ai-estimate"``
**server-side here**, regardless of what the model returns. It is shown as a
separate overlay, NEVER summed into or substituted for the authoritative oiq
total (which stays on the data-only ``/runs/{id}/cost-estimate`` endpoint), and
is never a gate.

The only input fed to the model is the ``cost_estimate.json`` artifact — a
derived, non-sensitive aggregate (resource addresses, types, monthly ranges).
It deliberately does NOT read Terraform state or the plan JSON, so no sensitive
value can leak through this path (asserted by ``TestCostNoStateLeakage``).

Enqueued as an ``ai_cost_summary`` trigger when the runner uploads
``cost_estimate.json`` (``run_artifacts.py``), only when the feature is globally
enabled. Multi-replica safe: any replica runs the handler; the upsert is
idempotent on ``run_id``.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CostSummary, CostSummaryMessage, Run, Workspace, now_utc
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.summariser import (
    FollowupBudgetExhausted,
    FollowupCapReached,
    FollowupDisabled,
    FollowupError,
    _budget_charge,
    _budget_remaining,
    _call_chat_model,
    _call_model,
    _emit_summary_event,
    _resolve_workspace_mode,
)
from terrapod.services.summariser_prompt import render_cost_prompt
from terrapod.storage import get_storage
from terrapod.storage.keys import cost_estimate_key
from terrapod.storage.protocol import ObjectNotFoundError

logger = get_logger(__name__)

_VALID_ADVISORY_KINDS = {"savings_plan", "reserved", "spot", "rightsizing", "other"}


async def _upsert_cost_summary(
    db,
    *,
    run_id: uuid.UUID,
    status: str,
    estimated_resources: list[dict] | None = None,
    narrative: str = "",
    advisories: list[dict] | None = None,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str = "",
) -> None:
    """Idempotent upsert keyed on (run_id); never downgrades a 'ready' row.

    Mirrors ``summariser._upsert_summary`` so a transient errored retry can't
    clobber a good result.
    """
    existing = (
        await db.execute(select(CostSummary).where(CostSummary.run_id == run_id))
    ).scalar_one_or_none()
    if existing is not None and existing.status == "ready" and status != "ready":
        return

    values = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "status": status,
        "estimated_resources": estimated_resources or [],
        "narrative": narrative,
        "advisories": advisories or [],
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error_message": error_message,
        "updated_at": now_utc(),
    }
    stmt = pg_insert(CostSummary).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "status": stmt.excluded.status,
            "estimated_resources": stmt.excluded.estimated_resources,
            "narrative": stmt.excluded.narrative,
            "advisories": stmt.excluded.advisories,
            "model": stmt.excluded.model,
            "input_tokens": stmt.excluded.input_tokens,
            "output_tokens": stmt.excluded.output_tokens,
            "error_message": stmt.excluded.error_message,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)


def _normalise_estimated_resources(raw: object) -> list[dict]:
    """Coerce the model's per-resource estimates into the stored shape.

    The PRIMARY output (#871): AI estimates for resources oiq couldn't price.
    Each becomes ``{address, type, monthly: {min, max}, basis, source:
    "ai-estimate"}``. ``source`` is forced here — the model can never claim a
    computed figure. Entries missing a numeric range or an address are dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        lo = item.get("monthly_min")
        hi = item.get("monthly_max")
        if not isinstance(address, str) or not address:
            continue
        if not isinstance(lo, int | float) or not isinstance(hi, int | float):
            continue
        basis = item.get("basis")
        out.append(
            {
                "address": address,
                "type": item.get("type") if isinstance(item.get("type"), str) else "",
                "monthly": {"min": float(lo), "max": float(hi)},
                "basis": basis[:400] if isinstance(basis, str) else "",
                "source": "ai-estimate",  # HARD provenance — always an estimate
            }
        )
    return out


def _normalise_advisories(raw: object) -> list[dict]:
    """Coerce the model's advisories into the stored shape, stamping provenance.

    Each advisory becomes ``{kind, title, detail, monthly_saving: {min, max} |
    None, source: "ai-estimate"}``. ``source`` is forced here — the model can't
    claim a computed figure. Malformed entries are dropped rather than trusted.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        title = item.get("title")
        detail = item.get("detail")
        if (
            kind not in _VALID_ADVISORY_KINDS
            or not isinstance(title, str)
            or not isinstance(detail, str)
        ):
            continue
        lo = item.get("monthly_saving_min")
        hi = item.get("monthly_saving_max")
        saving = None
        if isinstance(lo, int | float) and isinstance(hi, int | float):
            saving = {"min": float(lo), "max": float(hi)}
        out.append(
            {
                "kind": kind,
                "title": title[:120],
                "detail": detail[:600],
                "monthly_saving": saving,
                "source": "ai-estimate",  # HARD provenance — never trust the model
            }
        )
    return out


async def handle_ai_cost_summary(payload: dict) -> None:
    """Triggered handler: generate the AI cost narrative for a run.

    Payload: ``{"run_id": "<uuid>"}``. Enqueued when ``cost_estimate.json``
    lands, only when ``ai_summary.enabled``. Every exit path settles the
    ``cost_summaries`` row to a terminal state (ready / skipped / errored) and
    emits the matching SSE event so the run-page Cost tab updates live.
    """
    cfg = settings.ai_summary
    if not cfg.enabled:
        return

    try:
        run_id = uuid.UUID(payload["run_id"])
    except (KeyError, ValueError):
        logger.warning("Invalid ai_cost_summary payload", payload=payload)
        return

    async with get_db_session() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None or not run.has_cost_estimate:
            return
        ws = (
            await db.execute(select(Workspace).where(Workspace.id == run.workspace_id))
        ).scalar_one_or_none()
        if ws is None:
            return

        # Per-workspace mode gate (global × workspace). OFF → record 'skipped'
        # so the UI shows a deliberate skipped state, not a spinner.
        if not _resolve_workspace_mode(ws):
            await _upsert_cost_summary(db, run_id=run_id, status="skipped", model=cfg.model)
            await db.commit()
            await _emit_summary_event("cost_summary_skipped", ws.id, run_id)
            return

        await _upsert_cost_summary(db, run_id=run_id, status="pending", model=cfg.model)
        await db.commit()
        await _emit_summary_event("cost_summary_pending", ws.id, run_id)

        # Budget gate — shared daily output-token budget with the plan summary.
        remaining = await _budget_remaining()
        if remaining is not None and remaining <= 0:
            await _upsert_cost_summary(db, run_id=run_id, status="skipped", model=cfg.model)
            await db.commit()
            await _emit_summary_event("cost_summary_skipped", ws.id, run_id)
            return

        # Read the authoritative estimate — the ONLY model input (derived,
        # non-sensitive; no state, no plan JSON).
        storage = get_storage()
        key = cost_estimate_key(str(run.workspace_id), str(run.id))
        try:
            raw = await storage.get(key)
        except ObjectNotFoundError:
            await _upsert_cost_summary(db, run_id=run_id, status="skipped", model=cfg.model)
            await db.commit()
            await _emit_summary_event("cost_summary_skipped", ws.id, run_id)
            return
        estimate_json = raw.decode() if isinstance(raw, bytes) else str(raw)

        ctx = cfg.context
        system_message, user_message = render_cost_prompt(
            estimate_json=estimate_json,
            fleet_context=ctx.fleet_context,
            workspace_context=ws.ai_summary_context or "",
            prompt_prefix=ctx.prompt_prefix,
            prompt_suffix=ctx.prompt_suffix,
            output_language=cfg.summary_language,
        )

        try:
            args, input_tokens, output_tokens = await _call_model(
                kind="cost_summary",
                system_message=system_message,
                user_message=user_message,
                max_output_tokens=cfg.max_output_tokens,
            )
        except Exception as e:  # noqa: BLE001 — any model/HTTP/parse failure
            logger.info("cost summary model call failed", run_id=str(run_id), error=str(e))
            await _upsert_cost_summary(
                db,
                run_id=run_id,
                status="errored",
                model=cfg.model,
                error_message=str(e)[:2000],
            )
            await db.commit()
            await _emit_summary_event("cost_summary_errored", ws.id, run_id)
            return

        # A non-dict tool result is the only genuine failure — the model
        # returned something unusable. Empty estimates/advisories/narrative are
        # ALL legitimate "ready" states (oiq priced everything; nothing for the
        # AI to add), so we don't error on them.
        if not isinstance(args, dict):
            await _upsert_cost_summary(
                db,
                run_id=run_id,
                status="errored",
                model=cfg.model,
                error_message="model returned no structured result",
            )
            await db.commit()
            await _emit_summary_event("cost_summary_errored", ws.id, run_id)
            return

        estimated_resources = _normalise_estimated_resources(args.get("estimated_resources"))
        advisories = _normalise_advisories(args.get("advisories"))
        narrative = args.get("narrative")
        narrative = narrative if isinstance(narrative, str) else ""

        await _upsert_cost_summary(
            db,
            run_id=run_id,
            status="ready",
            estimated_resources=estimated_resources,
            narrative=narrative,
            advisories=advisories,
            model=cfg.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await db.commit()
        await _budget_charge(output_tokens)
        await _emit_summary_event("cost_summary_ready", ws.id, run_id)


# ---------------------------------------------------------------------------
# Chat follow-ups (#871) — Q&A thread grounded in the cost estimate
# ---------------------------------------------------------------------------

_COST_CHAT_SYSTEM = (
    "You are a FinOps assistant answering follow-up questions about a single "
    "Terraform/OpenTofu plan's cost estimate. You are given: (1) the "
    "deterministic cost estimate (from OpenInfraQuote pricing data — these "
    "figures are computed and authoritative), and (2) the AI-estimated figures "
    "for resources the pricing data could not cover (these are ESTIMATES, not "
    "computed). Answer concisely in {language}, grounded ONLY in the materials "
    "provided. Keep the distinction clear: never present an AI estimate as a "
    "computed/authoritative figure, and never invent prices for resources not in "
    "the materials — if a question can't be answered from what's provided, say so "
    "rather than guessing. No tool calls; plain prose."
)


def _render_cost_chat_context(estimate_json: str, summary: CostSummary) -> str:
    """The initial context turn for the cost chat — the derived estimate plus
    the AI estimates/advisories the summariser already produced. Small by
    construction (a derived aggregate, no state/plan JSON)."""
    import json

    ai = {
        "estimated_resources": summary.estimated_resources,
        "advisories": summary.advisories,
        "narrative": summary.narrative,
    }
    return (
        "DETERMINISTIC_COST_ESTIMATE (OpenInfraQuote — computed, authoritative):\n"
        f"{estimate_json}\n\n"
        "AI_ESTIMATES (source: ai-estimate — NOT computed):\n"
        f"{json.dumps(ai, ensure_ascii=False)}"
    )


async def _build_cost_followup_history(
    db: AsyncSession, summary: CostSummary, new_user_text: str
) -> list[dict]:
    """Chat history appended after the (system + initial-context) prefix:
    a framing exchange that establishes prose follow-up mode, prior turns in
    chronological order, then the just-posted user message."""
    history: list[dict] = [
        {
            "role": "user",
            "content": (
                "Thanks — the cost estimate above is recorded. I'd like to ask "
                "follow-up questions about these costs in plain prose. Answer "
                "concisely, grounded only in the estimate and AI figures above, "
                "and keep computed vs AI-estimated figures distinct."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Understood. I'll answer follow-up questions about this cost "
                "estimate in prose, grounded in the figures provided, and flag "
                "which numbers are AI estimates. What would you like to know?"
            ),
        },
    ]
    prior = (
        (
            await db.execute(
                sa.select(CostSummaryMessage)
                .where(CostSummaryMessage.cost_summary_id == summary.id)
                .order_by(CostSummaryMessage.created_at, CostSummaryMessage.id)
            )
        )
        .scalars()
        .all()
    )
    for msg in prior:
        if msg.role == "assistant" and not msg.content.strip():
            continue
        history.append({"role": msg.role, "content": msg.content})
    history.append({"role": "user", "content": new_user_text})
    return history


async def post_cost_followup(
    *,
    db: AsyncSession,
    cost_summary: CostSummary,
    run: Run,
    workspace: Workspace,
    user_message_text: str,
) -> CostSummaryMessage:
    """Process one operator follow-up turn on a cost estimate (#871).

    Mirrors ``summariser.post_followup`` but grounded in the (small) cost
    estimate rather than the plan context. The router has already authorised +
    loaded the rows. Persists the user turn first (so a failed model call still
    records the question), calls the model, then persists the assistant turn +
    telemetry and emits the SSE event.

    Raises FollowupDisabled / FollowupCapReached / FollowupBudgetExhausted /
    FollowupError, or RuntimeError/ValueError on a model failure (an errored
    assistant row is committed first so the transcript reflects it).
    """
    cfg = settings.ai_summary
    if not cfg.enabled or cfg.followup_max_messages_per_run <= 0:
        raise FollowupDisabled("AI follow-up chat is disabled")
    if not _resolve_workspace_mode(workspace):
        raise FollowupDisabled("AI summary is disabled for this workspace")

    text = (user_message_text or "").strip()
    if not text:
        raise FollowupError("message body is empty")
    if len(text) > 32 * 1024:
        raise FollowupError("message body exceeds 32 KiB")

    # Per-run cap counts USER rows only.
    user_count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(CostSummaryMessage)
            .where(
                CostSummaryMessage.cost_summary_id == cost_summary.id,
                CostSummaryMessage.role == "user",
            )
        )
    ).scalar() or 0
    if user_count >= cfg.followup_max_messages_per_run:
        raise FollowupCapReached(
            f"reached the {cfg.followup_max_messages_per_run}-message cap for this run"
        )

    remaining = await _budget_remaining()
    if remaining is not None and remaining <= 0:
        raise FollowupBudgetExhausted("daily AI token budget exhausted")

    # Persist the user turn first so it survives a downstream model failure.
    db.add(CostSummaryMessage(cost_summary_id=cost_summary.id, role="user", content=text))
    await db.flush()

    # Ground the chat in the derived cost estimate — the ONLY external input
    # (no state, no plan JSON). If it aged out, record an errored assistant turn.
    storage = get_storage()
    key = cost_estimate_key(str(run.workspace_id), str(run.id))
    try:
        raw = await storage.get(key)
    except ObjectNotFoundError:
        err = "no cost estimate available to ground the follow-up"
        db.add(
            CostSummaryMessage(
                cost_summary_id=cost_summary.id,
                role="assistant",
                content="",
                model=cfg.model,
                error_message=err,
            )
        )
        await db.commit()
        raise FollowupError(err) from None
    estimate_json = raw.decode() if isinstance(raw, bytes) else str(raw)

    system_message = _COST_CHAT_SYSTEM.format(language=cfg.summary_language or "English")
    initial_user_message = _render_cost_chat_context(estimate_json, cost_summary)
    history = await _build_cost_followup_history(db, cost_summary, text)

    try:
        reply_text, in_tok, out_tok = await _call_chat_model(
            system_message=system_message,
            user_message=initial_user_message,
            history=history,
            max_output_tokens=cfg.followup_max_output_tokens,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("cost follow-up call failed", run_id=str(run.id), error=str(e))
        db.add(
            CostSummaryMessage(
                cost_summary_id=cost_summary.id,
                role="assistant",
                content="",
                model=cfg.model,
                error_message=str(e)[:500],
            )
        )
        await db.commit()
        raise

    assistant_row = CostSummaryMessage(
        cost_summary_id=cost_summary.id,
        role="assistant",
        content=reply_text,
        model=cfg.model,
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
    db.add(assistant_row)
    await _budget_charge(out_tok)
    await db.commit()

    await _emit_summary_event(
        "cost_summary_message_posted",
        workspace.id,
        run.id,
        message_id=str(assistant_row.id),
    )
    return assistant_row
