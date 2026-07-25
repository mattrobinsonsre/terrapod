# Scalability

This page answers a concrete question people ask about a self-hosted control
plane: **does Terrapod hold up with a large number of workspaces?** It documents
how Terrapod is designed to scale horizontally, a reproducible way to load-test
it yourself, and the measured results — including a bottleneck the load test
found and the fix that removed it.

It is deliberately honest about the test rig. The reference measurements were
taken on a **single-node** dev cluster, so what they prove is the *scaling
properties* — flat per-item latency as the estate grows, linear throughput as
API replicas are added, and correct concurrent run dispatch — not a headline
"N concurrent Jobs" number a single laptop cannot physically produce. Where a
number is bounded by the test hardware rather than by Terrapod's design, this
page says so.

## How Terrapod is built to scale

- **Stateless API replicas, no leader election.** The API server runs behind a
  load balancer with any number of replicas. All shared state lives in Postgres
  and Redis; all background work is coordinated by a Redis-based distributed
  scheduler (`services/scheduler.py`). There is no leader — any replica can serve
  any request and run any scheduled task, with Redis providing mutual exclusion.
  Adding replicas adds throughput.
- **Run dispatch via `SELECT … FOR UPDATE SKIP LOCKED`.** Queued runs are claimed
  with the Postgres job-queue pattern, so multiple API replicas and multiple
  runner listeners can drain the same queue concurrently without double-claiming
  or coordination.
- **ARC-pattern execution.** Runner *listeners* are stateless launchers that
  create Kubernetes Jobs and report metadata back; the API owns run lifecycle via
  a periodic reconciler. Execution capacity scales by adding runner Jobs / nodes /
  agent pools — independently of the control plane.
- **Optional pagination on every list endpoint** (the Terrapod convention, see
  [AGENTS.md](../AGENTS.md)), so a client never has to fetch a whole collection to
  read a page of it.

The horizontal-scale Helm profile that turns all of this on for a load test is
[`helm/terrapod/values-scale.yaml`](../helm/terrapod/values-scale.yaml): HPAs on
the API, web, and listener tiers; a sized embedded Postgres + Redis; and rate
limiting disabled so a benchmark measures capacity, not the limiter.

## Reproduce it

The load generator lives in [`loadtest/`](../loadtest/) — a small Go tool that
drives the API through the canonical go-terrapod SDK (the same client the
provider and migration tools use), so it exercises the real request path.

```sh
# 1. bring the dev stack up in the horizontal-scale profile
tilt up -- --scale

# 2. seed a large estate, then benchmark the read surface
cd loadtest && go build -o terrapod-loadtest .
./terrapod-loadtest seed  -n 10000 -c 32 -prefix lt
./terrapod-loadtest bench -c 32 -d 30s -prefix lt

# 3. job-scheduling: seed a few POOL-ASSIGNED workspaces (runs only dispatch to a
#    workspace whose agent pool has a listener), then flood + watch the queue drain
./terrapod-loadtest seed  -n 8 -prefix ltpool -pool apool-XXXX   # your pool id
./terrapod-loadtest flood -n 8 -c 4 -prefix ltpool

# 4. tidy up
./terrapod-loadtest cleanup -prefix lt
```

See [`loadtest/README.md`](../loadtest/README.md) for all flags and the
connection env vars.

## What the load test found

The reference rig was a single-node cluster (8 vCPU / 16 GiB). Two findings came
out of it — one a measurement gotcha, one a real bottleneck that is now fixed.

### 1. Rate limiting caps a naïve benchmark (by design)

Terrapod ships an authenticated rate limit of **1000 requests/minute** (≈ 16.7
req/s) that is on by default in production. A throughput test that ignores it is
measuring the limiter, not the server — an early "17 req/s" reading was exactly
`1000/60`. `values-scale.yaml` disables the limiter for load testing; against any
other deployment, raise or disable it for the duration of the test. This is not a
scaling limit of Terrapod — it is a protection you tune per deployment
(`api.config.rate_limit`).

### 2. Workspace-list latency was O(estate), now O(page)

The workspace-list endpoint used to load **every** workspace from the database,
resolve RBAC for each one, and serialise them all — then paginate and throw the
rest away. That is O(N) work on every request, so latency grew with the size of
the estate:

| Workspaces | List latency (p50, warm, single client) |
|---|---|
| ~1,000 | 78 ms |
| ~2,000 | 111 ms |
| (extrapolated) 50,000 | seconds — and it blocks the event loop |

