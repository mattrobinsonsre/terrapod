"""OPA policy evaluation engine (#343).

Invokes the bundled ``opa`` binary as a subprocess to evaluate a Rego v1
policy against a run's plan JSON. This module is a pure function — given
a policy's Rego source, a plan-JSON document, and run/workspace context
it returns a per-policy pass/fail with violation messages. It owns no DB
state; orchestration and persistence live in ``policy_set_service``.

Terrapod policy convention
--------------------------
A policy's Rego must declare ``package terrapod`` and express violations
via a ``deny`` set of message strings (Rego v1 syntax)::

    package terrapod

    deny contains msg if {
        some rc in input.resource_changes
        rc.change.actions[_] == "create"
        rc.type == "aws_s3_bucket"
        not rc.change.after.server_side_encryption_configuration
        msg := sprintf("S3 bucket %s has no encryption", [rc.address])
    }

An optional ``warn`` set carries non-blocking advisories.

* ``input`` is the raw ``terraform show -json`` plan document, so
  existing community Terraform Rego works unchanged.
* ``data.terrapod_context`` exposes Terrapod run/workspace metadata.

The runner executes nothing here — evaluation is entirely server-side.
The subprocess is async (``asyncio.create_subprocess_exec``) so it never
blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# The bundled OPA binary (installed on PATH by docker/Dockerfile.api).
OPA_BINARY = "opa"

# Hard ceiling on a single policy evaluation. OPA has its own internal
# limits, but a subprocess-level timeout guards against a pathological
# Rego policy wedging the evaluation.
EVAL_TIMEOUT_SECONDS = 30.0

# The Rego package every Terrapod policy must declare, and the rule names
# Terrapod reads out of it.
POLICY_PACKAGE = "terrapod"


@dataclass
class PolicyResult:
    """Outcome of evaluating one policy against one plan."""

    policy_name: str
    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Set when OPA itself could not evaluate the policy (bad Rego, timeout,
    # unparseable output). An errored policy is treated as not-passed.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy_name,
            "passed": self.passed,
            "violations": self.violations,
            "warnings": self.warnings,
            "error": self.error,
        }


def _write_eval_inputs(rego: str, context: dict[str, Any]) -> str:
    """Create a temp dir holding the policy + context data files.

    Synchronous filesystem work — call via ``asyncio.to_thread``. Returns
    the temp dir path; the caller is responsible for removing it.
    """
    tmpdir = tempfile.mkdtemp(prefix="tp-policy-")
    with open(f"{tmpdir}/policy.rego", "w", encoding="utf-8") as fh:
        fh.write(rego)
    with open(f"{tmpdir}/context.json", "w", encoding="utf-8") as fh:
        json.dump({"terrapod_context": context}, fh)
    return tmpdir


async def evaluate_policy(
    policy_name: str,
    rego: str,
    plan_json: bytes,
    context: dict[str, Any],
    *,
    opa_binary: str = OPA_BINARY,
    timeout: float = EVAL_TIMEOUT_SECONDS,
) -> PolicyResult:
    """Evaluate a single Rego policy against a plan-JSON document.

    ``plan_json`` is the raw ``terraform show -json`` document (bytes),
    fed to OPA as ``input``. ``context`` (workspace/run metadata) is
    exposed as ``data.terrapod_context``. Never raises — an OPA failure
    is captured in ``PolicyResult.error`` with ``passed=False``.
    """
    tmpdir = await asyncio.to_thread(_write_eval_inputs, rego, context)
    try:
        proc = await asyncio.create_subprocess_exec(
            opa_binary,
            "eval",
            "--format",
            "json",
            "--stdin-input",
            "--data",
            f"{tmpdir}/policy.rego",
            "--data",
            f"{tmpdir}/context.json",
            f"data.{POLICY_PACKAGE}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=plan_json), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("Policy evaluation timed out", policy=policy_name)
            return PolicyResult(
                policy_name=policy_name,
                passed=False,
                error=f"OPA evaluation timed out after {timeout:g}s",
            )
    except FileNotFoundError:
        # The opa binary is missing from the image — a deployment fault.
        logger.error("OPA binary not found", binary=opa_binary)
        return PolicyResult(
            policy_name=policy_name,
            passed=False,
            error="OPA binary not available on the API server",
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        msg = stderr.decode("utf-8", "replace").strip() or "opa eval failed"
        logger.warning("Policy evaluation errored", policy=policy_name, error=msg)
        return PolicyResult(policy_name=policy_name, passed=False, error=msg[:2000])

    return _parse_opa_output(policy_name, stdout)


def _parse_opa_output(policy_name: str, stdout: bytes) -> PolicyResult:
    """Extract ``deny`` / ``warn`` from an ``opa eval --format json`` doc.

    Shape: ``{"result": [{"expressions": [{"value": {...}}]}]}``. A query
    on a package with no matching rules yields no ``result`` entry — that
    is a clean pass (no violations), not an error.
    """
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return PolicyResult(
            policy_name=policy_name,
            passed=False,
            error=f"could not parse OPA output: {exc}",
        )

    value: dict[str, Any] = {}
    results = doc.get("result") or []
    if results:
        expressions = results[0].get("expressions") or []
        if expressions:
            raw = expressions[0].get("value")
            if isinstance(raw, dict):
                value = raw

    violations = _string_list(value.get("deny"))
    warnings = _string_list(value.get("warn"))
    return PolicyResult(
        policy_name=policy_name,
        passed=not violations,
        violations=violations,
        warnings=warnings,
    )


def _string_list(value: Any) -> list[str]:
    """Coerce an OPA ``deny``/``warn`` set value into a sorted str list.

    A ``deny contains msg`` set serialises to a JSON array. Anything that
    is not a list (a malformed policy) yields an empty list — convention
    violations are caught by policy validation at creation time, not here.
    """
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value)


async def check_rego(rego: str, *, opa_binary: str = OPA_BINARY) -> str | None:
    """Validate that a Rego document compiles. Returns an error string on
    failure, or ``None`` if it is well-formed. Used by the policy CRUD
    API to reject broken Rego at creation time rather than at run time.
    """
    tmpdir = await asyncio.to_thread(_write_eval_inputs, rego, {})
    try:
        proc = await asyncio.create_subprocess_exec(
            opa_binary,
            "check",
            "--v1-compatible",
            f"{tmpdir}/policy.rego",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "opa check timed out"
    except FileNotFoundError:
        return "OPA binary not available on the API server"
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        detail = (stderr.decode("utf-8", "replace") or stdout.decode("utf-8", "replace")).strip()
        # Strip the internal temp path so the error reads `policy.rego:N`.
        detail = detail.replace(f"{tmpdir}/policy.rego", "policy.rego")
        return detail[:2000] or "Rego failed to compile"
    return None
