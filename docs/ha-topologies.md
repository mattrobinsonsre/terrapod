# HA topologies: multi-region, multi-cloud, and where the listeners go

[High availability](high-availability.md) explains the mechanism — how a
leader/follower pair works, what a follower refuses to do, how replication and
the peer link behave. This page is about **layout**: where you put the pieces,
what each choice actually buys, and what it does not.

Terrapod is built for the awkward cases — separate regions, separate clouds, a
cluster you cannot reach inbound, a network with no route to the internet. The
architecture that makes those work is the same one that makes an ordinary
two-region deployment work, so the interesting topologies are not a special
mode.

---

## Three planes, three different answers

The single most useful thing to understand is that "is Terrapod highly
available?" is three questions, and they have **different answers with different
recovery characteristics**. Treating them as one number is how availability
plans go wrong.

| Plane | What it is | How it survives failure | Human involved? |
|---|---|---|---|
| **Control plane** | The API, the web UI, the scheduler | Multiple stateless replicas behind a load balancer, **no leader election** — any replica does any job, coordinated through Redis | **No.** Automatic, continuous |
| **Execution plane** | Runs: plan and apply on ephemeral Kubernetes Jobs | A workspace routes to **a set of agent pools**; a queued run is offered to all of them and whichever listener claims it first executes it | **No.** Automatic, per run |
| **Data plane** | Postgres, the object store, and the node identity behind the shared name | A warm follower with its own database and its own object store, kept current by replication | **Yes — you move DNS.** Deliberately |

