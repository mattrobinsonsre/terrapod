"""AI *architecture critique* of a run's proposed infrastructure (#963/#1036).

This is the optional AI reasoning layer that reviews the infrastructure a
terraform/tofu plan PROPOSES, from the perspective of a senior cloud/platform
architect — security posture, reliability/HA, cost-efficiency, operational
excellence, scalability, and best-practice adherence. It renders in the web UI
*on top of* the deterministic Checkov/Trivy security-scan panel: the scan is the
deterministic rule layer; this critique is the AI reasoning layer above it. They
are distinct rows.

It is a near-exact clone of the AI cost summary (#871): it rides the SAME switch
as the plan summary (``ai_summary.enabled`` + the per-workspace mode) and reuses
the plan summariser's model plumbing — the LiteLLM tool-calling call
(:func:`summariser._call_model`), the shared daily token budget, and the
per-workspace run-events SSE channel.

**Advisory only.** The critique never gates a run and never restates or replaces
the deterministic scan verdict — it adds architectural judgement the scanner
can't.

**Input = the run's plan JSON ``planned_values``.** Unlike the cost summariser
(which reads only the derived cost aggregate), the critic needs the proposed
infrastructure itself. It reuses the plan-summary path's EXISTING clean+bound
helpers — :func:`summariser._clean_plan_json_bytes` strips every sensitive
attribute value, :func:`summariser._fit_plan_json` bounds the size — so no
sensitive state value can reach the model. It does NOT read Terraform state
(asserted by ``TestArchitectureCritiqueNoStateLeakage``).

Enqueued as an ``ai_architecture_critique`` trigger when the runner uploads the
plan JSON (``run_artifacts.py``), only when the feature is globally enabled.
Multi-replica safe: any replica runs the handler; the upsert is idempotent on
``run_id``.
"""

from __future__ import annotations

import asyncio
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import (
    ArchitectureCritique,
    ArchitectureCritiqueMessage,
    Run,
    Workspace,
    now_utc,
)
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
    _clean_plan_json_bytes,
    _emit_summary_event,
    _fit_plan_json,
    _resolve_workspace_mode,
)
from terrapod.services.summariser_prompt import render_architecture_critique_prompt
from terrapod.storage import get_storage
from terrapod.storage.keys import plan_json_output_key
from terrapod.storage.protocol import ObjectNotFoundError

logger = get_logger(__name__)

_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_VALID_CATEGORIES = {"security", "reliability", "cost", "operations", "scalability", "other"}
_VALID_RISK_LEVELS = {"critical", "high", "medium", "low", "none"}


async def _upsert_architecture_critique(
    db,
    *,
    run_id: uuid.UUID,
    status: str,
    critique: str = "",
    findings: list[dict] | None = None,
    risk_level: str = "",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str = "",
) -> None:
    """Idempotent upsert keyed on (run_id); never downgrades a 'ready' row.

    Mirrors ``cost_summariser._upsert_cost_summary`` so a transient errored
    retry can't clobber a good result.
    """
    existing = (
        await db.execute(select(ArchitectureCritique).where(ArchitectureCritique.run_id == run_id))
    ).scalar_one_or_none()
    if existing is not None and existing.status == "ready" and status != "ready":
        return

    values = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "status": status,
        "critique": critique,
        "findings": findings or [],
        "risk_level": risk_level,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error_message": error_message,
        "updated_at": now_utc(),
    }
    stmt = pg_insert(ArchitectureCritique).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "status": stmt.excluded.status,
            "critique": stmt.excluded.critique,
            "findings": stmt.excluded.findings,
            "risk_level": stmt.excluded.risk_level,
            "model": stmt.excluded.model,
            "input_tokens": stmt.excluded.input_tokens,
            "output_tokens": stmt.excluded.output_tokens,
            "error_message": stmt.excluded.error_message,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)


