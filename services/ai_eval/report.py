"""Aggregate CaseRun outputs into scorecards + a human report (#602).

For each case we score every successful repeat with the deterministic rubric,
then derive:
  - a headline pass/score from the representative (first successful) output;
  - a repeatability metric: how stable the risk_level + hard_pass are across
    the N repeats (1.0 = identical every time).

Across the corpus we report overall hard-pass rate, mean score, per-axis pass
rates, and breakdowns by surface and by risk-axis tag — plus, when several
models are swept, a side-by-side leaderboard so we can pick the cheapest model
that clears the bar.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import rubric
from .cases import Case, is_holdout
from .runner import CaseRun


@dataclass
class CaseScore:
    case_id: str
    surface: str
    tags: list[str]
    title: str
    n_runs: int
    n_ok: int
    hard_pass: bool  # representative run
    overall_score: float  # representative run
    axis_pass: dict[str, bool]
    risk_repeatability: float  # fraction of runs sharing the modal risk_level
    hard_pass_repeatability: float  # fraction of runs that hard-pass
    failures: list[str]
    error: str = ""
    # Representative model output (first successful run) — so a failure can be
    # diagnosed (real miss vs scoring-matcher gap vs label problem) without a
    # re-run. This is what makes the iterate loop tractable.
    risk_level: str = ""
    description: str = ""
    risk_factors: list[dict] = field(default_factory=list)
    holdout: bool = False


@dataclass
class ModelReport:
    model: str
    case_scores: list[CaseScore] = field(default_factory=list)

    # --- aggregates ----------------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.case_scores)

    @property
    def hard_pass_rate(self) -> float:
        return _mean([1.0 if c.hard_pass else 0.0 for c in self.case_scores])

    @property
    def mean_score(self) -> float:
        return _mean([c.overall_score for c in self.case_scores])

    @property
    def mean_risk_repeatability(self) -> float:
        return _mean([c.risk_repeatability for c in self.case_scores])

    def hard_pass_rate_split(self, holdout: bool) -> float:
        """Hard-pass rate over just the train (holdout=False) or holdout subset.

        The prompt is tuned against train only; holdout is the honest
        generalization signal. A train gain that doesn't carry to holdout is
        overfitting and must be reverted."""
        xs = [1.0 if c.hard_pass else 0.0 for c in self.case_scores if c.holdout == holdout]
        return _mean(xs)

    def split_counts(self) -> tuple[int, int]:
        train = sum(1 for c in self.case_scores if not c.holdout)
        return train, len(self.case_scores) - train

    def axis_pass_rates(self) -> dict[str, float]:
        names = ["risk_band", "must_flag", "no_false_risk", "key_facts", "no_forbidden"]
        return {
            ax: _mean([1.0 if c.axis_pass.get(ax) else 0.0 for c in self.case_scores])
            for ax in names
        }

    def by_surface(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for c in self.case_scores:
            out.setdefault(c.surface, []).append(1.0 if c.hard_pass else 0.0)
        return {k: _mean(v) for k, v in sorted(out.items())}

    def by_tag(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for c in self.case_scores:
            for t in c.tags:
                out.setdefault(t, []).append(1.0 if c.hard_pass else 0.0)
        return {k: _mean(v) for k, v in sorted(out.items())}

    def failing_cases(self) -> list[CaseScore]:
        return [c for c in self.case_scores if not c.hard_pass]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def score_model(cases_by_id: dict[str, Case], runs: list[CaseRun]) -> ModelReport:
    """Score one model's sweep into a ModelReport."""
    model = runs[0].model if runs else "?"
    rep = ModelReport(model=model)
    for run in runs:
        case = cases_by_id[run.case_id]
        oks = run.successes
        if not oks:
            err = run.outputs[0].error if run.outputs else "no output"
            rep.case_scores.append(
                CaseScore(
                    case_id=case.id,
                    surface=case.surface,
                    tags=list(case.tags),
                    title=case.title,
                    n_runs=len(run.outputs),
                    n_ok=0,
                    hard_pass=False,
                    overall_score=0.0,
                    axis_pass={},
                    risk_repeatability=0.0,
                    hard_pass_repeatability=0.0,
                    failures=[f"call_error: {err}"],
                    error=err,
                    holdout=is_holdout(case),
                )
            )
            continue

        scored = [rubric.score(case, o.parsed) for o in oks]
        rep_score = scored[0]  # representative = first successful run
        risk_levels = [str(o.parsed.get("risk_level", "")).lower() for o in oks]
        modal = Counter(risk_levels).most_common(1)[0][1]
        risk_rep = modal / len(risk_levels)
        hp_rep = _mean([1.0 if s.hard_pass else 0.0 for s in scored])

        rep.case_scores.append(
            CaseScore(
                case_id=case.id,
                surface=case.surface,
                tags=list(case.tags),
                title=case.title,
                n_runs=len(run.outputs),
                n_ok=len(oks),
                hard_pass=rep_score.hard_pass,
                overall_score=round(rep_score.overall_score, 3),
                axis_pass={a.name: a.passed for a in rep_score.axes},
                risk_repeatability=round(risk_rep, 3),
                hard_pass_repeatability=round(hp_rep, 3),
                failures=rep_score.failures,
                risk_level=str(oks[0].parsed.get("risk_level", "")),
                description=str(oks[0].parsed.get("description", "")),
                risk_factors=[
                    f for f in (oks[0].parsed.get("risk_factors") or []) if isinstance(f, dict)
                ],
                holdout=is_holdout(case),
            )
        )
    return rep


