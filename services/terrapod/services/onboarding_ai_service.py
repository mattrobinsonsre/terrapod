"""AI onboarding config polish handler (#824 Phase A).

Triggered task that turns a discovery session's machine-named config into a
human-readable one: resources renamed from their tags, grouped, and commented.
Runs in the API (never the runner — no model creds on Job pods), reusing the
summariser's LiteLLM machinery but keyed to the independent ``ai_onboarding``
config (its own model, endpoint, auth secret, and Redis token budget).

Safety is structural, not trusted: the model returns only naming decisions (see
``onboarding_polish_prompt``), and ``onboarding_polish.apply_polish`` copies every
attribute value and import id verbatim. A value-preservation assertion runs
before anything is persisted; any inconsistency keeps the raw config untouched
(``polished_config`` stays null, ``ai_assisted`` stays false) so the deterministic
output is never lost.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import litellm
from sqlalchemy import select

from terrapod.config import settings
from terrapod.db.models import OnboardingSession
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.onboarding_polish import (
    PolishError,
    ResourcePolish,
    apply_polish,
    assert_values_preserved,
)
from terrapod.services.onboarding_polish_prompt import (
    ONBOARDING_POLISH_TOOL,
    render_polish_prompt,
)

# Generic, config-agnostic helpers shared with the summariser (tool-call arg
# parsing, JSON fallback, Anthropic prompt-cache markers). Reused rather than
# duplicated; they take their inputs explicitly and read no ai_summary state.
from terrapod.services.summariser import (
    _apply_anthropic_cache_markers,
    _parse_model_json,
    _parse_tool_call_arguments,
    _supports_anthropic_cache_control,
)

logger = get_logger(__name__)

# Bounded retry for the model call — same rationale as the summariser (self-heal
# transient provider blips; a 4xx is final and not retried).
_LLM_NUM_RETRIES = 2  # → up to 3 attempts


# --- Daily budget (independent of ai_summary) --------------------------------


def _budget_key() -> str:
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return f"tp:ai_onboarding:budget:{today}"


async def _budget_remaining() -> int | None:
    """Remaining output-token budget for today, or None if unlimited."""
    cfg = settings.ai_onboarding
    if cfg.daily_token_budget <= 0:
        return None
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    spent_raw = await r.get(_budget_key())
    spent = int(spent_raw) if spent_raw else 0
    return max(0, cfg.daily_token_budget - spent)


async def _budget_charge(tokens: int) -> None:
    cfg = settings.ai_onboarding
    if cfg.daily_token_budget <= 0 or tokens <= 0:
        return
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    pipe = r.pipeline(transaction=False)
    pipe.incrby(_budget_key(), tokens)
    pipe.expire(_budget_key(), 60 * 60 * 36)  # span at least one UTC day
    await pipe.execute()


# --- Model call --------------------------------------------------------------


def _build_litellm_kwargs(
    *, system_message: str, user_message: str, max_output_tokens: int
) -> dict:
    """Assemble ``litellm.acompletion`` kwargs for the onboarding polish.

    Mirrors the summariser's builder but reads ``settings.ai_onboarding`` and
    forces the ``submit_resource_naming`` tool. Provider-specific auth keys are
    passed unconditionally — LiteLLM ignores the ones that don't apply.
    """
    cfg = settings.ai_onboarding
    auth = cfg.auth

    messages: list[dict] = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]
    if _supports_anthropic_cache_control(cfg.model):
        messages = _apply_anthropic_cache_markers(messages)

    tool_name = ONBOARDING_POLISH_TOOL["function"]["name"]
    kwargs: dict = {
        "model": cfg.model,
        "max_tokens": max_output_tokens,
        "messages": messages,
        "timeout": cfg.request_timeout_seconds,
        "num_retries": _LLM_NUM_RETRIES,
        "retry_strategy": "exponential_backoff_retry",
        "tools": [ONBOARDING_POLISH_TOOL],
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
    """Drive the tool-forced completion; return ``(parsed_args, in_tok, out_tok)``.

    Raises on HTTP error, no choices, or truncation. Falls back to parsing the
    body as JSON if a model ignores ``tool_choice`` (self-hosted endpoints).
    """
    cfg = settings.ai_onboarding
    if not cfg.model:
        raise RuntimeError("ai_onboarding.model must be set")

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
            f"(finish_reason=length); raise ai_onboarding.max_output_tokens"
        )

    tool_calls = getattr(choice.message, "tool_calls", None) or []
    if tool_calls:
        parsed = _parse_tool_call_arguments(tool_calls[0])
    else:
        text = choice.message.content or ""
        logger.warning(
            "onboarding polish: no tool_calls despite tool_choice; parsing body",
            finish_reason=finish_reason,
            response_length=len(text),
        )
        parsed = _parse_model_json(text)

    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return parsed, in_tok, out_tok


def _decisions_from_args(parsed: dict) -> tuple[list[ResourcePolish], str]:
    """Coerce the tool arguments into ``ResourcePolish`` decisions + file header.

    Defensive: skips malformed entries and stringifies fields. The heavy
    validation (address exists, name valid + unique) is ``apply_polish``'s job.
    """
    decisions: list[ResourcePolish] = []
    for r in parsed.get("resources") or []:
        if not isinstance(r, dict):
            continue
        address = str(r.get("address") or "").strip()
        if not address:
            continue
        decisions.append(
            ResourcePolish(
                address=address,
                new_name=str(r.get("new_name") or "").strip(),
                group=str(r.get("group") or "").strip(),
                comment=str(r.get("comment") or "").strip(),
            )
        )
    file_header = str(parsed.get("file_header") or "").strip()
    return decisions, file_header


# --- Trigger handler ---------------------------------------------------------


async def handle_onboarding_polish(payload: dict) -> None:
    """Triggered handler: polish a discovery session's generated config.

    Payload: ``{"session_id": "<uuid>"}``. Enqueued from ``complete_discovery``
    when a session reaches ``config_ready`` with a non-empty config and
    ``ai_onboarding.enabled``.

    Keyed on DATA PRESENCE (``generated_config`` non-empty, ``polished_config``
    unset), not on the status flip — the runner uploads the config before the
    reconciler flips ``config_ready``, so this is race-free regardless of
    enqueue-vs-commit ordering. Idempotent: a second run for an already-polished
    session no-ops.
    """
    cfg = settings.ai_onboarding
    if not cfg.enabled:
        return

    try:
        session_id = uuid.UUID(payload["session_id"])
    except KeyError, ValueError:
        logger.warning("invalid onboarding_polish payload", payload=payload)
        return

    async with get_db_session() as db:
        session = (
            await db.execute(select(OnboardingSession).where(OnboardingSession.id == session_id))
        ).scalar_one_or_none()
        if session is None:
            return
        # Data-presence + idempotency guards.
        if not session.generated_config:
            return  # nothing discovered to polish (or artifacts not yet uploaded)
        if session.polished_config is not None or session.ai_assisted:
            return  # already polished
        if session.status == "errored":
            return

        remaining = await _budget_remaining()
        if remaining is not None and remaining <= 0:
            logger.info("onboarding polish budget exhausted, skipping", session_id=str(session_id))
            return

        raw_config = session.generated_config
        raw_imports = session.import_blocks or ""
        provider = session.provider

    # Model call + apply happen OUTSIDE the DB session — no connection held for
    # the (potentially slow) completion.
    system_message, user_message = render_polish_prompt(
        provider=provider,
        generated_config=raw_config,
        fleet_context="",
        workspace_context="",
    )
    try:
        parsed, in_tok, out_tok = await _call_model(
            system_message=system_message,
            user_message=user_message,
            max_output_tokens=cfg.max_output_tokens,
        )
    except Exception as e:  # noqa: BLE001 — polish is best-effort; raw config stands
        logger.warning(
            "onboarding polish model call failed", session_id=str(session_id), error=str(e)
        )
        return

    decisions, file_header = _decisions_from_args(parsed)

    # apply_polish / assert_values_preserved are pure CPU (regex over text) — run
    # off the event loop per the no-sync-work-in-async rule for large estates.
    try:
        result = await asyncio.to_thread(
            apply_polish, raw_config, raw_imports, decisions, file_header=file_header
        )
        await asyncio.to_thread(assert_values_preserved, raw_config, result.config)
    except PolishError as e:
        # Tokens were spent; charge them, but keep the raw config (fail safe).
        logger.warning(
            "onboarding polish rejected — keeping raw config",
            session_id=str(session_id),
            reason=str(e),
        )
        await _budget_charge(out_tok)
        return

    # Persist the polish. Re-guard idempotency inside the write transaction in
    # case a concurrent handler won the race while the model call was in flight.
    async with get_db_session() as db:
        session = (
            await db.execute(select(OnboardingSession).where(OnboardingSession.id == session_id))
        ).scalar_one_or_none()
        if session is None:
            return
        if session.polished_config is None and not session.ai_assisted:
            session.polished_config = result.config
            session.polished_import_blocks = result.import_blocks
            session.ai_assisted = True
            await db.commit()

    await _budget_charge(out_tok)
    logger.info(
        "onboarding polish ready",
        session_id=str(session_id),
        renamed=result.renamed,
        grouped=result.grouped,
        commented=result.commented,
        in_tok=in_tok,
        out_tok=out_tok,
    )
