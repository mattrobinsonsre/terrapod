"""OPA policy evaluation phase (#343).

Port of `tp_evaluate_policies` in docker/runner-entrypoint.sh.

Flow:
  1. GET /api/terrapod/v1/runs/{run_id}/policy-bundle — bounded retries.
     Bundle-fetch failure after retries is FATAL (refuses to proceed
     is safer than silently skipping the gate).
  2. If the bundle has zero applicable policy sets, return — nothing
     to evaluate.
  3. Save the `context` to a temp file as `terrapod_context` data.
  4. For each policy set, for each policy:
       - Write rego to a temp file.
       - Run `opa eval --format json --stdin-input --data <rego>
         --data <context> 'data.terrapod' < /tmp/plan.json`.
       - Extract `deny` / `warn` lists from `result[0].expressions[0]
         .value` defensively (the rule values can be missing, an empty
         set, a partial-set array, OR a scalar that we coerce to a
         single-element array — without this a misauthored
         `deny := "msg"` would silently pass).
       - Aggregate per-set outcome: `errored` if any policy errored,
         else `failed` if any policy denied, else `passed`.
  5. POST the aggregated results to
     /api/terrapod/v1/runs/{run_id}/policy-results with bounded retries
     (the API enforces ON CONFLICT DO NOTHING on (run_id,
     policy_set_id) so retries are idempotent).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.opa")


class PolicyEvaluationError(RuntimeError):
    """Bundle fetch or results POST failed after retries. Caller exits
    non-zero — fail-closed for mandatory sets."""


def _coerce_to_string_list(value: object) -> list[str]:
    """Coerce the value of a rego `deny` / `warn` rule into a sorted
    list of strings.

    OPA can serialise the result as:
      - missing (rule not produced): treat as []
      - an empty set / array: []
      - a partial-set array (the normal `deny contains msg if ...` case)
      - a scalar (misauthored `deny := "msg"` or `deny := true`): wrap
        as a single-element list

    Without the coercion a scalar would explode in the bash version's
    `.[] | tostring` jq filter and the policy would silently "pass" —
    defeating the whole gate.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return sorted(str(v) for v in value)
    return [str(value)]


def _extract_deny_warn(opa_output: str) -> tuple[list[str], list[str]]:
    """Pull `deny` and `warn` out of OPA's `eval --format json` output.

    Output shape:
      {"result": [
         {"expressions": [
            {"value": {"deny": [...], "warn": [...]}, ...}
         ]}
      ]}

    Every layer can be missing / empty. Defensive accessor mirrors the
    bash jq `.result // [] | .[0] // {} | ...` chain.
    """
    try:
        parsed = json.loads(opa_output)
    except (json.JSONDecodeError, TypeError):
        return [], []
    results = parsed.get("result") or []
    if not results:
        return [], []
    expressions = results[0].get("expressions") or []
    if not expressions:
        return [], []
    value = expressions[0].get("value") or {}
    deny = _coerce_to_string_list(value.get("deny"))
    warn = _coerce_to_string_list(value.get("warn"))
    return deny, warn


def _run_opa_eval(
    *,
    plan_json: Path,
    rego_path: Path,
    context_path: Path,
    opa_binary: str = "opa",
) -> tuple[int, str, str]:
    """Invoke `opa eval` against the plan JSON. Returns
    (exit_code, stdout, stderr). Stdin is the plan JSON file."""
    cmd = [
        opa_binary,
        "eval",
        "--format",
        "json",
        "--stdin-input",
        "--data",
        str(rego_path),
        "--data",
        str(context_path),
        "data.terrapod",
    ]
    try:
        with plan_json.open("rb") as stdin_fh:
            result = subprocess.run(  # noqa: S603 — opa is operator-controlled
                cmd,
                check=False,
                stdin=stdin_fh,
                capture_output=True,
                text=True,
                timeout=60,
            )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError as exc:
        return 1, "", f"opa binary not found: {exc}"
    except subprocess.TimeoutExpired:
        return 1, "", "opa eval timed out"
    except OSError as exc:
        return 1, "", f"opa eval failed: {exc}"


def fetch_policy_bundle(
    cfg: RunnerConfig,
    *,
    client: httpx.Client | None = None,
    sleep: callable = time.sleep,
) -> dict:
    """GET the policy bundle. Bounded retries (3 attempts, 3s apart) on
    non-200; on final failure raise PolicyEvaluationError (FATAL —
    caller exits non-zero)."""
    if not cfg.has_api:
        return {"policy_sets": []}

    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/policy-bundle"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))

    try:
        last_status: int | None = None
        for attempt in (1, 2, 3):
            try:
                resp = client.get(url, headers=headers)
                last_status = resp.status_code
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except json.JSONDecodeError as exc:
                        raise PolicyEvaluationError(
                            f"policy bundle returned non-JSON body: {exc}"
                        ) from exc
                logger.info(
                    "policy bundle non-200 — will retry",
                    attempt=attempt,
                    status=resp.status_code,
                )
            except httpx.RequestError as exc:
                logger.info(
                    "policy bundle request failed — will retry",
                    attempt=attempt,
                    err=str(exc),
                )
            if attempt < 3:
                sleep(3)
        raise PolicyEvaluationError(
            f"policy bundle fetch failed after 3 attempts (last status={last_status})"
        )
    finally:
        if own_client:
            client.close()


