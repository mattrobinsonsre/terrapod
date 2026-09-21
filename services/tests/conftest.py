"""
Top-level test configuration for Terrapod.

The test suite is organised by tier — directory layout maps 1:1 to the
CI Python Test matrix (see "Code ↔ Tests Contract" in CLAUDE.md):

  tests/auth/           ─┐
  tests/runner/          │  shard: unit          (pytest-xdist -n auto)
  tests/storage/         │  fast, pure / mocked, no DB
  tests/test_logging…   ─┘

  tests/services/       ─┐  shard: services-api  (pytest-xdist -n auto)
  tests/api/            ─┘  bulk of tests; AsyncMock-driven

  tests/integration/    ──  shard: integration   (serial — real Postgres)

When adding a test, put it under the directory whose tier it belongs
to, NOT whichever directory feels closest by file name. The CI matrix
expects the split. The integration shard stays serial because its
session-scoped Postgres table-creation fixture races under xdist
workers.
"""

import os

import pytest

# Ensure test-friendly defaults
os.environ.setdefault("TERRAPOD_STORAGE__BACKEND", "filesystem")
os.environ.setdefault("TERRAPOD_JSON_LOGS", "false")
os.environ.setdefault("TERRAPOD_LOG_LEVEL", "DEBUG")


# ── A CA for capability signing (#GHSA-r9v9-24fv-jxm2) ─────────────
#
# Plan/apply/configuration-version/state-version serialization mints a signed
# capability, and the signing key is derived from the CA private key, so those
# serializers now need a CA the way the listener endpoints always have.
#
# In a deployment there is always one: `init_ca` runs in the app lifespan, and a
# pod without a CA cannot verify a listener certificate either, so it can run
# nothing at all. Installing one here matches that, rather than papering over a
# state the API never serves traffic in.
#
# Only installed when nothing else has set it, and restored afterwards, so a
# test that drives CA initialisation itself (tests/integration/test_ca_init_race)
# still starts from the state it sets.


@pytest.fixture(autouse=True)
def _capability_signing_ca():
    from terrapod.auth import ca as ca_module

    previous = ca_module._ca
    if previous is None:
        ca_module._ca = ca_module.CertificateAuthority.generate()
    try:
        yield
    finally:
        ca_module._ca = previous