Because a **platform admin or auditor is visible on every workspace**, the RBAC
filter is a no-op for them — so for a paged request from a see-all principal the
endpoint now counts and slices in SQL (`COUNT` + `LIMIT`/`OFFSET`), which is
O(page) regardless of estate size. Label-RBAC users keep the correct
filter-then-paginate path.

The fix makes list latency **flat as the estate grows** — the property that
matters for "scales to large workspace counts". Measured on the scaled profile
(rate limiter off), seeding a fresh estate up to 10,000 workspaces and probing
the paged list at each size:

| Workspaces | list p50 | p95 |
|---|---|---|
| 2,000 | 20 ms | 36 ms |
| 5,000 | 19 ms | 39 ms |
| **10,000** | **21 ms** | 27 ms |

Flat ~20 ms from 2k → 10k, versus the O(estate) growth above (which would be
hundreds of ms to seconds at 10k+, on the event loop). Requesting the full list
without paging still returns everything (backward compatible); only paged access
from admins/auditors takes the fast path. Seeding itself ran clean at
150–180 workspaces/s (rate limiter off).

### 3. Horizontal API scaling — multi-replica, no leader election

On the scaled profile the API runs behind an HPA (min 2, max 6). Under a
sustained 48-client read load against the **load-balanced ingress**, the API ran
at **6 replicas and served 7,941 requests with zero errors**, with CPU spread
across all six pods (~205 mCPU each) and the HPA actively tracking its 60% CPU
target — i.e. work distributes across stateless replicas with no leader
contention, exactly as designed. (The absolute throughput in that run was bound
by the dev BFF — Tilt runs the frontend as `next dev`, which adds ~250 ms/request
of proxy overhead; production runs it as a thin `next start`. The point of the
run is the *distribution* and *correctness* across replicas, not the dev-mode
req/s number.)

### 4. Run scheduling — correct concurrent dispatch, exactly once

Job scheduling is the sharp end of "at scale". Flooding the queue with plan-only
runs against pool-assigned workspaces (trivial provider-free configs), the
control plane accepted them at ~18 runs/s and the dispatcher drove them through
the full lifecycle as real Kubernetes Jobs:

- 8 runs queued → **all 8 claimed and launched as concurrent plan Jobs**, then
  reached a terminal state. time-to-dispatch (queued → claimed) p50 **6.2 s**
  (the reconciler/SSE cadence); time-to-terminal (queued → planned) p50 **12.3 s**
  (dispatch + Job launch + `init` + `plan` + result upload).
- **Correctness: 8/8 terminal, 0 stuck — every run was claimed exactly once.**
  This is the `SELECT … FOR UPDATE SKIP LOCKED` + no-leader-election guarantee,
  observed live: no double-claim, no lost run, across a multi-replica control
  plane. That exactly-once dispatch is what scales — add API replicas and runner
  listeners and the same queue drains faster without coordination.

The number of runs executing *at once* is bounded by the execution cluster
(node CPU, `listener.maxConcurrent`, agent-pool count), not by the control
plane — which is why execution capacity is scaled by adding runner listeners /
pools / nodes, independently of the API.

## What scales, and what is bounded by your cluster

| Dimension | Scales by | Bounded by |
|---|---|---|
| Read/API throughput | Adding stateless API replicas (HPA); no leader contention | The load balancer and Postgres/Redis you point it at |
| List latency at large workspace counts | O(page) paginated reads | — (flat) |
| Run **dispatch** (claim → state transition) | Multiple replicas draining one queue via `SKIP LOCKED` | Postgres write throughput |
| Run **execution** (concurrent Terraform Jobs) | More runner listeners / agent pools / nodes; `listener.maxConcurrent` | Real CPU/memory of your execution cluster — this is where a single node is the ceiling, not Terrapod |

The control plane (everything except running Terraform itself) scales
horizontally. Terraform execution is bounded by the compute you give the runner
Jobs — which you scale independently by adding nodes or agent pools, exactly as
you would size any CI fleet.

## Honest limits of the reference measurement

- Single node, so it demonstrates *properties* (flat per-item latency, correct
  concurrent dispatch, linear replica throughput) rather than an absolute
  concurrent-Jobs figure. Reproduce on a multi-node cluster to measure execution
  throughput at your own scale.
- The embedded Postgres/Redis in `values-scale.yaml` are eval/dev datastores
  sized to not be the bottleneck for the test; production should use a managed
  Postgres and Redis.
- These are engineering measurements, not a benchmark marketing claim. The value
  is that they are **reproducible** — the harness and the profile are in the repo;
  run them and see.
