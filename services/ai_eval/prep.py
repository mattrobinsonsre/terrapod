"""Build production render_prompt inputs from a Case (#602).

This deliberately reuses the *real* shipping helpers from
``terrapod.services.summariser`` (plan-JSON cleaning, head/tail truncation)
and the real ``render_prompt`` so the harness scores exactly what ships — not
a parallel re-implementation. The only things held empty are the operator
context layers (``fleet_context`` / per-workspace context / prefix / suffix):
the eval targets the in-code *skill prompt*, which is the part we refine and
which every deployment shares.
"""

from __future__ import annotations

import json

from terrapod.config import settings
from terrapod.services.summariser import (
    _clean_plan_json_bytes,
    _fit_plan_json,
    _truncate_tail,
)
from terrapod.services.summariser_prompt import render_prompt

from .cases import Case


def build_messages(
    case: Case,
    *,
    fleet_context: str = "",
    workspace_context: str = "",
) -> tuple[str, str]:
    """Return ``(system_message, user_message)`` for a Case via the prod path.

    Mirrors ``summariser._handle_ai_plan_summary`` lines that assemble inputs:
      - plan / drift: primary = ``_truncate_head(_clean_plan_json_bytes(raw))``,
        label ``PLAN_JSON``, lang ``json``.
      - apply_failure: primary = ``_truncate_tail(raw)``, label ``APPLY_LOG``,
        lang ``text``.
    """
    cfg = settings.ai_summary
    max_bytes = cfg.plan_json_max_bytes

    if case.surface in ("plan", "drift"):
        raw = json.dumps(case.plan_json or {}).encode("utf-8")
        cleaned = _clean_plan_json_bytes(raw)
        primary = _fit_plan_json(cleaned, max_bytes)
        label, lang = "PLAN_JSON", "json"
    elif case.surface == "apply_failure":
        primary = _truncate_tail(case.apply_log.encode("utf-8"), max_bytes)
        label, lang = "APPLY_LOG", "text"
    else:  # pragma: no cover - guarded by the Surface literal
        raise ValueError(f"unknown surface {case.surface!r}")

    return render_prompt(
        kind=case.kind,
        fleet_context=fleet_context,
        workspace_context=workspace_context,
        primary_input=primary,
        primary_input_label=label,
        primary_input_lang=lang,
        code_context_truncated=case.code_context,
        code_diff=case.code_diff,
        prompt_prefix="",
        prompt_suffix="",
        state_diverged=case.state_diverged,
        drift_detection=case.drift_detection,
    )
