"""Deterministic IaC security-scan phase — Checkov / Trivy (#1036).

The runner-side twin of the OPA policy phase (:mod:`terrapod.runner.phases.opa`),
but with one deliberate difference: **this phase is non-fatal throughout.** OPA
must fail the run closed when its bundle can't be fetched; the security scan does
not, because the *server* fails closed — ``security_scan_service.evaluate_post_plan``
synthesises an ``errored`` result and blocks the run if an ``enforced`` workspace
records no scan result. So a scanner crash, a config-fetch failure, or a
results-POST failure here just means "no result recorded", which the server
turns into a block for enforced workspaces and a no-op for advisory/off ones.

Flow:
  1. GET /api/terrapod/v1/runs/{run_id}/security-scan-config →
     {enabled, enforcement_level, engine, severity_threshold, skip_rules}.
     On failure or ``enabled=false`` → return None (skip).
  2. Run the configured engine(s) against the resolved plan JSON:
       - Checkov: ``--framework terraform_plan -f plan.json`` (plan-fed — scans
         *resolved* values, the recommended/verified engine).
       - Trivy: ``config`` on the plan JSON (best-effort; Trivy's plan-JSON
         support is engine-version-dependent, so an empty/errored Trivy run is
         tolerated).
  3. Normalise findings to a common shape and compute the outcome against the
     severity threshold.
  4. POST results to /api/terrapod/v1/runs/{run_id}/security-scan-results
     (idempotent — the API does ON CONFLICT DO NOTHING on run_id).

Severity model: scanners that don't rate a finding (Checkov OSS omits severity
for most checks — it's a Prisma-paid signal) default to **high**, so the default
``high`` threshold still counts them while an operator who raises the threshold
to ``critical`` can narrow to critical-rated findings. Suppress noise via
``skip_rules`` (Checkov ``CKV_*`` / Trivy ``AVD-*`` ids).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from terrapod.runner.phases import platform_tool
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.security_scan")

# Severity ranking. Unknown / unrated → treated as "high" (see module docstring).
_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_DEFAULT_SEVERITY = "high"

_SCAN_TIMEOUT = 300  # per-engine wall-clock cap (seconds)


def _severity_rank(sev: str | None) -> int:
    if not sev:
        return _SEVERITY_RANK[_DEFAULT_SEVERITY]
    return _SEVERITY_RANK.get(sev.strip().lower(), _SEVERITY_RANK[_DEFAULT_SEVERITY])


def _norm_severity(sev: str | None) -> str:
    if not sev:
        return _DEFAULT_SEVERITY
    s = sev.strip().lower()
    return s if s in _SEVERITY_RANK else _DEFAULT_SEVERITY


# ── config fetch ──────────────────────────────────────────────────────────


def fetch_scan_config(
    cfg: RunnerConfig,
    *,
    client: httpx.Client | None = None,
    sleep: Any = time.sleep,
) -> dict | None:
    """GET the per-workspace scan config. Bounded retries (3, 3s apart). Returns
    the config dict, or None on failure / no-API / disabled — the phase then
    skips (the server fails closed for enforced workspaces)."""
    if not cfg.has_api:
        return None

    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/security-scan-config"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))
    try:
        for attempt in (1, 2, 3):
            try:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except json.JSONDecodeError:
                        return None
                    return data if data.get("enabled") else None
                logger.info(
                    "scan config non-200 — will retry", attempt=attempt, status=resp.status_code
                )
            except httpx.RequestError as exc:
                logger.info(
                    "scan config request failed — will retry", attempt=attempt, err=str(exc)
                )
            if attempt < 3:
                sleep(3)
        logger.warning(
            "scan config fetch failed after retries — skipping scan (server fails closed)"
        )
        return None
    finally:
        if own_client:
            client.close()


# ── engine runners ──────────────────────────────────────────────────────────


def _run_checkov(
    plan_json: Path, skip_rules: list[str], *, binary: str = "checkov"
) -> tuple[bool, list[dict], str | None]:
    """Run Checkov against the resolved plan JSON. Returns (ok, findings, error)."""
    cmd = [
        binary,
        "-f",
        str(plan_json),
        "--framework",
        "terraform_plan",
        "--output",
        "json",
        "--compact",
        "--soft-fail",  # never let the exit code fail the run; the gate is server-side
        "--quiet",
    ]
    for rule in skip_rules:
        cmd += ["--skip-check", rule]
    try:
        result = subprocess.run(  # noqa: S603 — checkov is operator-controlled
            cmd, check=False, capture_output=True, text=True, timeout=_SCAN_TIMEOUT
        )
    except FileNotFoundError as exc:
        return False, [], f"checkov binary not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, [], "checkov timed out"
    except OSError as exc:
        return False, [], f"checkov failed: {exc}"

    if not result.stdout.strip():
        # No stdout at all — treat as errored unless checkov cleanly found nothing.
        if result.returncode == 0:
            return True, [], None
        return False, [], (result.stderr.strip()[:1000] or "checkov produced no output")
    try:
        return True, _normalize_checkov(result.stdout), None
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as exc:
        return False, [], f"failed to parse checkov output: {exc}"


def _normalize_checkov(output: str) -> list[dict]:
    """Normalise Checkov JSON (object for a single framework, or a list) into the
    common finding shape."""
    parsed = json.loads(output)
    frameworks = parsed if isinstance(parsed, list) else [parsed]
    findings: list[dict] = []
    for fw in frameworks:
        if not isinstance(fw, dict):
            continue
        failed = ((fw.get("results") or {}).get("failed_checks")) or []
        for chk in failed:
            line_range = chk.get("file_line_range") or []
            line = line_range[0] if line_range else None
            findings.append(
                {
                    "engine": "checkov",
                    "rule_id": chk.get("check_id") or "",
                    "severity": _norm_severity(chk.get("severity")),
                    "title": chk.get("check_name") or "",
                    "resource": chk.get("resource") or "",
                    "file": chk.get("file_path") or "",
                    "line": line,
                    "guideline": chk.get("guideline") or "",
                }
            )
    return findings


def _run_trivy(
    plan_json: Path, skip_rules: list[str], *, binary: str = "trivy"
) -> tuple[bool, list[dict], str | None]:
    """Run Trivy config-scan against the plan JSON. Best-effort — Trivy's
    plan-JSON support is version-dependent, so failures are tolerated."""
    cmd = [binary, "config", "--format", "json", "--quiet", str(plan_json)]
    try:
        result = subprocess.run(  # noqa: S603 — trivy is operator-controlled
            cmd, check=False, capture_output=True, text=True, timeout=_SCAN_TIMEOUT
        )
    except FileNotFoundError as exc:
        return False, [], f"trivy binary not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, [], "trivy timed out"
    except OSError as exc:
        return False, [], f"trivy failed: {exc}"

    if not result.stdout.strip():
        if result.returncode == 0:
            return True, [], None
        return False, [], (result.stderr.strip()[:1000] or "trivy produced no output")
    try:
        return True, _normalize_trivy(result.stdout, skip_rules), None
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as exc:
        return False, [], f"failed to parse trivy output: {exc}"


def _normalize_trivy(output: str, skip_rules: list[str]) -> list[dict]:
    """Normalise Trivy config-scan JSON into the common finding shape. Trivy
    doesn't take a --skip-check flag for config ids the way Checkov does, so we
    filter its findings by id here."""
    parsed = json.loads(output)
    skip = set(skip_rules)
    findings: list[dict] = []
    for res in parsed.get("Results") or []:
        target = res.get("Target") or ""
        for mis in res.get("Misconfigurations") or []:
            rule_id = mis.get("ID") or ""
            if rule_id in skip:
                continue
            cause = mis.get("CauseMetadata") or {}
            findings.append(
                {
                    "engine": "trivy",
                    "rule_id": rule_id,
                    "severity": _norm_severity(mis.get("Severity")),
                    "title": mis.get("Title") or "",
                    "resource": cause.get("Resource") or "",
                    "file": target,
                    "line": cause.get("StartLine"),
                    "guideline": mis.get("PrimaryURL") or "",
                }
            )
    return findings


# ── outcome ──────────────────────────────────────────────────────────────────


def compute_outcome(findings: list[dict], threshold: str, *, errored: bool) -> tuple[str, dict]:
    """Compute (outcome, summary) from findings and the severity threshold.

    outcome: ``errored`` if an engine crashed; else ``failed`` if any finding is
    at or above the threshold; else ``passed``. ``summary`` carries the counts
    the UI shows.
    """
    trank = _severity_rank(threshold)
    by_sev: dict[str, int] = {}
    blocking = 0
    for f in findings:
        sev = _norm_severity(f.get("severity"))
        by_sev[sev] = by_sev.get(sev, 0) + 1
        if _severity_rank(sev) >= trank:
            blocking += 1
    if errored:
        outcome = "errored"
    elif blocking:
        outcome = "failed"
    else:
        outcome = "passed"
    summary = {
        "total": len(findings),
        "blocking": blocking,
        "by_severity": by_sev,
        "threshold": threshold,
    }
    return outcome, summary


# ── results POST ──────────────────────────────────────────────────────────────


def post_scan_result(
    cfg: RunnerConfig,
    *,
    engine: str,
    outcome: str,
    findings: list[dict],
    summary: dict,
    error: str | None,
    client: httpx.Client | None = None,
    sleep: Any = time.sleep,
) -> bool:
    """POST the scan result. Bounded retries (3, 3s apart); idempotent server-side
    (ON CONFLICT DO NOTHING on run_id). Returns True on 201, False otherwise —
    non-fatal (the server fails closed on a missing enforced result)."""
    if not cfg.has_api:
        return False

    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/security-scan-results"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
    body = {
        "engine": engine,
        "outcome": outcome,
        "findings": findings,
        "summary": summary,
        "error": error,
    }

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))
    try:
        for attempt in (1, 2, 3):
            try:
                resp = client.post(url, json=body, headers=headers)
                if resp.status_code == 201:
                    return True
                # A definitive 4xx is final — don't retry a bad request.
                if 400 <= resp.status_code < 500:
                    logger.warning("scan results POST rejected", status=resp.status_code)
                    return False
                logger.info(
                    "scan results POST non-201 — will retry",
                    attempt=attempt,
                    status=resp.status_code,
                )
            except httpx.RequestError as exc:
                logger.info("scan results POST failed — will retry", attempt=attempt, err=str(exc))
            if attempt < 3:
                sleep(3)
        logger.warning("scan results POST failed after retries — server fails closed for enforced")
        return False
    finally:
        if own_client:
            client.close()


# ── orchestrator ──────────────────────────────────────────────────────────────


def run_and_post(
    cfg: RunnerConfig,
    scan_config: dict,
    *,
    plan_json: Path,
    client: httpx.Client | None = None,
) -> str | None:
    """Run the configured engine(s) against the plan JSON, compute the outcome,
    and POST results. Returns the outcome, or None if nothing ran. Fully
    non-fatal — the server gate enforces."""
    engine = (scan_config.get("engine") or "checkov").lower()
    threshold = (scan_config.get("severity_threshold") or "high").lower()
    skip_rules = list(scan_config.get("skip_rules") or [])

    if plan_json is None or not plan_json.exists() or plan_json.stat().st_size == 0:
        # No plan JSON to scan — record an errored result so an enforced
        # workspace surfaces it (rather than the generic missing-result net).
        outcome, summary = "errored", {"total": 0, "blocking": 0}
        post_scan_result(
            cfg,
            engine=engine,
            outcome=outcome,
            findings=[],
            summary=summary,
            error="plan JSON was not available for security scanning",
            client=client,
        )
        return outcome

    findings: list[dict] = []
    errors: list[str] = []
    any_errored = False

    # The engines are fetched from the binary cache rather than baked into the
    # image (#1208), and only the ones this run actually selected — a checkov
    # workspace never pulls trivy. A fetch failure is recorded as an errored
    # engine rather than raised: this whole phase is best-effort, and the
    # server's post-plan gate already fails closed on an errored result for an
    # enforced workspace. Crashing here would take out the *run*, which is a
    # heavier response than the scan being unavailable warrants.
    if engine in ("checkov", "both"):
        binary, why = platform_tool.tool_path_or_name(cfg, "checkov", client=client)
        if not binary:
            any_errored = True
            errors.append(f"checkov: {why}")
        else:
            ok, ck_findings, err = _run_checkov(plan_json, skip_rules, binary=binary)
            if ok:
                findings += ck_findings
            else:
                any_errored = True
                if err:
                    errors.append(f"checkov: {err}")

    if engine in ("trivy", "both"):
        binary, why = platform_tool.tool_path_or_name(cfg, "trivy", client=client)
        if not binary:
            any_errored = True
            errors.append(f"trivy: {why}")
        else:
            ok, tv_findings, err = _run_trivy(plan_json, skip_rules, binary=binary)
            if ok:
                findings += tv_findings
            else:
                any_errored = True
                if err:
                    errors.append(f"trivy: {err}")

    outcome, summary = compute_outcome(findings, threshold, errored=any_errored)
    logger.info(
        "security scan complete",
        engine=engine,
        outcome=outcome,
        total=summary.get("total"),
        blocking=summary.get("blocking"),
    )
    post_scan_result(
        cfg,
        engine=engine,
        outcome=outcome,
        findings=findings,
        summary=summary,
        error=("; ".join(errors)[:2000] or None),
        client=client,
    )
    return outcome
