"""Split the integration suite across runners, balanced by test count.

The integration slice runs serially — its session-scoped table-creation fixture
races under xdist workers, which share one database. Runner-level shards do not
have that problem: each gets its own compose stack, exactly as the `unit` and
`services-api` slices already do.

Balancing by **test count** rather than by file count is deliberate, and the
measurements say it is enough. Across five CI runs the per-test cost is uniform
at 0.58-1.07 s/test — the work is fixture setup and teardown, which every test
pays equally, so a file's duration is its test count times a constant. There are
no slow tests to special-case.

Two properties matter more than optimal packing, because both failure modes are
silent:

- **Total.** Every collected file lands in exactly one shard. A new test file
  cannot be missed, which is the hazard of a hand-maintained path list — the
  `unit` slice already carries a warning about exactly that.
- **Deterministic.** The same input gives the same split on every runner, so
  shard 2 runs the same files on the retry as it did on the first attempt.

Splitting by file rather than by test keeps a module's tests together, so
module-scoped fixtures behave as they do today.
"""

from __future__ import annotations


def plan_shards(counts: dict[str, int], shards: int) -> list[list[str]]:
    """Pack files into `shards` groups of roughly equal test count.

    `counts` maps a file identifier to its number of collected tests. Returns one
    list of file identifiers per shard.

    Greedy longest-first: repeatedly place the largest remaining file into the
    lightest shard. Not optimal in general, but for this distribution — 28 files
    of 2-55 tests — it lands within one test of perfect (123/123/122 at three
    shards), and being obvious matters more here than being optimal.

    Ties break on the file name so the result cannot depend on dict ordering.
    """
    if shards < 1:
        raise ValueError(f"shards must be >= 1, got {shards}")
    if shards == 1:
        return [sorted(counts)]

    buckets: list[list[str]] = [[] for _ in range(shards)]
    load = [0] * shards

    # Descending by count, then by name: deterministic regardless of input order.
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        target = load.index(min(load))
        buckets[target].append(name)
        load[target] += count

    return [sorted(b) for b in buckets]
