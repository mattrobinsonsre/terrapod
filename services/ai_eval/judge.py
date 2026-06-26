"""LLM-judge for description quality (#602).

The deterministic rubric scores risk-correctness (band / must-flag / churn),
but it is blind to the thing prompt framing most affects: how good the prose
`description` actually is for an on-call engineer. This judge fills that gap.

It runs POST-HOC over a saved scorecard JSON: for each case it re-loads the
plan facts (by id, from the corpus), pairs them with the model's stored
`description`, and asks a judge model to score three axes 1–5:

  • accuracy  — factually correct about what the plan does; no fabrication;
                doesn't miss the headline change.
  • utility   — actionable for an on-call SRE; surfaces what matters at the
                right altitude; not a restatement of the JSON.
  • clarity   — concise, well-structured operator prose.

Because both prompts are judged by the SAME judge model, the comparison is
fair for A/B (relative), even though absolute scores carry the usual
self-grading caveat. Use a different judge model for an unbiased absolute read.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import litellm

from .cases import Case, load_corpus
from .generator import build_generated_cases

_JUDGE_SYSTEM = """\
You are a pragmatic, hard-to-impress senior site reliability engineer. You are
grading an AI-generated SUMMARY of a terraform/tofu plan that one of your
on-call engineers would read before approving the change. You are given the
plan's machine facts (PLAN_JSON or the failure log) and the candidate SUMMARY.

Score the SUMMARY on three axes, each an integer 1–5 (5 = excellent, 3 =
adequate, 1 = poor). Be critical and discriminating — most real summaries are
a 3 or 4; reserve 5 for genuinely excellent, 1–2 for misleading or useless.

  • accuracy — Is every claim about what the plan does correct and supported by
    the facts? No fabricated resources, no invented attributes, no missed
    headline change (e.g. a destroy of a stateful resource not mentioned).
  • utility — Would an on-call SRE find this actionable? Does it lead with what
    matters (risk, blast radius, exposure), at the right altitude — not a
    line-by-line restatement of the JSON, not vague hand-waving?
  • clarity — Concise, well-structured operator prose. Easy to skim under
    pressure. No filler, no format noise.

Call `submit_judgement` exactly once with the three integer scores and one
sentence of rationale naming the single biggest strength or weakness.
"""

_JUDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_judgement",
        "description": "Submit the 1-5 scores for the candidate summary.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["accuracy", "utility", "clarity", "rationale"],
            "properties": {
                "accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
                "utility": {"type": "integer", "minimum": 1, "maximum": 5},
                "clarity": {"type": "integer", "minimum": 1, "maximum": 5},
                "rationale": {"type": "string", "maxLength": 300},
            },
        },
    },
}


@dataclass
class Judgement:
    case_id: str
    accuracy: int = 0
    utility: int = 0
    clarity: int = 0
    rationale: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def mean(self) -> float:
        return (self.accuracy + self.utility + self.clarity) / 3.0


def _facts(case: Case) -> str:
    if case.surface == "apply_failure":
        return f"APPLY_LOG:\n{case.apply_log[:8000]}"
    return f"PLAN_JSON:\n{json.dumps(case.plan_json)[:12000]}"


def _all_cases_by_id() -> dict[str, Case]:
    cases = load_corpus() + build_generated_cases()
    return {c.id: c for c in cases}


async def _judge_one(case: Case, description: str, model: str) -> Judgement:
    user = (
        f"{_facts(case)}\n\nCANDIDATE SUMMARY:\n{description}\n\n"
        "Call submit_judgement with your scores."
    )
    kwargs: dict = {
        "model": model,
        "max_tokens": 500,
        "temperature": 0.0,
        "num_retries": 5,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        "tools": [_JUDGE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "submit_judgement"}},
    }
    if model.startswith("bedrock/"):
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region:
            kwargs["aws_region_name"] = region
    try:
        resp = await litellm.acompletion(**kwargs)
        tc = (resp.choices[0].message.tool_calls or [None])[0]
        if tc is None:
            return Judgement(case.id, error="no tool_call")
        args = json.loads(tc.function.arguments)
        return Judgement(
            case.id,
            accuracy=int(args.get("accuracy", 0)),
            utility=int(args.get("utility", 0)),
            clarity=int(args.get("clarity", 0)),
            rationale=str(args.get("rationale", "")),
        )
    except Exception as e:  # noqa: BLE001
        return Judgement(case.id, error=str(e)[:300])


async def judge_scorecard(path: Path, *, model: str, concurrency: int = 3) -> list[Judgement]:
    """Judge every case's representative description in a saved scorecard JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    report = data[0] if isinstance(data, list) else data
    by_id = _all_cases_by_id()
    sem = asyncio.Semaphore(concurrency)

    async def _guard(case_entry: dict) -> Judgement | None:
        cid = case_entry["case_id"]
        desc = case_entry.get("description", "")
        case = by_id.get(cid)
        if case is None or not desc:
            return None
        async with sem:
            return await _judge_one(case, desc, model)

    results = await asyncio.gather(*(_guard(c) for c in report["cases"]))
    return [r for r in results if r is not None]


def summarise(judgements: list[Judgement]) -> dict:
    ok = [j for j in judgements if j.ok]
    if not ok:
        return {"n": 0}

    def _avg(attr: str) -> float:
        return round(sum(getattr(j, attr) for j in ok) / len(ok), 2)

    return {
        "n": len(ok),
        "accuracy": _avg("accuracy"),
        "utility": _avg("utility"),
        "clarity": _avg("clarity"),
        "mean": round(sum(j.mean for j in ok) / len(ok), 2),
        "weakest": sorted(ok, key=lambda j: j.mean)[:5],
    }
