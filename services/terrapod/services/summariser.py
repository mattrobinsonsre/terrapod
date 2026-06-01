"""AI plan summariser + plan-failure analyser (#401).

When enabled via ``ai_summary.enabled``, the API triggers an
asynchronous call after every plan-phase terminal transition:
  - ``planned`` → ``kind=plan_summary``: describe changes + rate risk.
  - ``errored`` while still in the plan phase → ``kind=failure_analysis``:
    explain why the plan failed and suggest fixes.

The model call goes through the LiteLLM Python library
(``litellm.acompletion``). Terrapod always speaks the OpenAI Chat
Completions request shape; LiteLLM handles the per-provider request
translation, auth, and response normalisation in-process. No gateway
deployment, no second pod.

The model string's prefix selects the provider:

  - ``bedrock/<model-id>`` — AWS Bedrock via boto3. IAM auth via the
    pod's IRSA service account; optional ``sts:AssumeRole`` hop into a
    cross-account role when ``auth.aws_role_arn`` is set. Works for
    Anthropic Claude, Amazon Nova, Mistral, Meta Llama, etc.
  - ``openai/<model-id>`` — OpenAI direct, with ``auth.api_key``.
  - ``anthropic/<model-id>`` — Anthropic direct, with ``auth.api_key``.
  - ``gemini/<model-id>`` — Google AI Studio, with ``auth.api_key``.
  - ``azure/<deployment-name>`` — Azure OpenAI, with ``api_base`` + key.
  - Self-hosted OpenAI-compat (vLLM, deployed LiteLLM proxy, etc.) —
    use ``openai/<model>`` with ``api_base`` pointing at the endpoint.

The model's JSON response is parsed and stored in the
``plan_summaries`` table; an SSE ``plan_summary_ready`` event is
emitted on the per-workspace channel so the UI re-fetches.

Daily token budget (``ai_summary.daily_token_budget``) is tracked in a
Redis counter per UTC day. Once exhausted, further calls are skipped
(``status='skipped'``) without raising — run lifecycle is never
affected by this feature.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import tarfile
import uuid

import litellm
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import PlanSummary, Run, Workspace, now_utc
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.summariser_prompt import render_prompt
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    config_version_key,
    plan_json_output_key,
    plan_log_key,
)

logger = get_logger(__name__)


# --- Input retrieval ---------------------------------------------------------


def _truncate_tail(data: bytes, max_bytes: int) -> str:
    """Return ``data`` decoded as UTF-8 (best-effort), tail-trimmed.

    Logs are tail-relevant — the failure is at the end. We drop the head
    when over budget rather than the tail.
    """
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    trimmed = data[-max_bytes:]
    return f"[... {len(data) - max_bytes} bytes truncated from head ...]\n" + trimmed.decode(
        "utf-8", errors="replace"
    )


def _truncate_head(data: bytes, max_bytes: int) -> str:
    """Return ``data`` decoded as UTF-8, truncated from the tail.

    Plan JSON's structural value is at the head — provider config and
    resource_changes come first.
    """
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    return (
        data[:max_bytes].decode("utf-8", errors="replace")
        + f"\n[... {len(data) - max_bytes} bytes truncated from tail ...]"
    )


def _extract_tf_sources(tarball: bytes, max_bytes: int) -> str:
    """Read .tf files from a config-version tarball, concatenated.

    Files are read in tar order until the byte cap is hit. Each file is
    prefixed with a header so the model can attribute snippets to paths.
    Defends against zip-bombs by bailing as soon as the cap is reached.
    """
    if max_bytes <= 0:
        return ""

    buf = io.StringIO()
    written = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(".tf"):
                    continue
                if member.size > max_bytes - written:
                    break
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                content = fobj.read().decode("utf-8", errors="replace")
                header = f"\n# === {member.name} ===\n"
                buf.write(header)
                buf.write(content)
                written += len(header) + len(content)
                if written >= max_bytes:
                    break
    except tarfile.TarError as e:
        logger.warning("Failed to read CV tarball for AI context", error=str(e))
        return ""
    return buf.getvalue()


async def _gather_inputs(run: Run, kind: str) -> tuple[str, str, str, str]:
    """Return ``(primary_input, primary_label, primary_lang, code_context)``.

    primary_input is the plan JSON (for plan_summary) or the plan log
    (for failure_analysis). code_context is the concatenated .tf source
    or "" when the workspace has no config version.
    """
    storage = get_storage()
    cfg = settings.ai_summary

    if kind == "plan_summary":
        key = plan_json_output_key(str(run.workspace_id), str(run.id))
        primary_label = "PLAN_JSON"
        primary_lang = "json"
        try:
            raw = await storage.get(key)
        except Exception as e:
            logger.warning(
                "plan JSON not available for summariser", run_id=str(run.id), error=str(e)
            )
            return "", primary_label, primary_lang, ""
        primary = _truncate_head(raw, cfg.plan_json_max_bytes)
    else:
        key = plan_log_key(str(run.workspace_id), str(run.id))
        primary_label = "PLAN_LOG"
        primary_lang = "text"
        try:
            raw = await storage.get(key)
        except Exception as e:
            logger.warning(
                "plan log not available for summariser", run_id=str(run.id), error=str(e)
            )
            return "", primary_label, primary_lang, ""
        primary = _truncate_tail(raw, cfg.plan_json_max_bytes)

    code_context = ""
    if cfg.code_context_max_bytes > 0 and run.configuration_version_id:
        cv_key = config_version_key(str(run.workspace_id), str(run.configuration_version_id))
        try:
            tarball = await storage.get(cv_key)
            code_context = await asyncio.to_thread(
                _extract_tf_sources, tarball, cfg.code_context_max_bytes
            )
        except Exception as e:
            logger.debug("CV tarball not available; running without code context", error=str(e))

    return primary, primary_label, primary_lang, code_context


# --- Daily budget ------------------------------------------------------------


def _budget_key() -> str:
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return f"tp:ai_summary:budget:{today}"


async def _budget_remaining() -> int | None:
    """Return remaining output-token budget, or None if unlimited."""
    cfg = settings.ai_summary
    if cfg.daily_token_budget <= 0:
        return None
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    spent_raw = await r.get(_budget_key())
    spent = int(spent_raw) if spent_raw else 0
    return max(0, cfg.daily_token_budget - spent)


async def _budget_charge(tokens: int) -> None:
    cfg = settings.ai_summary
    if cfg.daily_token_budget <= 0 or tokens <= 0:
        return
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    pipe = r.pipeline(transaction=False)
    pipe.incrby(_budget_key(), tokens)
    pipe.expire(_budget_key(), 60 * 60 * 36)  # span at least one UTC day
    await pipe.execute()


# --- Workspace mode resolution ----------------------------------------------


def _resolve_workspace_mode(ws: Workspace) -> bool:
    """Resolve the 3-state per-workspace toggle against the global flag.

    Truth table (global × workspace):
      enabled × default  → ON
      enabled × enabled  → ON  (UX may not even surface this state)
      enabled × disabled → OFF
      disabled × default  → OFF
      disabled × enabled  → OFF  (global wins; UX hides this state)
      disabled × disabled → OFF
    """
    if not settings.ai_summary.enabled:
        return False
    return ws.ai_summary_mode != "disabled"


# --- Model call --------------------------------------------------------------


def _build_litellm_kwargs(
    *, system_message: str, user_message: str, max_output_tokens: int
) -> dict:
    """Assemble the keyword arguments for ``litellm.acompletion``.

    Provider-specific keys (``api_key``, ``api_base``, ``aws_*``) are
    passed through unconditionally — LiteLLM ignores the ones that
    don't apply to the resolved provider, so this keeps the dispatch
    table flat (no per-provider branching in Terrapod).
    """
    cfg = settings.ai_summary
    auth = cfg.auth

    # We deliberately don't set response_format — Bedrock Converse for
    # Anthropic models rejects the OpenAI json_schema field with
    # "output_config.format: Extra inputs are not permitted", and other
    # providers' translations vary in quality. The skill prompt embeds
    # the JSON schema in the system message and instructs "Output JSON
    # only"; _parse_model_json forgives incidental fence wrapping. That
    # gives us portable behaviour across every LiteLLM backend.
    kwargs: dict = {
        "model": cfg.model,
        "max_tokens": max_output_tokens,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        "timeout": cfg.request_timeout_seconds,
    }

    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base
    if auth.api_key:
        kwargs["api_key"] = auth.api_key

    # AWS / Bedrock — LiteLLM only honours these when the model is
    # ``bedrock/...``; safe to send unconditionally.
    if auth.aws_region:
        kwargs["aws_region_name"] = auth.aws_region
    if auth.aws_role_arn:
        kwargs["aws_role_name"] = auth.aws_role_arn
        kwargs["aws_session_name"] = auth.aws_session_name
        if auth.aws_external_id:
            kwargs["aws_external_id"] = auth.aws_external_id

    return kwargs


async def _call_model(
    *,
    system_message: str,
    user_message: str,
    max_output_tokens: int,
) -> tuple[dict, int, int]:
    """Drive a Chat Completions call via the LiteLLM library.

    Returns ``(parsed_json, input_tokens, output_tokens)``. Raises on
    HTTP errors, missing choices, or JSON parse errors.
    """
    cfg = settings.ai_summary
    if not cfg.model:
        raise RuntimeError("ai_summary.model must be set")

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
    text = choice.message.content or ""
    finish_reason = getattr(choice, "finish_reason", None)

    # When the model runs out of output tokens it stops mid-JSON, so both
    # strict json.loads and the balanced-brace fallback fail with a
    # misleading "not JSON" — call out the real cause first.
    if finish_reason == "length":
        raise RuntimeError(
            f"model response truncated at max_output_tokens={max_output_tokens} "
            f"(finish_reason=length); raise ai_summary.max_output_tokens"
        )

    try:
        parsed = _parse_model_json(text)
    except ValueError as e:
        # Log a head+tail snippet so future failures are diagnosable
        # without rerunning. Limit size so we don't dump megabytes into
        # the structured log.
        snippet = text if len(text) <= 800 else f"{text[:400]}…{text[-400:]}"
        logger.warning(
            "Summariser response did not parse as JSON",
            finish_reason=finish_reason,
            response_length=len(text),
            response_snippet=snippet,
        )
        raise ValueError(f"{e} (finish_reason={finish_reason}, len={len(text)})") from e

    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return parsed, in_tok, out_tok


def _parse_model_json(text: str) -> dict:
    """Parse the model's textual response as a JSON object.

    Permits incidental wrapping (a leading ``"Here's the JSON:"`` line, a
    ```json fenced block, a stray sentence after the object) by trying:

      1. Strict parse of the whole stripped body.
      2. Strip a fenced ```json / ``` block if present.
      3. Extract the first balanced ``{...}`` block.

    The strict schema in the prompt should prevent any of this, but the
    fallbacks are cheap and reduce flakes from chatty models.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip a ```json ... ``` (or plain ``` ... ```) fence — Opus
    # occasionally adds one despite the "no fences" instruction.
    if text.startswith("```"):
        body = text[3:]
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.lstrip("\n")
        end = body.rfind("```")
        if end >= 0:
            try:
                return json.loads(body[:end].strip())
            except json.JSONDecodeError:
                pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("model response was not JSON")


# --- Trigger handler ---------------------------------------------------------


async def _emit_ready_event(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    try:
        from terrapod.redis.client import RUN_EVENTS_PREFIX, publish_event

        await publish_event(
            f"{RUN_EVENTS_PREFIX}{workspace_id}",
            json.dumps(
                {
                    "event": "plan_summary_ready",
                    "run_id": str(run_id),
                    "workspace_id": str(workspace_id),
                }
            ),
        )
    except Exception as e:  # SSE is best-effort
        logger.debug("Failed to publish plan_summary_ready", error=str(e))


async def _upsert_summary(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    kind: str,
    status: str,
    description: str = "",
    risk_level: str = "",
    risk_factors: list[dict] | None = None,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str = "",
) -> None:
    """Idempotent upsert keyed on (run_id).

    Never downgrades a 'ready' row — a later errored retry for the same
    run is a no-op so the UI doesn't lose a good summary to a transient
    network blip.
    """
    existing = (
        await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id))
    ).scalar_one_or_none()

    if existing is not None and existing.status == "ready" and status != "ready":
        return

    values = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "kind": kind,
        "status": status,
        "description": description,
        "risk_level": risk_level,
        "risk_factors": risk_factors or [],
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error_message": error_message,
        "updated_at": now_utc(),
    }
    stmt = pg_insert(PlanSummary).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "kind": stmt.excluded.kind,
            "status": stmt.excluded.status,
            "description": stmt.excluded.description,
            "risk_level": stmt.excluded.risk_level,
            "risk_factors": stmt.excluded.risk_factors,
            "model": stmt.excluded.model,
            "input_tokens": stmt.excluded.input_tokens,
            "output_tokens": stmt.excluded.output_tokens,
            "error_message": stmt.excluded.error_message,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)