The first two absorb failure without anyone being paged. The third does not, and
that is a design decision, not a gap: **Terrapod never votes and never
arbitrates**, so it cannot split-brain your state. See
[High availability → roles](high-availability.md#roles) for why.

Be precise when you plan against this. Losing a zone, a node, a listener, a
whole agent pool, or an API replica is **absorbed**. Losing the region or the
cloud that holds the leader's database is a **failover** — fast, rehearsed, and
documented, but a human decides.

---

## Topology 1 — Single region, and how far it gets you

Most deployments start here, and it is worth being clear that this is already a
genuinely resilient configuration, not a stepping stone.

```
        ┌──────────── region ────────────┐
        │  API × N   web × N   (no leader election)   │
        │  Postgres (managed, multi-AZ)               │
        │  Object store (regional, replicated by CSP) │
        │  Listeners in ≥2 pools, spread across AZs   │
        └─────────────────────────────────────────────┘
```

What it survives without a human: a pod, a node, an availability zone, a
listener, an entire agent pool (given more than one in the workspace's set).

What it does not survive: the region, or the database.

Do this much first. A second region protects against something a single region
already handles well, and it is worth having the cheaper layers right before
adding the expensive one.

**Enable, in order:** multiple API replicas and PodDisruptionBudgets (both on by
default), a managed multi-AZ Postgres, then a second agent pool in another zone
added to your workspaces' pool sets.

---

## Topology 2 — Two regions, same cloud

The common enterprise shape: a warm standby in a second region of the same
provider.

```
   region A (leader)                       region B (follower)
   ┌──────────────────────┐                ┌──────────────────────┐
   │ API × N              │                │ API × N (passive)    │
   │ Postgres A           │◀── replication ┤ Postgres B           │
   │ Bucket A             │      (B pulls) │ Bucket B             │
   └──────────┬───────────┘                └──────────┬───────────┘
              │                                       │
        terrapod.example.com  ─────── DNS ────────────┘
                (points at A; you move it to B)
```

Two things about this are worth stating plainly, because they are the parts
people expect to be harder than they are.

**The follower pulls.** Node B asks node A for changes; A never pushes. So a
follower that is down, slow, or broken cannot slow the leader down, and there is
no queue on the leader that backs up when the peer goes away. A peer outage is,
from the leader's point of view, nothing happening.

**Each node owns its own storage.** Separate database, separate bucket. That is
what makes the pair a real failure boundary — a shared database or a shared
bucket sits in one of the domains you are trying to survive, and takes both
nodes down together.

For the object store you then have a genuine choice, and it is **per prefix
class**, not global:

- **`verify`** (the default) — you have arranged replication elsewhere, typically
  the provider's own (S3 Cross-Region Replication, GCS dual-region, Azure object
  replication). Terrapod's job is then to **prove it actually happened** rather
  than duplicate it. Cheaper and usually better plumbing than anything Terrapod
  would write.
- **`copy`** — Terrapod pulls the objects across the peer link itself,
  irreplaceable classes first, streamed and throttled.

The reason it is per class is that "replicate state and configuration versions
across regions, but let the provider cache stay cold and re-warm on demand" is a
sensible position that a bucket-level policy cannot express — and cross-region
egress on a cache nobody needs is a real bill.

```yaml
api:
  config:
    ha:
      blobs:
        mode: verify        # observe everything, commit to nothing
        classes:
          state: copy                   # irreplaceable — carry it ourselves
          configuration_versions: copy  # the only copy for a CLI-uploaded workspace
          run_logs: "off"               # history; not worth the egress
```

**Before you ever fail over, check the second data plane.** The dangerous state
is *rows present, blobs absent* — a promoted node that lists four hundred
workspaces and cannot serve one. `GET /api/terrapod/v1/ha/blob-readiness` checks
that the objects its rows name are actually there, and names
`irreplaceable-missing` as the list that should stop a failover. It is worth
running **even under provider-native bucket replication**, where it catches a
misconfigured prefix or a lifecycle rule quietly expiring objects out from under
the rows.

---

## Topology 3 — Two clouds

Here the provider-native path does not exist. There is no cross-region
replication feature that spans AWS and Azure, so Terrapod copies over the peer
link itself — and this is where the design earns its keep.

```
   AWS (leader)                             Azure (follower)
   ┌──────────────────────┐                ┌──────────────────────┐
   │ RDS Postgres         │◀── replication ┤ Azure Database       │
   │ S3                   │◀── blob copy ──┤ Blob Storage         │
   └──────────────────────┘                └──────────────────────┘
```

Nothing above is a special mode. The **storage backends are independent per
node** — each speaks its provider's native SDK, and the replication protocol
carries rows and bytes, not provider-specific handles. A pair can therefore be
S3↔Blob, Blob↔GCS, or S3↔filesystem-on-a-PVC without either node knowing what
the other is using.

The same property covers **on-prem and air-gapped standbys**, which is the case
where there is no provider path at all and no internet to fall back on. Two
extra things matter there:

- **Encryption keys never travel.** Values are decrypted on send and
  re-encrypted under the receiving node's own key. A node in another cloud can
  hold your secrets under *its* KMS, not yours. See
  [Encryption at rest](encryption-at-rest.md).
- **A sealed node needs its caches carried.** With `registry.cache_only` the
  node is forbidden to reach upstream, so a promoted node with a cold provider
  cache cannot run anything at all — no terraform binary, no providers. Terrapod
  knows this: on a sealed node the upstream-fed caches report as
  **irreplaceable** rather than re-derivable, because for that deployment they
  are. Set them to `copy`.

---

## Where the listeners go

Two topologies, both supported, and **neither needs a shared certificate
authority** — each node generates and keeps its own.

### Dedicated listeners (per node)

Each node has its own listener fleet, pointed at its own per-node name. Nothing
ever crosses the boundary.

```
region A: listeners ──▶ terrapod-a.example.com
region B: listeners ──▶ terrapod-b.example.com
```

**Choose this when** the execution clusters are themselves regional, or when the
two sides have different network reachability or different cloud credentials.
Nothing re-points at a cutover and no listener changes the name it talks to.

> **A standby's fleet cannot join until that node is promoted.**
>
> Enrolling a listener is a write, and a follower refuses writes — so a listener
> pointed at a node that has never led retries indefinitely and never becomes
> ready, which also makes `helm install --wait` fail on that node
> ([#1191](https://github.com/mattrobinsonsre/terrapod/issues/1191)).
>
> Practical consequence: **neither topology gives you a standby fleet proven
> before promotion.** They differ in *how* they heal at cutover — the shared
> fleet re-joins because its certificates stop authenticating, the per-node
> fleet joins for the first time — not in whether.
>
> There is currently **no workaround**. Joining as leader and then demoting does
> not help: an already-joined listener's heartbeat is also a write, so it is
> refused too and the listener expires from the node's Redis a few minutes
> later. Plan for the standby's fleet to become useful at promotion, not before.

The trade is that node B's listeners sit idle while A leads (they are cheap —
they launch Jobs, they do not run terraform), and each side's pools must be
sized for the whole load if it may take the whole load.

### Shared listeners (following the DNS name)

One fleet, pointed at the shared name. At cutover it re-points at the promoted
node.

```
one fleet ──▶ terrapod.example.com ──▶ (A, then B after failover)
```

The certificates issued by node A stop authenticating against node B, so each
listener **re-joins** and heals itself — the trust chain is rebuilt at the
failover rather than carried through it. This works because join tokens
replicate: the promoted node already accepts the token the fleet is holding.

> **Size join tokens for this topology deliberately: long-lived, generously
> reusable.**
>
> Every failover costs **one use per listener**. A token with a tight `max_uses`
> sized for the initial rollout, or a short expiry, is exhausted by the first
> real failover — at precisely the moment nobody wants to be minting
> credentials. The dedicated topology never crosses a boundary and has no such
> constraint.

**Choose this when** the execution fleet lives somewhere that is not tied to
either node's region — a central build cluster, an on-prem estate, a managed
Kubernetes footprint of its own — and you would rather run one fleet than two.

---

## Multi-pool routing: the layer most people miss

A workspace does not route to *an* agent pool. It routes to **a set of them**,
and this is what turns the loss of a pool, a cluster, or a region's execution
capacity into a non-event.

```hcl
resource "terrapod_workspace" "payments" {
  name           = "payments-prod"
  execution_mode = "agent"

  agent_pool_ids = [
    terrapod_agent_pool.eu_west.id,
    terrapod_agent_pool.eu_north.id,
  ]
}
```

The set is **flat**. There is no primary, no rank, no preference, no rotation. A
queued run is published to **every** pool in its set at once and whichever
listener claims it first runs it — a single row claimed under `SELECT … FOR
UPDATE SKIP LOCKED`, so offering it to five pools does not risk it running
twice.

That deliberate absence of ranking is what makes it resilient rather than merely
configurable. There is no failover step to get wrong, no health check to be
stale, no grace period during which the "primary" is being declared dead:
whichever pool has a listener asking for work gets the work. Distribution is at
the mercy of poll timing, which is the accepted trade.

**What it survives:** a listener crash, a pool with no live listeners, a whole
execution cluster or region going away, a rolling upgrade of one fleet.

**What it does not:** a run already in flight when its cluster dies — that run
fails, and is re-queued like any other failed run. And note that pools belong to
the node that holds them; multi-pool routing gives you cross-*cluster*
resilience under one control plane, not cross-*node* execution. Those compose,
they do not substitute.

**Alert on it.** `terrapod_workspaces_without_live_pool` counts agent-mode
workspaces where **no** pool in the set has a live listener — runs queued
against them cannot execute. Because losing one pool is survivable, any non-zero
value means a workspace has lost *all* of its execution capacity, which makes it
a much better signal than per-pool liveness. See [Monitoring](monitoring.md).

Terrapod also surfaces this per workspace as a health condition, so the gap is
visible in the UI before someone queues a run into it.

---

## Putting it together: a worked layout

A two-region, same-cloud deployment aiming for a high control-plane availability
target with a rehearsed regional failover:

| Layer | Choice | Recovers |
|---|---|---|
| API / web | 3 replicas per node, PDBs on, spread across zones | Automatically, continuously |
| Postgres | Managed, multi-AZ, in each region | AZ loss automatically; region loss at failover |
| Object store | Provider cross-region replication + Terrapod `verify`; `copy` for state and config versions | Automatically, with Terrapod proving it |
| Execution | Two agent pools per region, all four in each production workspace's pool set | Pool, cluster, and region execution loss automatically |
| Listeners | Dedicated per node — both regions' capacity proven continuously | No re-join at cutover |
| Node identity | `ha.role: auto`, both nodes probing the shared name | **You move DNS** |

The honest summary of that table: **everything except the last row is
automatic.** The last row is a deliberate human act, it is documented as a
procedure, and it is the reason Terrapod cannot split-brain your state.

## Rehearse it

An untested failover is a hypothesis. The pair harness runs the whole
release-gate table — backfill from empty, catch-up inside retention, fallback to
backfill past retention, a peer outage not blocking the leader, the follower
refusing writes through the real API path, planned and unplanned failover, and
fail-back:

```sh
scripts/ha-smoke.sh up      # two full nodes, own Postgres and Redis each
scripts/ha-smoke.sh table   # walk the gate; every row reports PASS/FAIL/SKIP
scripts/ha-smoke.sh down
```

It prints a verdict per scenario, including the rows it did **not** exercise and
why — a partial run reported as a full one is exactly the failure a rehearsal is
supposed to prevent.

## Related

- [High availability](high-availability.md) — the mechanism: roles, the peer
  link, replication, what a follower refuses to do
- [HA operations](ha-operations.md) — the naming model, failover, failback,
  maintenance, and version skew
- [Disaster recovery](disaster-recovery.md) — backups, restore, and the
  break-glass state index
- [Agent execution & runners](runners.md) — agent pools, listeners, and pool sets
- [Encryption at rest](encryption-at-rest.md) — why keys never cross the link
- [Monitoring](monitoring.md) — the metrics to alert on, including
  `terrapod_workspaces_without_live_pool`
