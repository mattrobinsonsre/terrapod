"""OPA Rego validation (write-time only) for the API (#343).

OPA policy *evaluation* runs on the runner — see
``docker/runner-entrypoint.sh`` and the ``/policy-bundle`` /
``/policy-results`` endpoints in ``api/routers/policy_sets.py``. This
module keeps a single API-side responsibility: validate that a Rego
document compiles and is well-formed at the moment an operator creates
or updates a policy, so broken Rego is rejected up front rather than
silently failing later inside the runner.

The bundled ``opa`` binary on the API image is used **only** for
``opa check`` here. The heavy evaluation work — per-run, per-policy,
high-volume — lives on the runner, where the plan JSON is already
local and CPU/memory naturally scales with K8s.

Terrapod policy convention
--------------------------
A policy's Rego must declare ``package terrapod`` and express
violations via a ``deny`` set of message strings (Rego v1 syntax). See
``docs/policies.md`` for the full authoring contract; this file's job
is to enforce the *syntactic* requirement (that the Rego compiles).
The package-name and deny-rule requirements are enforced separately in
``api/routers/policy_sets.py``.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile

import structlog

logger = structlog.get_logger(__name__)

# The bundled OPA binary (installed on PATH by docker/Dockerfile.api).
OPA_BINARY = "opa"

# Hard ceiling on `opa check`. The command is fast — this just guards
# against a wedged subprocess.
CHECK_TIMEOUT_SECONDS = 15.0


def _write_rego_to_tempdir(rego: str) -> str:
    """Create a temp dir holding the Rego source. Sync filesystem work —
    call via ``asyncio.to_thread``. Returns the temp dir path; the
    caller is responsible for removing it."""
    tmpdir = tempfile.mkdtemp(prefix="tp-policy-")
    with open(f"{tmpdir}/policy.rego", "w", encoding="utf-8") as fh:
        fh.write(rego)
    return tmpdir


async def check_rego(rego: str, *, opa_binary: str = OPA_BINARY) -> str | None:
    """Validate that a Rego document compiles. Returns an error string
    on failure, or ``None`` if it is well-formed. Used by the policy
    CRUD API to reject broken Rego at write time rather than at
    runner-eval time.
    """
    tmpdir = await asyncio.to_thread(_write_rego_to_tempdir, rego)
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
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CHECK_TIMEOUT_SECONDS
            )
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
