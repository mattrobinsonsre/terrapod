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
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from terrapod.runner.phases import platform_tool
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.opa")


class PolicyGateBlocked(Exception):
    """A mandatory policy set failed or errored, as judged by the API.

    Separate from `PolicyEvaluationError`, which means "we could not evaluate".
    The two want opposite handling on a phase the API does not hold: a failure
    to evaluate is an infrastructure fault to report, while this is the gate
    doing its job and must stop the work.
    """


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
    (exit_code, stdout, stderr). Stdin is the plan JSON file.

    ``rego_path`` may be a single file or a DIRECTORY. OPA loads a directory
    recursively, picking up `.rego`, `.json` and `.yaml` by extension, which is
    exactly what shared evaluation needs (#1842) — so the two modes differ only
    in what is written to disk beforehand, not in how OPA is invoked.
    """
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


def _safe_dir_component(set_id: str) -> str:
    """A policy set's id reduced to one safe path component.

    The id is server-generated (`polset-<uuid>`), but it reaches the runner
    over the wire, so it is sanitised for the same reason support-file names
    are rather than trusted. Anything outside the allow-list becomes `_`, and
    an empty result falls back to a constant -- a set with an unusable id still
    gets a directory, it just does not get an isolated one, which is no worse
    than the single shared directory this replaced.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", set_id or "")
    return cleaned or "unnamed"


def _safe_support_name(name: str) -> str | None:
    """A support file's name, reduced to something safe to write to disk.

    The name reaches us from a git repository by way of the policy bundle, so
    it is attacker-influenced in the same sense the rego is. Anything with a
    path separator or a parent reference is dropped rather than sanitised:
    a support file is a direct child of the policy path by construction, so a
    name that is not a bare filename did not come from the sync and there is
    nothing to salvage.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not name.endswith((".rego", ".yaml", ".yml", ".json")):
        return None
    return name


def _evaluate_set_together(
    *,
    set_id: str,
    set_name: str,
    enforcement: str,
    policies: list[dict],
    support_files: dict,
    plan_json: Path | None,
    context_path: Path,
    rego_dir: Path,
    opa_binary: str = "opa",
) -> dict:
    """Evaluate a whole set in ONE `opa eval`, so its files can share (#1842).

    Every policy and every support file goes into one directory, which OPA
    loads recursively — helpers become callable from any policy and data lands
    under `data.<key>` the way `conftest -d` does it.

    **Results are per SET here, not per policy, and that is not a shortcut.**
    Every file shares `package terrapod`, so once they are evaluated together
    the `deny` set is the union of all of them and OPA does not say which file
    produced which message. Reporting a per-policy breakdown would mean
    inventing an attribution we do not have. One honest entry, named for the
    set, is the truthful shape — and it is why `shared_evaluation` is opt-in
    rather than the default: a set that wants per-policy results keeps them by
    leaving it off.
    """
    if plan_json is None or not plan_json.exists() or plan_json.stat().st_size == 0:
        return {
            "policy_set_id": set_id,
            "policy_set_name": set_name,
            "enforcement_level": enforcement,
            "outcome": "errored",
            "result": {
                "policies": [
                    {
                        "policy": set_name,
                        "passed": False,
                        "violations": [],
                        "warnings": [],
                        "error": "plan JSON was not available for policy evaluation",
                    }
                ],
                "shared_evaluation": True,
                "evaluated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }

    # A directory PER SET, emptied first. Every set in a run shares one
    # `rego_dir`, and `opa eval` loads a directory recursively -- so a single
    # shared path let one set's files reach another's evaluation. Measured
    # against OPA (re-verified on 1.21.0): with set A (advisory, two policies, a data.yaml)
    # evaluated before set B (mandatory, one policy), B's own `policy_0.rego`
    # overwrote A's but A's `policy_1.rego` and `data.yaml` remained, so B
    # reported `failed` citing a rule it does not contain -- an advisory rule
    # silently became blocking under another set's name. Same-named files hid
    # it whenever the sets had equal file counts, which is what the tests had.
    shared_dir = rego_dir / f"shared-{_safe_dir_component(set_id)}"
    shutil.rmtree(shared_dir, ignore_errors=True)
    shared_dir.mkdir(parents=True, exist_ok=True)

    for ix, pol in enumerate(policies):
        (shared_dir / f"policy_{ix}.rego").write_text(pol.get("rego", ""))

    skipped: list[str] = []
    for raw_name, content in sorted(support_files.items()):
        safe = _safe_support_name(raw_name)
        if safe is None:
            skipped.append(raw_name)
            continue
        (shared_dir / safe).write_text(content if isinstance(content, str) else str(content))
    if skipped:
        logger.warning("support files skipped: unsafe names", files=skipped, set=set_name)

    opa_exit, opa_stdout, opa_stderr = _run_opa_eval(
        plan_json=plan_json,
        rego_path=shared_dir,
        context_path=context_path,
        opa_binary=opa_binary,
    )

    evaluated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    names = [p.get("name", "") for p in policies]

    if opa_exit != 0:
        err = opa_stderr.strip()[:1000] or f"OPA evaluation failed with exit code {opa_exit}"
        return {
            "policy_set_id": set_id,
            "policy_set_name": set_name,
            "enforcement_level": enforcement,
            "outcome": "errored",
            "result": {
                "policies": [
                    {
                        "policy": set_name,
                        "passed": False,
                        "violations": [],
                        "warnings": [],
                        "error": err,
                    }
                ],
                "shared_evaluation": True,
                "evaluated_from": names,
                "evaluated_at": evaluated_at,
            },
        }

    deny, warn = _extract_deny_warn(opa_stdout)
    return {
        "policy_set_id": set_id,
        "policy_set_name": set_name,
        "enforcement_level": enforcement,
        "outcome": "failed" if deny else "passed",
        "result": {
            "policies": [
                {
                    "policy": set_name,
                    "passed": not deny,
                    "violations": deny,
                    "warnings": warn,
                    "error": None,
                }
            ],
            # Which files went in, so a reader can see what produced the
            # verdict even though OPA cannot attribute each message.
            "shared_evaluation": True,
            "evaluated_from": names,
            "evaluated_at": evaluated_at,
        },
    }


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

    if policy_set.get("shared_evaluation"):
        return _evaluate_set_together(
            set_id=set_id,
            set_name=set_name,
            enforcement=enforcement,
            policies=policies,
            support_files=policy_set.get("support_files") or {},
            plan_json=plan_json,
            context_path=context_path,
            rego_dir=rego_dir,
            opa_binary=opa_binary,
        )

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
) -> bool:
    """POST aggregated results; return whether the API says they BLOCK.

    Bounded retries (3, 3s apart); the API enforces ON CONFLICT DO NOTHING so
    retries are idempotent. On final failure raise PolicyEvaluationError.

    **The verdict comes from the API, never computed here (#2435).** "Blocking"
    means a mandatory set that failed or errored and was not overridden, and
    that rule belongs to `policy_set_service.run_is_policy_blocked`. A second
    copy in the runner is how the two come to disagree about what mandatory
    means -- so the response carries it and this returns it.

    `False` when there is no API to ask (`has_api` false, i.e. a local-exec
    run), which is the same answer that path has always given for every other
    server-side decision.
    """
    if not cfg.has_api:
        return False

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
                    # An older API does not send `blocked`. Absent reads as
                    # False, which matches what that API does -- it holds the
                    # run itself between Jobs for a plan, and had no configure
                    # gate at all -- rather than failing a run against it.
                    try:
                        return bool((resp.json() or {}).get("blocked", False))
                    except ValueError:
                        return False
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
    plan_json: Path | None | Callable[[], Path | None],
    work_dir: Path,
    enforce: bool,
    opa_binary: str | None = None,
    client: httpx.Client | None = None,
) -> int:
    """Drive the whole OPA phase. Returns the number of policy sets
    evaluated (0 = nothing applicable, caller may skip). Raises
    PolicyEvaluationError on FATAL bundle-fetch / POST failures.

    **`enforce` says whether THIS phase is the thing that stops the work, and
    it is required rather than defaulted (#2435).** Whether a blocking verdict
    must fail the Job is a property of the phase, not of OPA:

    * The **plan** phase passes `False`. The API holds the run between the plan
      Job and the apply Job, and `evaluate_post_plan` is the decision; a plan
      Job that failed itself on a deny would lose the plan artifact and the
      override path with it.
    * The **configure** phase passes `True`. A configure is a single Job, so
      there is no between-Jobs moment and refusing here is the whole mechanism.

    Required because the wrong default is unsafe in one direction and merely
    annoying in the other: defaulting to `False` would silently give any new
    single-Job phase a gate that reports instead of gating, which is exactly
    the defect this parameter was added to fix.

    `opa_binary` defaults to fetching OPA from the binary cache (#1208) — but
    only *after* the bundle turns out to have applicable sets, so a run with no
    policy never pays for a download it will not use. Tests pass an explicit
    path.

    `plan_json` may be a **callable**, for the same reason. Terraform's plan
    JSON is produced for the UI artifact regardless, so OPA rides along on it
    free; Pulumi's policy input has no other consumer and is built solely for
    this, so building it before knowing whether any set applies is pure waste
    on the overwhelmingly common no-policy run (#1567 review). Passing a
    factory defers that until the bundle proves non-empty. A factory that
    returns None is not an error here — `evaluate_set` records an `errored`
    evaluation per set, which is what makes the failure visible and
    overridable rather than silently skipped."""
    bundle = fetch_policy_bundle(cfg, client=client)
    policy_sets = bundle.get("policy_sets") or []
    if not policy_sets:
        logger.info("no applicable policy sets — skipping evaluation")
        return 0

    # Only now: past this point at least one set applies.
    plan_json = plan_json() if callable(plan_json) else plan_json

    if opa_binary is None:
        # FATAL, deliberately. OPA is no longer baked into the runner image, so
        # there is nothing on PATH to fall back to — and even if there were,
        # letting a policy set go unevaluated because its engine did not arrive
        # would pass a gate that was supposed to block. Failing the run is the
        # only safe reading of "we could not evaluate your policy".
        try:
            opa_binary = str(platform_tool.ensure_tool(cfg, "opa", client=client))
        except platform_tool.PlatformToolError as exc:
            raise PolicyEvaluationError(
                f"{len(policy_sets)} policy set(s) apply to this run but OPA could "
                f"not be obtained, so they cannot be evaluated: {exc}"
            ) from exc

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
    blocked = post_results(cfg, results, client=client)
    if blocked and enforce:
        # Reported as a FAILURE, not as a count, because a caller with no
        # between-Jobs moment has to act on it. `evaluate_policies` used to
        # return only `len(results)` and raise solely on infrastructure
        # failure, so a configure's gate was inverted: it failed CLOSED when
        # the bundle could not be fetched and OPEN on an actual mandatory
        # deny -- the playbook ran, with a failed mandatory set beside it in
        # the UI. The plan phase is unaffected: it ignores the return and the
        # API holds the run.
        raise PolicyGateBlocked(
            f"{len(results)} policy set(s) evaluated and a mandatory set did not pass"
        )
    if blocked:
        # Recorded even where this phase does not act on it, so the plan path's
        # log says the verdict was known here rather than leaving a reader to
        # infer it from the API's later decision.
        logger.info("policy verdict is blocking; this phase does not enforce", enforce=False)
    return len(results)
