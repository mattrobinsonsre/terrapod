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

# Ensure test-friendly defaults
os.environ.setdefault("TERRAPOD_STORAGE__BACKEND", "filesystem")
os.environ.setdefault("TERRAPOD_JSON_LOGS", "false")
os.environ.setdefault("TERRAPOD_LOG_LEVEL", "DEBUG")
