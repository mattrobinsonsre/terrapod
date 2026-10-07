"""Tests for the API-side OPA Rego validator (#343).

`policy_engine.py` now exposes a single function: ``check_rego``, used
at write time to reject broken Rego before it ever reaches a runner.
Evaluation itself runs on the runner (see
``terrapod.runner.phases.opa`` and its tests).

The tests skip cleanly when the ``opa`` binary isn't on PATH so the
suite can still run in environments without OPA. The test image
installs OPA so CI exercises the real binary.

**They pass ``opa_binary`` explicitly, and that is the point rather than a
shortcut.** `api_opa.opa_binary()` does not consult PATH -- by design, since
#1208 made the cache the only source -- so a test that omitted the argument
did not use the image's binary at all: it reached the acquisition path and
fetched OPA from the network, inside a tier that mocks its database. That
coupling was invisible until the acquisition path started requiring storage.
Acquisition has its own tests in ``test_api_opa.py``; these are about the
validator, and the one thing they need from acquisition -- that a failure
degrades rather than rejects -- is pinned below instead of inferred.
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
    assert await policy_engine.check_rego(VALID_POLICY, opa_binary="opa") is None


@needs_opa
async def test_check_rego_rejects_broken() -> None:
    err = await policy_engine.check_rego(BROKEN_POLICY, opa_binary="opa")
    assert err is not None
    # The internal temp path must not leak into the error message.
    assert "/tmp/tp-policy-" not in err


async def test_check_rego_reports_missing_binary() -> None:
    """When OPA isn't on PATH the validator returns a clear message
    rather than a cryptic FileNotFoundError. We exercise this by
    pointing at a deliberately bogus binary name."""
    err = await policy_engine.check_rego(VALID_POLICY, opa_binary="opa-does-not-exist")
    assert err == "OPA binary not available on the API server"


async def test_check_rego_degrades_when_opa_cannot_be_obtained(monkeypatch) -> None:
    """Acquisition failing must not reject the policy.

    `check_rego`'s own docstring promises this: "the returned string says
    validation is unavailable, and the caller treats that as a warning rather
    than a rejection". A sealed install with a cold cache is the real case, and
    the alternative -- refusing every policy-set write because the validator is
    unavailable -- would be a far worse failure than not validating.

    Distinct from `test_check_rego_reports_missing_binary`, which supplies a
    bogus binary NAME and so exercises the subprocess-missing path further down.
    This is the acquisition path returning None, which is the one thing these
    tests take from `api_opa`.
    """

    async def _unavailable() -> None:
        return None

    monkeypatch.setattr(policy_engine.api_opa, "opa_binary", _unavailable)
    assert await policy_engine.check_rego(VALID_POLICY) == policy_engine.VALIDATION_UNAVAILABLE
