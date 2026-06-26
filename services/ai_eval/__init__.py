"""Terrapod AI-analysis evaluation harness (#602).

A committed, CI-runnable suite for iteratively refining the AI analysis
prompts across the three surfaces — plan summaries, drift detection, and
apply-failure analysis — by driving the *real* shipping code path
(``terrapod.services.summariser_prompt.render_prompt`` +
``terrapod.services.summariser._call_model``) against a labelled corpus of
synthetic, OSS-safe fixtures.

Modules:
  - ``cases``     — the Case / Truth schema + corpus loader.
  - ``prep``      — build the production render_prompt inputs from a Case
                    (reuses the real plan-JSON cleaning + truncation helpers).
  - ``rubric``    — deterministic scoring against ground-truth labels
                    (risk band, must-flag / must-not-flag, churn-not-risk,
                    key facts, forbidden claims).
  - ``runner``    — drive a Case through the engine N times (repeatability).
  - ``judge``     — LLM-judge for description accuracy / utility.
  - ``report``    — aggregate scorecards + human-readable report + baseline.
  - ``generator`` — parametric synthesis of labelled plan-JSON fixtures.
  - ``__main__``  — CLI (``python -m ai_eval ...``).

No real-estate plan data is ever committed here (content hygiene); every
fixture is synthetic.
"""
