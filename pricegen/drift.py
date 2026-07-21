#!/usr/bin/env python3
"""Drift guardrail for the scheduled publish job (#922).

A **self-generated** pricesheet is exposed to a failure mode a hosted feed hides
from us: a cloud provider quietly renames a product family, adds an enum value
(a new engine / volume type / machine family), or restructures its API — and our
recipes then silently emit **fewer rows, or wrong ones**. This diffs the run's
drift **manifest** (from ``publish.py``) against the **last published** manifest
and decides whether the new sheet is safe to publish:

* **BLOCK** — a recipe vanished, or its row count fell below its floor
  (``min_rows`` in ``health.yaml``): the family regex / select almost certainly
  stopped matching. The scheduled job must NOT publish the collapsed sheet over
  the good one, and should open/refresh an issue.
* **WARN** — a new unmapped value the recipe doesn't cover (and isn't on the
  expected-drop allowlist), a large row-count move, or a price swing. Surfaced,
  not blocking.

Runs only on the weekly schedule (it needs two manifests across runs), never
per-PR. Pure logic (``check_drift``) so it is unit tested with synthetic
manifests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).parent


@dataclass
class Finding:
    severity: str  # "block" | "warn"
    recipe: str
    message: str


@dataclass
class DriftReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def issue_body(self) -> str:
        """A deterministic markdown body for the drift issue (stable title →
        update-in-place, no duplicate spam)."""
        if not self.findings:
            return "No pricesheet drift detected."
        blocks = [f for f in self.findings if f.severity == "block"]
        warns = [f for f in self.findings if f.severity == "warn"]
        lines = ["## pricegen drift detected", ""]
        if blocks:
            lines.append("### 🛑 Blocking — sheet NOT published")
            lines += [f"- **{f.recipe}**: {f.message}" for f in blocks]
            lines.append("")
        if warns:
            lines.append("### ⚠️ Warnings")
            lines += [f"- **{f.recipe}**: {f.message}" for f in warns]
        return "\n".join(lines)


def _by_recipe(manifest: dict) -> dict[str, dict]:
    return {r["recipe"]: r for r in manifest.get("recipes", [])}


def check_drift(prev: dict, curr: dict, health: dict) -> DriftReport:
    """Diff ``curr`` manifest against ``prev`` under ``health`` thresholds."""
    report = DriftReport()
    prev_by, curr_by = _by_recipe(prev), _by_recipe(curr)
    floors = health.get("min_rows", {})
    allow = health.get("known_unmapped", {})
    swing = float(health.get("price_swing_factor", 10))
    band = float(health.get("row_delta_fraction", 0.5))

    # A recipe present last publish but gone now — hard block.
    for name, prev_r in prev_by.items():
        if name not in curr_by:
            report.findings.append(
                Finding(
                    "block",
                    name,
                    f"vanished from the sheet (was {prev_r['rows']} rows)",
                )
            )

    for name, cur in curr_by.items():
        prev_r = prev_by.get(name)
        floor = floors.get(name, 1)
        if cur["rows"] < floor:
            report.findings.append(
                Finding(
                    "block",
                    name,
                    f"produced {cur['rows']} rows (< floor {floor}) — the product-family "
                    "regex / select likely stopped matching (renamed family?)",
                )
            )
        elif prev_r and prev_r["rows"] > 0:
            frac = abs(cur["rows"] - prev_r["rows"]) / prev_r["rows"]
            if frac > band:
                report.findings.append(
                    Finding(
                        "warn",
                        name,
                        f"row count moved {prev_r['rows']} → {cur['rows']} ({frac:.0%})",
                    )
                )

        # New unmapped values not on the allowlist and not seen last publish.
        patterns = {
            f: [re.compile(p) for p in ps] for f, ps in allow.get(name, {}).items()
        }
        prev_unmapped = (prev_r or {}).get("unmapped", {})
        for fieldname, values in cur.get("unmapped", {}).items():
            known_prev = set(prev_unmapped.get(fieldname, {}))
            for value in values:
                if value in known_prev:
                    continue
                if any(rx.search(value) for rx in patterns.get(fieldname, [])):
                    continue
                report.findings.append(
                    Finding(
                        "warn",
                        name,
                        f"new unmapped {fieldname}={value!r} — a value the feed added "
                        "that the recipe doesn't cover (map/regex may need an update)",
                    )
                )

        # Price anomaly on the top of the range.
        pmax_prev, pmax_cur = (prev_r or {}).get("price_max"), cur.get("price_max")
        if prev_r and pmax_prev and pmax_cur:
            ratio = pmax_cur / pmax_prev
            if ratio > swing or ratio < 1 / swing:
                report.findings.append(
                    Finding("warn", name, f"max price swung {pmax_prev} → {pmax_cur}")
                )

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--prev", type=Path, required=True, help="last published manifest.json"
    )
    ap.add_argument("--curr", type=Path, required=True, help="this run's manifest.json")
    ap.add_argument("--health", type=Path, default=HERE / "health.yaml")
    ap.add_argument(
        "--issue-body", type=Path, help="write the markdown issue body here"
    )
    args = ap.parse_args()

    prev = json.loads(args.prev.read_text())
    curr = json.loads(args.curr.read_text())
    health = yaml.safe_load(args.health.read_text())
    report = check_drift(prev, curr, health)

    print(report.issue_body(), file=sys.stderr)
    if args.issue_body:
        args.issue_body.write_text(report.issue_body())
    # Exit non-zero on a blocking finding so the workflow refuses to publish.
    return 1 if report.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
