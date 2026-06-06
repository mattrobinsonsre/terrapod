"""Tests for the API-side OPA Rego validator (#343).

`policy_engine.py` now exposes a single function: ``check_rego``, used
at write time to reject broken Rego before it ever reaches a runner.
Evaluation itself runs on the runner (see
``terrapod.runner.phases.opa`` and its tests).

The tests skip cleanly when the ``opa`` binary isn't on PATH so the
suite can still run in environments without OPA. The test image
installs OPA so CI exercises the real binary.
"""

from __future__ import annotations

import shutil

import pytest

from terrapod.services import policy_engine

_OPA = shutil.which("opa") is not None
needs_opa = pytest.mark.skipif(not _OPA, reason="opa binary not on PATH")

VALID_POLICY = """
package terrapod

deny contains msg if {
    false
    msg := "never"
}
"""

BROKEN_POLICY = "package terrapod\n\ndeny contains msg if { ::: }\n"


@needs_opa
async def test_check_rego_accepts_valid() -> None:
    assert await policy_engine.check_rego(VALID_POLICY) is None


@needs_opa
async def test_check_rego_rejects_broken() -> None:
    err = await policy_engine.check_rego(BROKEN_POLICY)
    assert err is not None
    # The internal temp path must not leak into the error message.
    assert "/tmp/tp-policy-" not in err


async def test_check_rego_reports_missing_binary() -> None:
    """When OPA isn't on PATH the validator returns a clear message
    rather than a cryptic FileNotFoundError. We exercise this by
    pointing at a deliberately bogus binary name."""
    err = await policy_engine.check_rego(VALID_POLICY, opa_binary="opa-does-not-exist")
    assert err == "OPA binary not available on the API server"
