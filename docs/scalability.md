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
# 1. bring the stack up in the horizontal-scale profile. This builds the
#    PRODUCTION web image (standalone `node server.js`), not the Tilt dev
#    `next dev` server, so the BFF is measured as it actually ships.
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

### 3. Horizontal scaling — both tiers auto-scale, no leader election

The API and the BFF/web tier both run behind HPAs (API min 2 / max 6, web min 1
/ max 3). Under sustained 64-client load through the **load-balanced production
ingress** (CDN → ingress → BFF → API), starting from idle (API 2 / web 1):

- CPU on both tiers crossed the HPA target (API peaked ~270% of its 60% target,
  web ~219% of 70%), and **both tiers scaled up: API 2 → 4 → 6 replicas, web
  1 → 3 replicas.** As the added replicas came online, CPU% fell back toward
  target (API to ~96%, web to ~96%) — the added capacity absorbed the load.
- At steady state (6 API / 3 web) the stack served **9,441 requests with 0
  errors at 315 req/s** (p50 175 ms) through the full proxy chain — on one
  8-vCPU node, so the *absolute* number is node-bound; the point is that both
  stateless tiers scale out under load, with no leader contention, and load
  distributes across every replica.

> **The BFF scales too.** The frontend/BFF is stateless (it proxies `/api/*` and
> does SSR) and horizontally scalable — the run above scaled it 1 → 3 under load.
> **This must be measured with the production web image** (`node server.js` on
> the standalone build), which the `--scale` profile builds. The Tilt dev image
> (`next dev`) is a single-process JIT dev server that bottlenecks on latency,
> not CPU, so it neither represents production nor trips a CPU-based HPA — do not
> benchmark the BFF against it. (One sizing note CPU-HPA doesn't capture: the BFF
> also carries all SSE/long-lived streams, so size it for concurrent
> *connections*, not just request rate.)

### 4. Run scheduling — the control plane stays healthy; execution backs up sanely

Job scheduling is the sharp end of "at scale", and the design goal is specific:
**the control plane stays responsive under an arbitrarily large run backlog, and
execution concurrency is bounded by the compute you give the runner Jobs — with
the excess backing up gracefully, not collapsing anything.** These are two
independent axes:

- **Control plane** (API, listener, web) — stateless, all behind HPAs, no leader
  election. Under load they scale out and drain the run queue with
  `SELECT … FOR UPDATE SKIP LOCKED`, so every run is claimed exactly once with no
  coordination. Correctness (exactly-once dispatch, no lost/double-claimed runs)
  holds across a multi-replica control plane.
- **Execution** (runner Jobs) — bounded by real CPU/memory. Each run becomes a
  Kubernetes Job requesting the workspace's `resource_cpu`/`resource_memory`.
  When the cluster has capacity the Jobs run; when it doesn't, they stay
  **`Pending`** — which is **correct backpressure, not a scaling failure** — and
  drain as capacity frees. On a cluster-autoscaling setup (Karpenter, ASGs with
  headroom), those `Pending` Jobs are exactly the signal that adds nodes, so the
  backlog then executes on fresh compute. Terrapod **waits out that
  provisioning** — a Job whose pod a cluster autoscaler is actively adding a node
  for (`TriggeredScaleUp` / `Nominated`) is *not* failed as "unschedulable"; only
  a pod with no path to scheduling is failed, and then fast with an actionable
  "insufficient resources / unsatisfiable nodeSelector" message. And when the run
  queue is drained under load, the listener admits Jobs **against the live K8s
  active-Job count** and **fails closed** (defers, leaving runs queued) if it
  can't confirm capacity — so a burst never stampedes the apiserver into an
  over-launch storm.