def evaluate_set(
    *,
    policy_set: dict,
    plan_json: Path | None,
    context_path: Path,
    rego_dir: Path,
    opa_binary: str = "opa",
) -> dict:
    """Evaluate every policy in a set, returning the JSON shape the API
    accepts:

      {
        "policy_set_id": "...",
        "policy_set_name": "...",
        "enforcement_level": "...",
        "outcome": "passed" | "failed" | "errored",
        "result": {"policies": [...], "evaluated_at": "..."}
      }
    """
    set_id = policy_set.get("id", "")
    set_name = policy_set.get("name", "")
    enforcement = policy_set.get("enforcement_level", "")
    policies = policy_set.get("policies") or []

    policy_results: list[dict] = []
    outcome = "passed"

    for ix, pol in enumerate(policies):
        name = pol.get("name", "")
        rego = pol.get("rego", "")
        rego_path = rego_dir / f"policy_{ix}.rego"
        rego_path.write_text(rego)

        if plan_json is None or not plan_json.exists() or plan_json.stat().st_size == 0:
            policy_results.append(
                {
                    "policy": name,
                    "passed": False,
                    "violations": [],
                    "warnings": [],
                    "error": "plan JSON was not available for policy evaluation",
                }
            )
            outcome = "errored"
            continue

        opa_exit, opa_stdout, opa_stderr = _run_opa_eval(
            plan_json=plan_json,
            rego_path=rego_path,
            context_path=context_path,
            opa_binary=opa_binary,
        )

        if opa_exit != 0:
            err = opa_stderr.strip()[:1000] or f"OPA evaluation failed with exit code {opa_exit}"
            policy_results.append(
                {
                    "policy": name,
                    "passed": False,
                    "violations": [],
                    "warnings": [],
                    "error": err,
                }
            )
            outcome = "errored"
            continue

        deny, warn = _extract_deny_warn(opa_stdout)
        passed = not deny
        policy_results.append(
            {
                "policy": name,
                "passed": passed,
                "violations": deny,
                "warnings": warn,
                "error": None,
            }
        )
        if deny and outcome != "errored":
            outcome = "failed"

    return {
        "policy_set_id": set_id,
        "policy_set_name": set_name,
        "enforcement_level": enforcement,
        "outcome": outcome,
        "result": {
            "policies": policy_results,
            "evaluated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def post_results(
    cfg: RunnerConfig,
    results: list[dict],
    *,
    client: httpx.Client | None = None,
    sleep: callable = time.sleep,
) -> None:
    """POST aggregated results. Bounded retries (3, 3s apart); the API
    enforces ON CONFLICT DO NOTHING so retries are idempotent. On final
    failure raise PolicyEvaluationError."""
    if not cfg.has_api:
        return

    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/policy-results"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))

    try:
        last_status: int | None = None
        body = {"results": results}
        for attempt in (1, 2, 3):
            try:
                resp = client.post(url, json=body, headers=headers)
                last_status = resp.status_code
                if resp.status_code == 201:
                    return
                logger.info(
                    "policy results POST non-201 — will retry",
                    attempt=attempt,
                    status=resp.status_code,
                )
            except httpx.RequestError as exc:
                logger.info(
                    "policy results POST failed — will retry",
                    attempt=attempt,
                    err=str(exc),
                )
            if attempt < 3:
                sleep(3)
        raise PolicyEvaluationError(
            f"policy results POST failed after 3 attempts (last status={last_status})"
        )
    finally:
        if own_client:
            client.close()


def evaluate_policies(
    cfg: RunnerConfig,
    *,
    plan_json: Path,
    work_dir: Path,
    opa_binary: str = "opa",
    client: httpx.Client | None = None,
) -> int:
    """Drive the whole OPA phase. Returns the number of policy sets
    evaluated (0 = nothing applicable, caller may skip). Raises
    PolicyEvaluationError on FATAL bundle-fetch / POST failures."""
    bundle = fetch_policy_bundle(cfg, client=client)
    policy_sets = bundle.get("policy_sets") or []
    if not policy_sets:
        logger.info("no applicable policy sets — skipping evaluation")
        return 0

    work_dir.mkdir(parents=True, exist_ok=True)
    context_path = work_dir / "policy-context.json"
    context_path.write_text(json.dumps({"terrapod_context": bundle.get("context", {})}))

    rego_dir = work_dir / "rego"
    rego_dir.mkdir(parents=True, exist_ok=True)

    results = [
        evaluate_set(
            policy_set=ps,
            plan_json=plan_json,
            context_path=context_path,
            rego_dir=rego_dir,
            opa_binary=opa_binary,
        )
        for ps in policy_sets
    ]
    logger.info("evaluated policy sets", count=len(results))
    post_results(cfg, results, client=client)
    return len(results)
