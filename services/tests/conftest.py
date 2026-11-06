"""
Top-level test configuration for Terrapod.

The test suite is organised by tier — directory layout maps 1:1 to the
CI Python Test matrix (see "Code ↔ Tests Contract" in CLAUDE.md):

  tests/auth/           ─┐
  tests/runner/          │  shard: unit          (pytest-xdist -n auto)
  tests/storage/         │  fast, pure / mocked, no DB
  tests/test_logging…   ─┘

  tests/services/       ─┐  shards: services-api-1..6  (pytest-xdist -n auto,
  tests/api/            ─┘  and `--shard k/6` across runners); AsyncMock-driven

  tests/integration/    ──  shards: integration-1..8  (serial — real Postgres)

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


# The token signing key is no longer derived lazily from the database URL — it is
# resolved once in the app lifespan and `get_token_signing_key()` raises before
# that (#1994). Runner tokens, run-task callbacks, download tickets and Slack
# link tokens all sign with it, so without this fixture every one of those paths
# would fail in tests for a reason that has nothing to do with what they assert.
#
# A deployment always has one: the lifespan resolves it before serving traffic and
# crashes if it cannot, so installing one here matches production rather than
# papering over a state the API never serves in.
#
# Only installed when nothing else has set it, and restored afterwards, so a test
# that drives initialization itself keeps the state it sets.


@pytest.fixture(autouse=True)
def _stateless_token_signing_key():
    import secrets

    from terrapod.auth import token_signing

    previous_key = token_signing._signing_key
    previous_provenance = token_signing._provenance
    if previous_key is None:
        token_signing._set_key_for_tests(secrets.token_bytes(32))
    try:
        yield
    finally:
        token_signing._signing_key = previous_key
        token_signing._provenance = previous_provenance


# No test may reach a real Kubernetes cluster (#2406).
#
# A test that forgets to patch a Kubernetes call behaves DIFFERENTLY by
# environment: on a workstation it issues the real call against whatever context
# happens to be current and passes, while on a CI runner with no cluster it fails
# closed and fails. That is not a hypothetical -- it is how two tests in #2399
# passed the entire local blast radius and then failed in CI.
#
# The guard is on CONFIG LOADING rather than on a list of accessors, because
# every Kubernetes client must load config before it can be built. That closes
# the class: `job_manager.init_k8s` loads it (`job_manager.py:94-102`), and so
# does `component_status._sample_blocking`, which builds its own `CoreV1Api`,
# `AppsV1Api` and `PolicyV1Api` with its own `load_kube_config()` and never goes
# through `job_manager` at all. An accessor-shaped guard cannot see that one, and
# an accessor nobody has written yet is covered here for free.
#
# The two accessors are ALSO refused, for two reasons: the message can name the
# fix precisely, and a client cached by an earlier test would otherwise be handed
# out without loading config again. The cached globals are cleared as well, since
# restoring the accessor would otherwise hand back a live client built earlier in
# the worker process.
#
# WHERE THE MESSAGE SURFACES, honestly: on a direct Job or pod call it propagates.
# On the ADMISSION path it does NOT -- `_get_batch_api()` is called inside
# `list_occupying_runner_jobs`' retry `try` deliberately, so that an
# uninitialised client is treated as a transient failure, and the blanket
# `except Exception` swallows this RuntimeError exactly as it swallows a real one.
# The author sees occupancy come back `None` and admission fail closed, plus the
# "Could not determine occupying runner-Job names" warning. That is the right
# production behaviour and it is worth knowing it hides this message, so a test
# whose runs mysteriously never launch should suspect an unpatched occupancy call.
#
# A test that genuinely wants a client patches the accessor itself, which
# overrides this.
@pytest.fixture(autouse=True)
def _no_real_kubernetes():
    from kubernetes import config as k8s_config

    from terrapod.runner import job_manager

    def _refuse(*_args, **_kwargs):
        raise RuntimeError(
            "This test reached the real Kubernetes client. Patch what it calls "
            "instead -- e.g. patch('terrapod.runner.job_manager."
            "list_occupying_runner_jobs') for admission, "
            "patch('terrapod.runner.job_manager._get_batch_api') for a Job call, "
            "patch('terrapod.runner.job_manager._get_core_api') for a pod or "
            "Secret call, or patch asyncio.to_thread for component status. "
            "Left unpatched, this test passes on a machine with a kubeconfig and "
            "fails in CI (#2406). Note the admission path swallows this message "
            "and reports occupancy as unknown instead."
        )

    loaders = ("load_incluster_config", "load_kube_config")
    saved_loaders = {name: getattr(k8s_config, name) for name in loaders}
    saved_accessors = {
        name: getattr(job_manager, name) for name in ("_get_batch_api", "_get_core_api")
    }
    saved_clients = (job_manager._batch_v1, job_manager._core_v1)
    for name in saved_loaders:
        setattr(k8s_config, name, _refuse)
    for name in saved_accessors:
        setattr(job_manager, name, _refuse)
    job_manager._batch_v1 = None
    job_manager._core_v1 = None
    try:
        yield
    finally:
        for name, previous in saved_loaders.items():
            setattr(k8s_config, name, previous)
        for name, previous in saved_accessors.items():
            setattr(job_manager, name, previous)
        job_manager._batch_v1, job_manager._core_v1 = saved_clients
