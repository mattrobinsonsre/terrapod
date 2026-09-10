"""
Top-level test configuration for Terrapod.

The test suite is organised by tier — directory layout maps 1:1 to the
CI Python Test matrix (see "Code ↔ Tests Contract" in CLAUDE.md):

  tests/auth/           ─┐
  tests/runner/          │  shard: unit          (pytest-xdist -n auto)
  tests/storage/         │  fast, pure / mocked, no DB
  tests/test_logging…   ─┘

  tests/services/       ─┐  shards: services-api-1..3  (pytest-xdist -n auto,
  tests/api/            ─┘  and `--shard k/3` across runners); AsyncMock-driven

  tests/integration/    ──  shards: integration-1..4  (serial — real Postgres)

When adding a test, put it under the directory whose tier it belongs
to, NOT whichever directory feels closest by file name. The CI matrix
expects the split. The integration shard stays serial because its
session-scoped Postgres table-creation fixture races under xdist
workers.
"""

import os

import pytest

from .meta.shard_plan import plan_shards

# Ensure test-friendly defaults
os.environ.setdefault("TERRAPOD_STORAGE__BACKEND", "filesystem")
os.environ.setdefault("TERRAPOD_JSON_LOGS", "false")
os.environ.setdefault("TERRAPOD_LOG_LEVEL", "DEBUG")


# ── Runner-level sharding (#1468) ─────────────────────────
#
# `--shard k/N` keeps only the files assigned to shard k. Splitting happens
# AFTER collection, so it sees the true set of files and a new test file cannot
# be silently missed — the failure mode of a hand-maintained path list.
#
# Defined here rather than per tier so any tier can shard. Integration shards
# because its xdist workers would share one database; services-api shards
# because a 4-vCPU runner with `-n auto` is the ceiling on one job, and the
# suite builds the FastAPI app about a thousand times.


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        default=None,
        help="Run only this shard of the suite, as k/N (1-based). Splits by file, "
        "balanced on collected test count.",
    )


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return

    try:
        index_s, total_s = spec.split("/", 1)
        index, total = int(index_s), int(total_s)
    except ValueError:
        raise pytest.UsageError(f"--shard expects k/N, got {spec!r}") from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {spec}: k must be within 1..N")

    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.path)] = counts.get(str(item.path), 0) + 1

    keep = set(plan_shards(counts, total)[index - 1])
    selected = [i for i in items if str(i.path) in keep]
    deselected = [i for i in items if str(i.path) not in keep]

    # Printed so a shard that selects nothing is visible in the log rather than
    # passing as a vacuous success.
    print(
        f"\nshard {index}/{total}: {len(keep)} files, {len(selected)} tests "
        f"({len(deselected)} deselected)"
    )
    if not selected:
        raise pytest.UsageError(
            f"--shard {spec} selected no tests. With {len(counts)} files collected "
            "this means the split is wrong, not that there is nothing to run."
        )

    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