async def handle_ai_plan_summary(payload: dict) -> None:
    """Triggered handler: summarise (or analyse the failure of) one plan.

    Payload: ``{"run_id": "<uuid>", "kind": "plan_summary" | "failure_analysis"}``.
    Enqueued from ``run_service.transition_run`` on plan-phase terminal
    transitions when the feature is globally enabled.
    """
    cfg = settings.ai_summary
    if not cfg.enabled:
        return

    try:
        run_id = uuid.UUID(payload["run_id"])
    except (KeyError, ValueError):
        logger.warning("Invalid ai_plan_summary payload", payload=payload)
        return

    kind = payload.get("kind") or "plan_summary"
    if kind not in {"plan_summary", "failure_analysis"}:
        logger.warning("Invalid ai_plan_summary kind", kind=kind)
        return

    async with get_db_session() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None:
            return

        ws = (
            await db.execute(select(Workspace).where(Workspace.id == run.workspace_id))
        ).scalar_one_or_none()
        if ws is None:
            return

        if not _resolve_workspace_mode(ws):
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="skipped",
                error_message="workspace disabled",
            )
            await db.commit()
            return

        remaining = await _budget_remaining()
        if remaining is not None and remaining <= 0:
            logger.info("AI summariser daily budget exhausted, skipping", run_id=str(run_id))
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="skipped",
                error_message="daily token budget exhausted",
            )
            await db.commit()
            await _emit_ready_event(ws.id, run_id)
            return

        primary, label, lang, code_context = await _gather_inputs(run, kind)
        if not primary:
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="errored",
                error_message=f"no {label} available",
            )
            await db.commit()
            return

        system_message, user_message = render_prompt(
            kind=kind,
            fleet_context=cfg.context.fleet_context,
            workspace_context=ws.ai_summary_context,
            primary_input=primary,
            primary_input_label=label,
            primary_input_lang=lang,
            code_context_truncated=code_context,
            prompt_prefix=cfg.context.prompt_prefix,
            prompt_suffix=cfg.context.prompt_suffix,
        )

        try:
            parsed, in_tok, out_tok = await _call_model(
                system_message=system_message,
                user_message=user_message,
                max_output_tokens=cfg.max_output_tokens,
            )
        except Exception as e:
            logger.warning(
                "AI summariser call failed",
                run_id=str(run_id),
                kind=kind,
                error=str(e),
            )
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="errored",
                model=cfg.model,
                error_message=str(e)[:500],
            )
            await db.commit()
            return

        description = str(parsed.get("description", ""))[:50_000]
        risk_level = str(parsed.get("risk_level", "")).lower()
        if risk_level not in {"low", "medium", "high", "critical"}:
            risk_level = "low"
        risk_factors = parsed.get("risk_factors") or []
        if not isinstance(risk_factors, list):
            risk_factors = []

        await _upsert_summary(
            db,
            run_id=run_id,
            kind=kind,
            status="ready",
            description=description,
            risk_level=risk_level,
            risk_factors=risk_factors,
            model=cfg.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        await db.commit()

    # Out-of-transaction side effects
    await _budget_charge(out_tok)
    await _emit_ready_event(ws.id, run_id)

    # PR/MR comment refresh — re-enqueue the existing vcs_commit_status
    # trigger so handle_vcs_commit_status picks up the now-ready
    # PlanSummary and edits it into the per-workspace comment in place.
    # The dedup key includes "aisum" so it doesn't collide with the
    # standard run-state-change enqueues from run_service.
    if run.vcs_pull_request_number:
        try:
            from terrapod.services.scheduler import enqueue_trigger

            await enqueue_trigger(
                "vcs_commit_status",
                {
                    "run_id": str(run_id),
                    "workspace_id": str(ws.id),
                    "target_status": run.status,
                    "has_changes": run.has_changes,
                },
                dedup_key=f"vcs_status:aisum:{run_id}",
                dedup_ttl=60,
            )
        except Exception as e:
            logger.debug("Failed to refresh PR comment after AI summary", error=str(e))

    logger.info(
        "AI summary ready",
        run_id=str(run_id),
        kind=kind,
        risk_level=risk_level,
        in_tok=in_tok,
        out_tok=out_tok,
    )