def _normalise_findings(raw: object) -> list[dict]:
    """Coerce the model's findings into the stored shape.

    Each becomes ``{severity, category, title, detail, address}``. Entries with
    an invalid ``severity``/``category`` or missing ``title``/``detail`` are
    dropped rather than trusted. ``address`` is optional (empty string when the
    finding is not resource-specific).
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        category = item.get("category")
        title = item.get("title")
        detail = item.get("detail")
        if severity not in _VALID_SEVERITIES or category not in _VALID_CATEGORIES:
            continue
        if not isinstance(title, str) or not isinstance(detail, str):
            continue
        address = item.get("address")
        out.append(
            {
                "severity": severity,
                "category": category,
                "title": title[:120],
                "detail": detail[:600],
                "address": address if isinstance(address, str) else "",
            }
        )
    return out


def _normalise_risk_level(raw: object) -> str:
    """Return a valid risk level, defaulting to 'none' for anything unexpected."""
    return raw if raw in _VALID_RISK_LEVELS else "none"


async def _load_plan_json(run: Run) -> str | None:
    """Fetch + clean + bound the run's plan JSON, or None if unavailable.

    Reuses the plan-summary path's redaction: ``_clean_plan_json_bytes`` strips
    every sensitive value BEFORE truncation, ``_fit_plan_json`` bounds the size.
    Both run in a thread (sync JSON work). Returns the model-ready string, or
    None when the artifact is missing.
    """
    storage = get_storage()
    key = plan_json_output_key(str(run.workspace_id), str(run.id))
    try:
        raw = await storage.get(key)
    except ObjectNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 — any storage error → treat as missing
        logger.info("plan JSON not available for critic", run_id=str(run.id), error=str(e))
        return None
    cleaned = await asyncio.to_thread(_clean_plan_json_bytes, raw)
    return await asyncio.to_thread(_fit_plan_json, cleaned, settings.ai_summary.plan_json_max_bytes)


async def handle_ai_architecture_critique(payload: dict) -> None:
    """Triggered handler: generate the AI architecture critique for a run.

    Payload: ``{"run_id": "<uuid>"}``. Enqueued when the plan JSON lands, only
    when ``ai_summary.enabled``. Every exit path settles the
    ``architecture_critiques`` row to a terminal state (ready / skipped /
    errored) and emits the matching SSE event so the run-page Security tab
    updates live.
    """
    cfg = settings.ai_summary
    if not cfg.enabled:
        return

    try:
        run_id = uuid.UUID(payload["run_id"])
    except (KeyError, ValueError):
        logger.warning("Invalid ai_architecture_critique payload", payload=payload)
        return

    async with get_db_session() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None or not run.has_json_output:
            return
        ws = (
            await db.execute(select(Workspace).where(Workspace.id == run.workspace_id))
        ).scalar_one_or_none()
        if ws is None:
            return

        # Per-workspace mode gate (global × workspace). OFF → record 'skipped'
        # so the UI shows a deliberate skipped state, not a spinner.
        if not _resolve_workspace_mode(ws):
            await _upsert_architecture_critique(
                db, run_id=run_id, status="skipped", model=cfg.model
            )
            await db.commit()
            await _emit_summary_event("architecture_critique_skipped", ws.id, run_id)
            return

        await _upsert_architecture_critique(db, run_id=run_id, status="pending", model=cfg.model)
        await db.commit()
        await _emit_summary_event("architecture_critique_pending", ws.id, run_id)

        # Budget gate — shared daily output-token budget with the plan summary.
        remaining = await _budget_remaining()
        if remaining is not None and remaining <= 0:
            await _upsert_architecture_critique(
                db, run_id=run_id, status="skipped", model=cfg.model
            )
            await db.commit()
            await _emit_summary_event("architecture_critique_skipped", ws.id, run_id)
            return

        # Read the plan JSON — the ONLY model input. Cleaned of sensitive values
        # + bounded via the plan-summary path's helpers (no raw state, ever).
        plan_json = await _load_plan_json(run)
        if plan_json is None:
            await _upsert_architecture_critique(
                db, run_id=run_id, status="skipped", model=cfg.model
            )
            await db.commit()
            await _emit_summary_event("architecture_critique_skipped", ws.id, run_id)
            return

        ctx = cfg.context
        system_message, user_message = render_architecture_critique_prompt(
            plan_json=plan_json,
            fleet_context=ctx.fleet_context,
            workspace_context=ws.ai_summary_context or "",
            prompt_prefix=ctx.prompt_prefix,
            prompt_suffix=ctx.prompt_suffix,
            output_language=cfg.summary_language,
        )

        try:
            args, input_tokens, output_tokens = await _call_model(
                kind="architecture_critique",
                system_message=system_message,
                user_message=user_message,
                max_output_tokens=cfg.max_output_tokens,
            )
        except Exception as e:  # noqa: BLE001 — any model/HTTP/parse failure
            logger.info("architecture critique model call failed", run_id=str(run_id), error=str(e))
            await _upsert_architecture_critique(
                db,
                run_id=run_id,
                status="errored",
                model=cfg.model,
                error_message=str(e)[:2000],
            )
            await db.commit()
            await _emit_summary_event("architecture_critique_errored", ws.id, run_id)
            return

        # A non-dict tool result is the only genuine failure. Empty
        # critique/findings at risk_level "none" is a legitimate "ready" state
        # (the proposed architecture is sound; nothing to flag).
        if not isinstance(args, dict):
            await _upsert_architecture_critique(
                db,
                run_id=run_id,
                status="errored",
                model=cfg.model,
                error_message="model returned no structured result",
            )
            await db.commit()
            await _emit_summary_event("architecture_critique_errored", ws.id, run_id)
            return

        critique = args.get("critique")
        critique = critique if isinstance(critique, str) else ""
        findings = _normalise_findings(args.get("findings"))
        risk_level = _normalise_risk_level(args.get("risk_level"))

        await _upsert_architecture_critique(
            db,
            run_id=run_id,
            status="ready",
            critique=critique,
            findings=findings,
            risk_level=risk_level,
            model=cfg.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await db.commit()
        await _budget_charge(output_tokens)
        await _emit_summary_event("architecture_critique_ready", ws.id, run_id)


# ---------------------------------------------------------------------------
# Chat follow-ups (#963/#1036) — Q&A thread grounded in the plan JSON
# ---------------------------------------------------------------------------

_ARCHITECTURE_CHAT_SYSTEM = (
    "You are a senior cloud/platform architect answering follow-up questions "
    "about a single Terraform/OpenTofu plan's PROPOSED infrastructure. You are "
    "given: (1) the plan's cleaned JSON (its `planned_values` describe the "
    "infrastructure as it will exist after apply; sensitive values are already "
    "stripped), and (2) the architectural critique + findings you already "
    "produced. Answer concisely in {language}, grounded ONLY in the materials "
    "provided. Your answers are advisory design guidance — never a gate, and "
    "never a restatement of the deterministic security-scan verdict. If a "
    "question can't be answered from what's provided, say so rather than "
    "guessing, and never invent resources or addresses not in the plan. No tool "
    "calls; plain prose."
)


def _render_architecture_chat_context(plan_json: str, critique: ArchitectureCritique) -> str:
    """The initial context turn for the architecture chat — the cleaned plan
    JSON plus the critique/findings the critic already produced."""
    import json

    ai = {
        "critique": critique.critique,
        "risk_level": critique.risk_level,
        "findings": critique.findings,
    }
    return (
        "PROPOSED_INFRASTRUCTURE (cleaned plan JSON — planned_values, sensitive "
        "values stripped):\n"
        f"```json\n{plan_json}\n```\n\n"
        "ARCHITECTURE_CRITIQUE (advisory reasoning already produced):\n"
        f"{json.dumps(ai, ensure_ascii=False)}"
    )


async def _build_architecture_followup_history(
    db: AsyncSession, critique: ArchitectureCritique, new_user_text: str
) -> list[dict]:
    """Chat history appended after the (system + initial-context) prefix:
    a framing exchange that establishes prose follow-up mode, prior turns in
    chronological order, then the just-posted user message."""
    history: list[dict] = [
        {
            "role": "user",
            "content": (
                "Thanks — the architecture critique above is recorded. I'd like "
                "to ask follow-up questions about this design in plain prose. "
                "Answer concisely, grounded only in the plan and critique above, "
                "as advisory design guidance."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Understood. I'll answer follow-up questions about this proposed "
                "architecture in prose, grounded in the plan and critique "
                "provided. What would you like to know?"
            ),
        },
    ]
    prior = (
        (
            await db.execute(
                sa.select(ArchitectureCritiqueMessage)
                .where(ArchitectureCritiqueMessage.architecture_critique_id == critique.id)
                .order_by(ArchitectureCritiqueMessage.created_at, ArchitectureCritiqueMessage.id)
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


async def post_architecture_followup(
    *,
    db: AsyncSession,
    architecture_critique: ArchitectureCritique,
    run: Run,
    workspace: Workspace,
    user_message_text: str,
) -> ArchitectureCritiqueMessage:
    """Process one operator follow-up turn on an architecture critique.

    Mirrors ``cost_summariser.post_cost_followup`` but grounded in the (cleaned)
    plan JSON rather than the cost estimate. The router has already authorised +
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
            .select_from(ArchitectureCritiqueMessage)
            .where(
                ArchitectureCritiqueMessage.architecture_critique_id == architecture_critique.id,
                ArchitectureCritiqueMessage.role == "user",
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
    db.add(
        ArchitectureCritiqueMessage(
            architecture_critique_id=architecture_critique.id, role="user", content=text
        )
    )
    await db.flush()

    # Ground the chat in the cleaned plan JSON — the ONLY external input (no
    # state). If it aged out, record an errored assistant turn.
    plan_json = await _load_plan_json(run)
    if plan_json is None:
        err = "no plan JSON available to ground the follow-up"
        db.add(
            ArchitectureCritiqueMessage(
                architecture_critique_id=architecture_critique.id,
                role="assistant",
                content="",
                model=cfg.model,
                error_message=err,
            )
        )
        await db.commit()
        raise FollowupError(err) from None

    system_message = _ARCHITECTURE_CHAT_SYSTEM.format(language=cfg.summary_language or "English")
    initial_user_message = _render_architecture_chat_context(plan_json, architecture_critique)
    history = await _build_architecture_followup_history(db, architecture_critique, text)

    try:
        reply_text, in_tok, out_tok = await _call_chat_model(
            system_message=system_message,
            user_message=initial_user_message,
            history=history,
            max_output_tokens=cfg.followup_max_output_tokens,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("architecture follow-up call failed", run_id=str(run.id), error=str(e))
        db.add(
            ArchitectureCritiqueMessage(
                architecture_critique_id=architecture_critique.id,
                role="assistant",
                content="",
                model=cfg.model,
                error_message=str(e)[:500],
            )
        )
        await db.commit()
        raise

    assistant_row = ArchitectureCritiqueMessage(
        architecture_critique_id=architecture_critique.id,
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
        "architecture_critique_message_posted",
        workspace.id,
        run.id,
        message_id=str(assistant_row.id),
    )
    return assistant_row
