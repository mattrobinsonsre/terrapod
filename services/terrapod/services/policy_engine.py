"""OPA Rego validation (write-time only) for the API (#343).

OPA policy *evaluation* runs on the runner — see
``docker/runner-entrypoint.sh`` and the ``/policy-bundle`` /
``/policy-results`` endpoints in ``api/routers/policy_sets.py``. This
module keeps a single API-side responsibility: validate that a Rego
document compiles and is well-formed at the moment an operator creates
or updates a policy, so broken Rego is rejected up front rather than
silently failing later inside the runner.

``opa`` is used here **only** for ``opa check``. The heavy evaluation
work — per-run, per-policy, high-volume — lives on the runner, where the
plan JSON is already local and CPU/memory naturally scales with K8s.

Since #1208 the binary is fetched on demand (see
:mod:`terrapod.services.api_opa`) rather than baked into the API image,
so its version is a Helm value. If it cannot be obtained this check
reports that validation is unavailable and the write proceeds: Rego that
slips past a *syntax* check here still fails closed at evaluation time on
the runner, so degrading is the proportionate response.

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
import re
import shutil
import tempfile

import structlog

from terrapod.services import api_opa

logger = structlog.get_logger(__name__)

# Kept as the name to exec when a caller supplies no path — the test image still
# bakes OPA in (docker/Dockerfile.test), so the 143 policy tests keep exercising
# a real binary rather than a mock.
OPA_BINARY = "opa"

# Hard ceiling on `opa check`. The command is fast — this just guards
# against a wedged subprocess.
CHECK_TIMEOUT_SECONDS = 15.0

#: Returned when OPA itself could not be obtained, as distinct from "your Rego
#: is broken". The caller must tell the two apart: rejecting a policy write
#: because an unrelated fetch failed would leave an operator unable to edit any
#: policy, while the thing that was skipped — a syntax check — still happens at
#: evaluation time on the runner, which fails closed.
VALIDATION_UNAVAILABLE = "OPA binary not available on the API server"


#: What makes a `.rego` file a Terrapod policy. ONE producer, because this was
#: written twice -- in `api/routers/policy_sets.py` and in `policy_vcs_poller`
#: -- and the two had already diverged (#2715): the poller counted a `warn`
#: rule and the API did not, so a warn-only advisory policy synced cleanly from
#: VCS and was a 422 through the API or the UI. The poller's reading is the
#: correct one and its comment says why: a set may be advisory-only, and a file
#: that produces warnings is a policy by any reading.
#:
#: `\b` alone would match at the `.` in `terrapod.foo`, so the package anchor
#: ends with `\s*$` or a comment instead. v1 syntax allows
#: `deny contains msg if { ... }`, `deny := [...]` and `deny = ...`, so the
#: rule-head patterns match at the start of a line.
POLICY_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+terrapod\s*(#.*)?$")
DENY_RULE_RE = re.compile(r"(?m)^\s*deny\s+(contains|:=|=)")
WARN_RULE_RE = re.compile(r"(?m)^\s*warn\s+(contains|:=|=)")


def declares_a_verdict(rego: str) -> bool:
    """Does this Rego produce a verdict -- a `deny` OR a `warn` rule?

    A policy in `package terrapod` that declares neither would silently always
    pass, which is what both callers are screening for.
    """
    return bool(DENY_RULE_RE.search(rego) or WARN_RULE_RE.search(rego))


def is_terrapod_policy(rego: str) -> bool:
    """The whole rule: the right package AND a verdict rule."""
    return bool(POLICY_PACKAGE_RE.search(rego)) and declares_a_verdict(rego)


def _write_rego_to_tempdir(rego: str) -> str:
    """Create a temp dir holding the Rego source. Sync filesystem work —
    call via ``asyncio.to_thread``. Returns the temp dir path; the
    caller is responsible for removing it."""
    tmpdir = tempfile.mkdtemp(prefix="tp-policy-")
    with open(f"{tmpdir}/policy.rego", "w", encoding="utf-8") as fh:
        fh.write(rego)
    return tmpdir


async def check_rego(rego: str, *, opa_binary: str | None = None) -> str | None:
    """Validate that a Rego document compiles. Returns an error string
    on failure, or ``None`` if it is well-formed. Used by the policy
    CRUD API to reject broken Rego at write time rather than at
    runner-eval time.

    With no ``opa_binary`` the binary is obtained on demand (#1208). If it
    cannot be — no upstream reach, a sealed install with a cold cache — the
    returned string says validation is unavailable, and the caller treats that
    as a warning rather than a rejection: see the module docstring for why
    degrading is the proportionate response here.
    """
    if opa_binary is None:
        opa_binary = await api_opa.opa_binary()
        if opa_binary is None:
            return VALIDATION_UNAVAILABLE

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
        return VALIDATION_UNAVAILABLE
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        detail = (stderr.decode("utf-8", "replace") or stdout.decode("utf-8", "replace")).strip()
        # Strip the internal temp path so the error reads `policy.rego:N`.
        detail = detail.replace(f"{tmpdir}/policy.rego", "policy.rego")
        return detail[:2000] or "Rego failed to compile"
    return None