So the honest way to read a huge backlog is: **a large number of *queued* runs is
the platform working, not failing** — it means more execution compute is needed,
which you add by scaling the runner side (nodes / agent pools / listeners),
independently of the control plane. What must NOT happen is the control plane
degrading under that backlog; that stays healthy by scaling out (HPAs) and, when
runner Jobs share compute with it, by out-prioritising them (see
[Troubleshooting](#troubleshooting-protecting-the-control-plane-from-runner-jobs)).

> **On the single-node reference rig this is only partly observable.** A fixed,
> co-located 8-vCPU node is precisely the *resource-exhaustion* case — there's no
> second node to autoscale onto and the runner Jobs share the node with the
> control plane. You can see the backpressure engage (Jobs correctly held
> `Pending` once the node's requestable CPU is full), but a clean end-to-end drain
> of an overwhelming backlog needs either headroom to autoscale into or the
> control-plane priority protection below — it is not a property a single shared
> node can demonstrate on its own. Reproduce the full picture on a multi-node or
> autoscaling cluster.

## Troubleshooting: protecting the control plane from runner Jobs

The control plane (API, listener, web) and the runner Jobs only contend for
resources when they **share compute**. In the standard topology they don't —
runner Jobs run in agent pools on their own nodes, separate from the API — so
there is nothing to tune. Two situations change that:

- **A cluster that autoscales with headroom (Karpenter, ASGs with high
  ceilings).** You generally don't need to do anything. When runner Jobs can't
  fit, they go `Pending`, the autoscaler adds nodes, and they schedule on fresh
  compute. There's no exhaustion, so there's no contention to protect against —
  you can get away without priority classes entirely.
- **A fixed or capacity-constrained cluster where runner Jobs can land on the
  same nodes as the control plane.** Here a burst of Terraform Jobs can exhaust a
  node and degrade the co-located API/listener. **In this case — and only when
  there is a real risk of resource exhaustion — runner Jobs MUST be given lower
  scheduling priority than the control-plane (and other long-lived deployment)
  pods**, so the scheduler places the control plane first and reaps runner Jobs
  first under pressure.

The requirement is only that **runner Jobs end up lower priority than the
deployment pods** — how the PriorityClasses come to exist is the operator's
choice:

- **Your cluster already has suitable PriorityClasses** (e.g. an org-wide
  "platform-high / batch-low" scheme)? Just point Terrapod's components at them
  and leave `priorityClasses.create: false` — set `api.priorityClassName` /
  `listener.priorityClassName` / `web.priorityClassName` to the higher class and
  `runners.priorityClassName` to the lower one. No need to have the chart create
  more.
- **Otherwise, let the chart create them:** `priorityClasses.create: true`
  renders a control-plane class (auto-applied to api/listener/web) and a lower
  runner class (auto-applied to runner Jobs). If runner Jobs may also share nodes
  with *un-classed* pods you want them to yield to (e.g. an in-cluster
  Postgres/Redis, or the metrics-server), set `priorityClasses.runner.value`
  **negative** so runner Jobs sit beneath everything.

One caveat to set expectations: a PriorityClass governs **scheduling,
preemption, and eviction** — it does **not** change runtime CPU (CFS) shares. So
it reliably protects the control plane on the *scheduling and memory-pressure*
axis (control-plane pods get placed first; runner Jobs get evicted first), but it
is not a fix for sustained CPU starvation of a control plane that is genuinely
sharing a saturated node. For that, the answer is topology — give runner Jobs
their own nodes (agent pools / `nodeSelector` + taints), or let the cluster
autoscale — rather than co-locating heavy Terraform execution with the API.

## What scales, and what is bounded by your cluster

| Dimension | Scales by | Bounded by |
|---|---|---|
| Read/API throughput | Adding stateless API replicas (HPA); no leader contention | The load balancer and Postgres/Redis you point it at |
| List latency at large workspace counts | O(page) paginated reads | — (flat) |
| Run **dispatch** (claim → state transition) | Multiple replicas draining one queue via `SKIP LOCKED` | Postgres write throughput |
| Run **execution** (concurrent Terraform Jobs) | More runner listeners / agent pools / nodes; a cluster-autoscaler adds nodes when Jobs go `Pending` | Real CPU/memory of your execution cluster. Excess Jobs back up (`Pending`/queued) — correct backpressure, not a platform limit |

The control plane (everything except running Terraform itself) scales
horizontally. Terraform execution is bounded by the compute you give the runner
Jobs — which you scale independently by adding nodes or agent pools, exactly as
you would size any CI fleet. A large backlog of *queued* runs is the platform
working as designed (more execution compute is needed), not the platform
failing — provided the control plane stays healthy, which is what the
autoscaling + priority guidance above ensures.

## Honest limits of the reference measurement

- Single node, so it demonstrates *properties* (flat per-item latency, correct
  concurrent dispatch, linear replica throughput, backpressure engaging) rather
  than an absolute concurrent-Jobs figure. Reproduce on a multi-node cluster to
  measure execution throughput at your own scale.
- A single node is also, by definition, the *resource-exhaustion* case: runner
  Jobs share it with the control plane and there's no second node to autoscale
  onto. So it is the one topology where the control-plane priority protection
  (above) matters most — and even that protects scheduling/eviction, not runtime
  CPU. The realistic deployments (separate runner nodes, or an autoscaling
  cluster) don't have this contention; don't read the single-node ceiling as a
  Terrapod limit.
- The embedded Postgres/Redis in `values-scale.yaml` are eval/dev datastores
  sized to not be the bottleneck for the test; production should use a managed
  Postgres and Redis.
- These are engineering measurements, not a benchmark marketing claim. The value
  is that they are **reproducible** — the harness and the profile are in the repo;
  run them and see.
