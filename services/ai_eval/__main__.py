"""CLI for the AI-eval harness (#602).

    python -m ai_eval list [--include all]
    python -m ai_eval run --model anthropic/claude-sonnet-4-6 [--model ...] \
        [-n 3] [--surfaces plan,drift] [--tags data_loss,security] \
        [--include all] [--limit N] [--temperature 0] [--out reports/ai-eval]

Credentials are ambient (ANTHROPIC_API_KEY for anthropic/*, AWS env for
bedrock/*). The deterministic corpus + rubric need no creds — only ``run``
calls the model.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .cases import Case, load_corpus
from .generator import build_generated_cases
from .report import score_model, write_reports
from .runner import run_sweep


def _collect_cases(args: argparse.Namespace) -> list[Case]:
    include = args.include
    cases: list[Case] = []
    if include in ("all", "curated"):
        cases += load_corpus()
    if include in ("all", "generated"):
        cases += build_generated_cases()
    if args.surfaces:
        wanted = set(args.surfaces.split(","))
        cases = [c for c in cases if c.surface in wanted]
    if args.tags:
        wanted = set(args.tags.split(","))
        cases = [c for c in cases if set(c.tags) & wanted]
    if args.limit:
        cases = cases[: args.limit]
    return cases


def _cmd_list(args: argparse.Namespace) -> int:
    cases = _collect_cases(args)
    by_surface: dict[str, int] = {}
    for c in cases:
        by_surface[c.surface] = by_surface.get(c.surface, 0) + 1
    print(f"{len(cases)} cases: " + ", ".join(f"{k}={v}" for k, v in sorted(by_surface.items())))
    for c in cases:
        print(f"  {c.id:50s} {c.surface:14s} [{','.join(c.tags)}]")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cases = _collect_cases(args)
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    cases_by_id = {c.id: c for c in cases}
    models = args.model
    stamp = time.strftime("%Y%m%d-%H%M%S")
    print(f"running {len(cases)} cases x {len(models)} model(s) x n={args.n} ...", file=sys.stderr)

    reports = []
    for model in models:
        runs = asyncio.run(
            run_sweep(
                cases,
                model=model,
                n=args.n,
                concurrency=args.concurrency,
                temperature=args.temperature,
            )
        )
        rep = score_model(cases_by_id, runs)
        reports.append(rep)
        tr, ho = rep.split_counts()
        print(
            f"  {model}: hard-pass {rep.hard_pass_rate:.0%} "
            f"(train {rep.hard_pass_rate_split(False):.0%} n={tr} / "
            f"holdout {rep.hard_pass_rate_split(True):.0%} n={ho})  "
            f"mean {rep.mean_score:.2f}  risk-repeat {rep.mean_risk_repeatability:.0%}",
            file=sys.stderr,
        )

    md_path, json_path = write_reports(reports, args.out, stamp=stamp)
    print(f"\nwrote {md_path}\n      {json_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    import pathlib

    p = argparse.ArgumentParser(prog="ai_eval")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--include", choices=["all", "curated", "generated"], default="all")
        sp.add_argument("--surfaces", default="", help="comma list: plan,drift,apply_failure")
        sp.add_argument("--tags", default="", help="comma list of risk-axis tags")
        sp.add_argument("--limit", type=int, default=0)

    lp = sub.add_parser("list", help="list selected cases")
    add_common(lp)
    lp.set_defaults(func=_cmd_list)

    rp = sub.add_parser("run", help="run the model sweep + score")
    add_common(rp)
    rp.add_argument(
        "--model", action="append", required=True, help="repeatable LiteLLM model string"
    )
    rp.add_argument("-n", type=int, default=1, help="repeats per case (repeatability)")
    rp.add_argument("--temperature", type=float, default=0.0)
    rp.add_argument("--concurrency", type=int, default=4)
    rp.add_argument("--out", type=pathlib.Path, default=pathlib.Path("reports/ai-eval"))
    rp.set_defaults(func=_cmd_run)

    jp = sub.add_parser("judge", help="LLM-judge the descriptions in a saved scorecard")
    jp.add_argument("--scorecard", type=pathlib.Path, required=True)
    jp.add_argument("--model", default="bedrock/us.anthropic.claude-sonnet-4-6")
    jp.add_argument("--concurrency", type=int, default=3)
    jp.set_defaults(func=_cmd_judge)

    args = p.parse_args(argv)
    return args.func(args)


def _cmd_judge(args: argparse.Namespace) -> int:
    from .judge import judge_scorecard, summarise

    judgements = asyncio.run(
        judge_scorecard(args.scorecard, model=args.model, concurrency=args.concurrency)
    )
    s = summarise(judgements)
    print(f"description quality (n={s.get('n', 0)}, judge={args.model}):", file=sys.stderr)
    for k in ("accuracy", "utility", "clarity", "mean"):
        if k in s:
            print(f"  {k:10s} {s[k]}", file=sys.stderr)
    for j in s.get("weakest", []):
        print(f"  weak: {j.case_id} ({j.mean:.1f}) — {j.rationale}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
