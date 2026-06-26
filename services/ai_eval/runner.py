"""Drive a Case through the real engine, N times, for repeatability (#602).

Reuses the production ``_build_litellm_kwargs`` (tools + tool_choice + messages
+ provider auth passthrough) and ``_parse_tool_call_arguments`` so the harness
exercises exactly the shipping request shape. The only overrides are the
**model** (so we can sweep candidates) and **temperature=0** (so repeatability
is a measurable property, not left to the provider default).

Credentials are ambient: for ``anthropic/<model>`` LiteLLM reads
``ANTHROPIC_API_KEY`` from the environment; for ``bedrock/<id>`` it uses the
pod/host AWS credentials. The harness passes neither in code.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import litellm

from terrapod.config import settings
from terrapod.services.summariser import _build_litellm_kwargs, _parse_tool_call_arguments

from .cases import Case
from .prep import build_messages


@dataclass
class RunOutput:
    """One model call's result (or its error)."""

    parsed: dict | None = None
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.parsed is not None and not self.error


@dataclass
class CaseRun:
    """All N repeats of one case against one model."""

    case_id: str
    model: str
    outputs: list[RunOutput] = field(default_factory=list)

    @property
    def successes(self) -> list[RunOutput]:
        return [o for o in self.outputs if o.ok]


async def _one_call(
    case: Case, model: str, max_output_tokens: int, temperature: float
) -> RunOutput:
    system_message, user_message = build_messages(case)
    kwargs = _build_litellm_kwargs(
        kind=case.kind,
        system_message=system_message,
        user_message=user_message,
        max_output_tokens=max_output_tokens,
    )
    kwargs["model"] = model
    if temperature is not None:
        kwargs["temperature"] = temperature
    # Bounded retry with backoff so Bedrock/anthropic throttling on a large
    # sweep degrades to slower, not to spurious call errors that corrupt the
    # scorecard. LiteLLM retries RateLimitError / transient 5xx internally.
    kwargs["num_retries"] = 5
    # Bedrock: ensure LiteLLM/boto3 has a region even when the summariser
    # settings don't carry one (the harness reads ambient AWS_* env creds).
    if model.startswith("bedrock/") and "aws_region_name" not in kwargs:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region:
            kwargs["aws_region_name"] = region
    try:
        resp = await litellm.acompletion(**kwargs)
        choice = resp.choices[0]
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        if not tool_calls:
            return RunOutput(error="model returned no tool_calls")
        parsed = _parse_tool_call_arguments(tool_calls[0])
        usage = getattr(resp, "usage", None)
        return RunOutput(
            parsed=parsed,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
    except Exception as e:  # noqa: BLE001 - record any provider/parse failure
        return RunOutput(error=str(e)[:500])


async def run_case(
    case: Case,
    *,
    model: str,
    n: int = 1,
    max_output_tokens: int | None = None,
    temperature: float = 0.0,
) -> CaseRun:
    """Run ``case`` ``n`` times against ``model``; collect every output."""
    max_tokens = max_output_tokens or settings.ai_summary.max_output_tokens
    outputs = [await _one_call(case, model, max_tokens, temperature) for _ in range(n)]
    return CaseRun(case_id=case.id, model=model, outputs=outputs)


async def run_sweep(
    cases: list[Case],
    *,
    model: str,
    n: int = 1,
    concurrency: int = 4,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> list[CaseRun]:
    """Run every case against one model with bounded concurrency."""
    sem = asyncio.Semaphore(concurrency)

    async def _guarded(case: Case) -> CaseRun:
        async with sem:
            return await run_case(
                case,
                model=model,
                n=n,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

    return await asyncio.gather(*(_guarded(c) for c in cases))
