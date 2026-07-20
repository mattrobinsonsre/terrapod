"""AI *enhancement* of a run's cost estimate (#871).

This is the optional AI layer over the deterministic, oiq-derived cost estimate.
It rides the SAME switch as the plan summary (``ai_summary.enabled`` + the
per-workspace mode) and reuses the plan summariser's model plumbing — the
LiteLLM tool-calling call (:func:`summariser._call_model`), the shared daily
token budget, and the per-workspace run-events SSE channel.

**Provenance is a hard invariant (the whole reason cost is data-first).** This
service produces AI *polish* only — a plain-language narrative of the estimate
plus optional savings advisories — stored in ``cost_summaries``. It NEVER
computes, restates, or overrides the authoritative figures: those come from the
runner-produced ``cost_estimate.json`` artifact (oiq-derived) and are served by
the data-only ``/runs/{id}/cost-estimate`` endpoint. Every advisory dollar
amount is stamped ``source: "ai-estimate"`` **server-side here**, regardless of
what the model returns, and is never a gate.

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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from terrapod.config import settings
from terrapod.db.models import CostSummary, Run, Workspace, now_utc
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.summariser import (
    _budget_charge,
    _budget_remaining,
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
    narrative: str = "",
    advisories: list[dict] | None = None,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str = "",
) -> None:
    """Idempotent upsert keyed on (run_id); never downgrades a 'ready' row.

    Mirrors ``summariser._upsert_summary`` so a transient errored retry can't
    clobber a good narrative.
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

        narrative = args.get("narrative") if isinstance(args, dict) else None
        advisories = _normalise_advisories(
            args.get("advisories") if isinstance(args, dict) else None
        )
        if not isinstance(narrative, str) or not narrative.strip():
            await _upsert_cost_summary(
                db,
                run_id=run_id,
                status="errored",
                model=cfg.model,
                error_message="model returned no narrative",
            )
            await db.commit()
            await _emit_summary_event("cost_summary_errored", ws.id, run_id)
            return

        await _upsert_cost_summary(
            db,
            run_id=run_id,
            status="ready",
            narrative=narrative,
            advisories=advisories,
            model=cfg.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await db.commit()
        await _budget_charge(output_tokens)
        await _emit_summary_event("cost_summary_ready", ws.id, run_id)