# --- rendering ---------------------------------------------------------------


def render_markdown(reports: list[ModelReport]) -> str:
    lines: list[str] = ["# AI-eval scorecard", ""]

    if len(reports) > 1:
        lines += [
            "## Model leaderboard",
            "",
            "| Model | Hard-pass | Mean score | Risk repeatability |",
            "|---|---|---|---|",
        ]
        for r in sorted(reports, key=lambda r: r.hard_pass_rate, reverse=True):
            lines.append(
                f"| `{r.model}` | {r.hard_pass_rate:.0%} | {r.mean_score:.2f} | "
                f"{r.mean_risk_repeatability:.0%} |"
            )
        lines.append("")

    for r in reports:
        lines += [
            f"## `{r.model}`",
            "",
            f"- cases: **{r.n}**",
            f"- hard-pass (risk-correctness): **{r.hard_pass_rate:.0%}**",
            f"- hard-pass train / **holdout**: "
            f"{r.hard_pass_rate_split(False):.0%} / **{r.hard_pass_rate_split(True):.0%}** "
            f"(n={r.split_counts()[0]}/{r.split_counts()[1]})",
            f"- mean score: **{r.mean_score:.2f}**",
            f"- risk repeatability: **{r.mean_risk_repeatability:.0%}**",
            "",
        ]
        lines += ["**Per-axis pass rate**", ""]
        for ax, v in r.axis_pass_rates().items():
            lines.append(f"- {ax}: {v:.0%}")
        lines += ["", "**By surface**", ""]
        for s, v in r.by_surface().items():
            lines.append(f"- {s}: {v:.0%}")
        lines += ["", "**By risk axis (tag)**", ""]
        for t, v in r.by_tag().items():
            lines.append(f"- {t}: {v:.0%}")
        fails = r.failing_cases()
        lines += ["", f"**Failing cases ({len(fails)})**", ""]
        for c in fails:
            lines.append(f"- `{c.case_id}` ({c.surface}) — {'; '.join(c.failures) or 'n/a'}")
        lines.append("")
    return "\n".join(lines)


def write_reports(reports: list[ModelReport], out_dir: Path, *, stamp: str) -> tuple[Path, Path]:
    """Write the markdown report + a machine JSON. ``stamp`` is supplied by the
    caller (the harness can't call Date.now()/time in a way that breaks
    reproducibility for the journal; the CLI passes an explicit stamp)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"scorecard-{stamp}.md"
    json_path = out_dir / f"scorecard-{stamp}.json"
    md_path.write_text(render_markdown(reports), encoding="utf-8")
    json_path.write_text(json.dumps([_report_json(r) for r in reports], indent=2), encoding="utf-8")
    return md_path, json_path


def _report_json(r: ModelReport) -> dict:
    return {
        "model": r.model,
        "n": r.n,
        "hard_pass_rate": round(r.hard_pass_rate, 4),
        "mean_score": round(r.mean_score, 4),
        "mean_risk_repeatability": round(r.mean_risk_repeatability, 4),
        "axis_pass_rates": {k: round(v, 4) for k, v in r.axis_pass_rates().items()},
        "by_surface": {k: round(v, 4) for k, v in r.by_surface().items()},
        "by_tag": {k: round(v, 4) for k, v in r.by_tag().items()},
        "cases": [asdict(c) for c in r.case_scores],
    }
