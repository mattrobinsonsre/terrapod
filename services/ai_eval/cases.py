"""Case + Truth schema and corpus loader for the AI-eval harness (#602).

A *case* is one labelled evaluation example: an input (a terraform plan JSON
for plan/drift, or an apply log for apply-failure) plus the ground-truth the
analysis must satisfy. Cases are loaded from YAML files under ``corpus/``;
generated cases are emitted in the same shape by ``generator.py``.

Surfaces map to the production summariser ``kind`` + flags:
  - ``plan``          → kind=plan_summary, drift_detection=False
  - ``drift``         → kind=plan_summary, drift_detection=True
  - ``apply_failure`` → kind=failure_analysis
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Surface = Literal["plan", "drift", "apply_failure"]

# Strict ordering of risk levels — used for band comparisons everywhere.
RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def risk_rank(level: str) -> int:
    """Numeric rank of a risk level; unknown → -1 (sorts below 'low')."""
    return RISK_ORDER.get((level or "").strip().lower(), -1)


# Fraction of the corpus reserved as holdout when not pinned explicitly.
_HOLDOUT_MODULUS = 4  # ~25%


def is_holdout(case: Case) -> bool:
    """Whether a case is in the held-out validation set.

    Explicit ``case.holdout`` wins; otherwise a deterministic id hash assigns
    ~1/_HOLDOUT_MODULUS of cases to holdout. Deterministic so the split is
    stable across runs and across machines — the prompt-tuner can rely on the
    same cases always being held out.
    """
    if case.holdout is not None:
        return case.holdout
    digest = hashlib.md5(case.id.encode("utf-8")).hexdigest()
    return int(digest, 16) % _HOLDOUT_MODULUS == 0


@dataclass(frozen=True)
class MustFlag:
    """A resource the analysis MUST flag as a risk, at >= a minimum severity."""

    address: str
    min_severity: str = "medium"


@dataclass(frozen=True)
class RiskBand:
    """Acceptable band for the overall risk_level.

    Any of exact / min / max may be set. ``exact`` pins a single level;
    ``min``/``max`` bound an inclusive range. Empty band = unconstrained
    (only the other axes are scored).
    """

    exact: str | None = None
    min: str | None = None
    max: str | None = None

    def contains(self, level: str) -> bool:
        r = risk_rank(level)
        if self.exact is not None:
            return r == risk_rank(self.exact)
        if self.min is not None and r < risk_rank(self.min):
            return False
        if self.max is not None and r > risk_rank(self.max):
            return False
        return True

    def describe(self) -> str:
        if self.exact is not None:
            return f"=={self.exact}"
        parts = []
        if self.min is not None:
            parts.append(f">={self.min}")
        if self.max is not None:
            parts.append(f"<={self.max}")
        return " & ".join(parts) if parts else "any"


@dataclass(frozen=True)
class Truth:
    """Ground-truth labels a case's analysis is scored against."""

    risk: RiskBand = field(default_factory=RiskBand)
    # Resources that MUST appear as risk_factors at >= min_severity.
    must_flag: tuple[MustFlag, ...] = ()
    # Resources that MUST NOT appear as risk_factors (false-positive guard).
    must_not_flag: tuple[str, ...] = ()
    # Churn addresses (tag-only / known_after_apply / no-op drift). These
    # must NOT be risk_factors AND should not drive the risk band up. Scored
    # as the "real-change-vs-churn-noise" axis. A superset relationship with
    # must_not_flag for scoring, kept separate for reporting clarity.
    churn_addresses: tuple[str, ...] = ()
    # Case-insensitive substrings the description MUST contain.
    key_facts: tuple[str, ...] = ()
    # Case-insensitive substrings the description MUST NOT contain.
    forbidden_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    """One labelled evaluation example."""

    id: str
    surface: Surface
    title: str
    truth: Truth
    source: Literal["curated", "generated"] = "curated"
    tags: tuple[str, ...] = ()
    # Inputs — exactly one primary per surface.
    plan_json: dict[str, Any] | None = None  # plan / drift
    apply_log: str = ""  # apply_failure
    # Context the production prompt also receives (CODE_DIFF + CODE_CONTEXT
    # sections). These inform the narrative/"why" but, per the prompt's
    # grounding rule, must NEVER raise risk above what PLAN_JSON justifies —
    # which is exactly what the corpus needs to test.
    code_diff: str = ""  # unified diff of *.tf / *.tfvars vs prior applied config
    code_context: str = ""  # current .tf source (truncated)
    # NOT fed to the shipping prompt today; carried here so the corpus can test
    # a future COMMIT_CONTEXT enhancement (incl. misleading-message resistance).
    commit_message: str = ""
    state_diverged: bool = False
    # Train/holdout split for honest generalization measurement. None = decide
    # deterministically by id hash (see is_holdout); an explicit bool pins it.
    # The prompt is tuned ONLY against train failures; holdout is never read
    # while editing the prompt, so a gain that doesn't show on holdout is
    # overfitting (teaching-to-the-test) and must be reverted.
    holdout: bool | None = None

    @property
    def kind(self) -> str:
        """Production summariser kind for this surface."""
        return "failure_analysis" if self.surface == "apply_failure" else "plan_summary"

    @property
    def drift_detection(self) -> bool:
        return self.surface == "drift"


# --- Loading -----------------------------------------------------------------


def _truth_from_dict(d: dict[str, Any]) -> Truth:
    risk_d = d.get("risk") or {}
    band = RiskBand(
        exact=risk_d.get("exact"),
        min=risk_d.get("min"),
        max=risk_d.get("max"),
    )
    must_flag = tuple(
        MustFlag(address=mf["address"], min_severity=mf.get("min_severity", "medium"))
        for mf in (d.get("must_flag") or [])
    )
    return Truth(
        risk=band,
        must_flag=must_flag,
        must_not_flag=tuple(d.get("must_not_flag") or []),
        churn_addresses=tuple(d.get("churn_addresses") or []),
        key_facts=tuple(d.get("key_facts") or []),
        forbidden_claims=tuple(d.get("forbidden_claims") or []),
    )


def _concat_tf_sources(base_dir: Path) -> str:
    """Concatenate the scenario's real *.tf files into a CODE_CONTEXT blob,
    mirroring the production format (``# === <file> ===`` headers) so the model
    sees the same shape it would in a real run."""
    parts: list[str] = []
    for tf in sorted(base_dir.glob("*.tf")):
        parts.append(f"# === {tf.name} ===\n{tf.read_text(encoding='utf-8')}")
    return "\n".join(parts)


def case_from_dict(d: dict[str, Any], *, base_dir: Path | None = None) -> Case:
    """Build a Case from a parsed YAML/JSON mapping.

    ``inputs.plan_json`` may be inline or via ``inputs.plan_json_file`` (a path
    relative to ``base_dir``). Same for ``apply_log`` / ``apply_log_file`` and
    ``code_diff`` / ``code_diff_file``. CODE_CONTEXT is either inline
    (``code_context``), from a file (``code_context_file``), or — the common
    case for scenario dirs — auto-assembled from the dir's ``*.tf`` when
    ``code_context_from_tf: true``. ``commit_message`` is carried for the future
    COMMIT_CONTEXT enhancement (not fed to the shipping prompt yet).
    """
    inputs = d.get("inputs") or {}
    bd = base_dir or Path(".")

    plan_json = inputs.get("plan_json")
    if plan_json is None and inputs.get("plan_json_file"):
        plan_json = json.loads((bd / inputs["plan_json_file"]).read_text(encoding="utf-8"))

    apply_log = inputs.get("apply_log", "")
    if not apply_log and inputs.get("apply_log_file"):
        apply_log = (bd / inputs["apply_log_file"]).read_text(encoding="utf-8")

    code_diff = inputs.get("code_diff", "")
    if not code_diff and inputs.get("code_diff_file"):
        code_diff = (bd / inputs["code_diff_file"]).read_text(encoding="utf-8")

    code_context = inputs.get("code_context", "")
    if not code_context and inputs.get("code_context_file"):
        code_context = (bd / inputs["code_context_file"]).read_text(encoding="utf-8")
    if not code_context and inputs.get("code_context_from_tf"):
        code_context = _concat_tf_sources(bd)

    return Case(
        id=d["id"],
        surface=d["surface"],
        title=d.get("title", d["id"]),
        truth=_truth_from_dict(d.get("truth") or {}),
        source=d.get("source", "curated"),
        tags=tuple(d.get("tags") or []),
        plan_json=plan_json,
        apply_log=apply_log,
        code_diff=code_diff,
        code_context=code_context,
        commit_message=inputs.get("commit_message", ""),
        state_diverged=bool(inputs.get("state_diverged", False)),
        holdout=d.get("holdout"),
    )


def corpus_dir() -> Path:
    """Absolute path to the bundled corpus directory."""
    return Path(__file__).resolve().parent / "corpus"


def load_corpus(
    root: Path | None = None,
    *,
    surfaces: set[str] | None = None,
    tags: set[str] | None = None,
) -> list[Case]:
    """Load every ``*.case.yaml`` under ``root`` (default: bundled corpus).

    Optional ``surfaces`` / ``tags`` filters narrow the returned set. IDs are
    asserted unique across the whole corpus so a duplicate is a loud failure,
    not a silent overwrite.
    """
    root = root or corpus_dir()
    cases: list[Case] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*.case.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not doc:
            continue
        case = case_from_dict(doc, base_dir=path.parent)
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r} (at {path})")
        seen.add(case.id)
        if surfaces and case.surface not in surfaces:
            continue
        if tags and not (set(case.tags) & tags):
            continue
        cases.append(case)
    return cases
