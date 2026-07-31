# HA operations: names, failover, failback, and maintenance

The audience for this page is the person who will be doing it at 03:00.

[High availability](high-availability.md) is the mechanism and
[HA topologies](ha-topologies.md) is the layout. This is **procedure** — the
naming model you have to get right up front, and the runbooks for every
transition a pair goes through.

Terrapod deliberately does not supervise any of this. It never votes, never
arbitrates, and never moves DNS for you; its job is to behave predictably and to
make its state legible so that a competent operator does not need a guardrail.
That is why these are procedures rather than buttons.

---

## The naming model

Get this wrong and it breaks in durable ways, so it is worth ten minutes before
the first failover rather than ten minutes during one.

Every name doubles under HA: a **shared** form that moves at cutover, and a
**per-node** form that never does.

### Shared names — these cut over

| Name | Chart value | Used by |
|---|---|---|
| Shared **internal** | `internalIngress.hostname` | Private-network clients, and **the leadership probe** (preferred) |
| Shared **external** | `ingress.hostname` → `api.config.external_url` | The CLI `cloud` block, the web UI, generated registry module sources; probe fallback |
| Shared **webhook** | `webhookIngress.hostname` → `api.config.public_webhook_url` | VCS webhook delivery, run-task callbacks |

The probe is not a fourth name. It targets the *shared* name, preferring the
internal one, and the whole semantic is **"does traffic for the name clients use
reach me?"** Internal is preferred because it avoids hairpin NAT and any CDN or
WAF in front of the external name — either can cache or filter a probe and give
a node the wrong answer about itself.

> **`api.config.external_url` must be identical on both nodes.**
>
> It is not just link generation. The catalog derives the module `source =` host
> from it and writes that **into the generated configuration version**, which is
> persisted and replayed on every subsequent run. Set it per node and a catalog
> wrapper generated on one node points at a dead host forever after cutover.

### Per-node names — these never move

| Name | Used by |
|---|---|
| Per-node **internal** | The peer replication link (preferred — the nodes reach each other privately) |
| Per-node **external** | The peer link when the nodes share no private network (cross-region, cross-cloud) |

The peer link must address a **specific** node regardless of who leads, so it can
never use a shared name. Each node is configured with its peer's per-node
address, and **nothing about that configuration changes at failover** — which is
also why the pair needs no credential exchange during a cutover.

### The webhook name degrades gracefully — know this before you panic

A VCS webhook URL has a single target, so the follower never receives VCS events
anyway. If you forget to move the webhook name, deliveries keep landing on the
demoted node, which refuses to act on them.

**That costs a poll interval of latency, not lost events.** Webhooks only
*enqueue an immediate poll* — they are an accelerator, not the delivery
mechanism. The periodic VCS poll re-reads branch heads and open pull requests on
its own schedule regardless, so the estate keeps working, slightly slower.

This is the correct failure mode for something an operator can forget, and it is
a dividend of the polling-first design rather than luck: a webhook-only platform
would silently stop every VCS-driven run after a missed name, with no signal
until someone noticed pushes were not building. The runbook still lists the step
— a poll interval of latency on every push is worth avoiding — but it is not the
emergency the other two names are.

### Listener URLs

The chart separates two roles, and the distinction matters more under HA:

- **`listener.apiUrl`** — the address the listener and runner pods actually call.
  **Shared** for a DNS-following fleet; **per-node** for per-node fleets. Left
  empty it resolves to the in-cluster Service, which is correct for a per-node
  fleet and **wrong for a DNS-following one** — those listeners would keep
  talking to their local node after the name moved.
- **`listener.publicApiUrl`** — the canonical hostname users and `source = "…"`
  URLs see, which flows through to the runner's registry-discovery redirect.
  Always a **shared** name.

This is documented rather than enforced, because a per-node fleet setting
`apiUrl` to its own node is correct and indistinguishable from the mistake.

---

## Planned failover

The rehearsed path. Everything here is checkable before you touch DNS.

1. **Confirm the standby is caught up.** The HA page (`/ha`) shows replication
   age; the nav-bar indicator shows it on every page. Do not proceed on a
   follower that is behind.
2. **Confirm the object store.** `GET /api/terrapod/v1/ha/blob-readiness` —
   `irreplaceable-missing` must be empty. Read `irreplaceable-unchecked` in the
   same breath: it names any irreplaceable class the check made no claim about,
   which is what stops an empty `irreplaceable-missing` being mistaken for a
   verified store. *Rows without blobs is the failure that looks like success.*
3. **Quiesce the outgoing node** — set `ha.role: follower` on it and roll it. It
   stops originating writes, stops all scheduled work, and **retires its
   in-flight runs**: anything mid-plan or mid-apply ends `errored`, naming the
   role change as the reason. That last part matters more than it sounds. A run
   left sitting in `planning` would hold its workspace at the per-workspace
   serialization gate, and no operator could clear it — `workspace.locked` is
   untouched, so there is nothing to unlock; the block is the row itself.
   Retiring them means the workspace is usable on the incoming node straight
   away, and re-queueing is the recovery.
4. **Confirm it has stood down.** Its own `/ha` page reports its role. A write
   against it now returns 503.
5. **Move the shared names** — internal, external, and webhook.
6. **Wait for DNS caches to expire.** This, not the probe interval, governs the
   overlap.
7. **Confirm the incoming node has taken over.** On `ha.role: auto` both nodes
   converge on their own once the probe threshold is met; on explicit roles, set
   `ha.role: leader` on the incoming node and roll it.
8. **Re-point the listeners** if you run the shared-fleet topology — they
   re-join automatically; watch them reappear in the pool.

**RPO:** whatever replication lag you measured at step 1. **RTO:** dominated by
DNS TTL, not by Terrapod.

---

## Unplanned failover

The same, minus the step you cannot take.

1. Skip the quiesce — the outgoing node is gone. If it is merely *unreachable*
   rather than dead, that is fine: it cannot do damage, because a node that
   cannot see itself at the shared name stops leading on its own.
2. Check blob readiness on the **surviving** node (step 2 above). Under time
   pressure this is the check people skip and the one that decides whether the
   promotion is real.
3. Move the shared names, wait out DNS, confirm the survivor leads.

**What you lose: in-flight runs.** A run executing when its node went away is
gone — its Job was launched from a cluster the new leader does not drive. The
run's workspace is not corrupted, and re-queueing is the recovery. Anything that
had already reached a terminal state is safe.

There was no chance to quiesce, so those runs are still sitting on the node that
vanished. It resolves them itself the moment it comes back: a node that starts
in a different role from the one it last held retires them before it does
anything else, so it never carries a previous era's work into a new one.

**What you also lose: replication lag.** Writes the leader accepted but had not
yet been pulled are not on the survivor. This is the honest cost of asynchronous
replication and the reason step 1 of the planned procedure exists.

---

## Failback

Failback is a **planned failover in the other direction** — there is no separate
mechanism and no special mode, because roles are symmetric and the credentials
for both directions were configured up front.

1. Let the recovered node come up **as a follower**. It will backfill or catch
   up on its own, and the direction of replication is simply reversed.
2. Wait for it to be caught up — the same check as step 1 of a planned failover.
3. Run the planned failover procedure with the nodes swapped.

Do not rush step 2. A node that has been down long enough for its cursor to age
out will fall back to a full backfill, which is the designed recovery but takes
as long as it takes. Watch it rather than assuming.

---

## Taking the follower down for maintenance

This is the cheapest operation in the pair, and it is worth knowing how cheap.

**The follower pulls.** Node B asks node A for changes; A never pushes. So a
follower that is stopped is, from the leader's point of view, nothing happening
— no queue backs up, no write blocks, no timeout fires. Stop it, patch it, start
it.

On return it catches up from its cursor. If it was down long enough that the
events it missed have been purged, it falls back to a **full backfill**
automatically — that fallback is what makes a bounded event log safe, and it
needs no operator action.

The only thing to watch is that you do not fail over *to* it before it has
caught up, which is what step 1 of the planned procedure is for.

## Removing a node

1. Point nothing at it: confirm the shared names resolve elsewhere and no
   listener has it as `apiUrl`.
2. Remove the peer configuration **from the surviving node** — otherwise it keeps
   trying to reach a peer that will never answer, and its HA page will honestly
   report a peer it cannot see, which is noise rather than signal.
3. Uninstall the release and delete its namespace.

Its database and object store are its own; nothing on the surviving node
references them, and nothing needs cleaning up on the survivor beyond the peer
configuration.

## Adding a node

Not a runbook — it is a **feature**, and the interesting property is that it
needs no special handling: a new node has no deltas to catch up on, so it
**backfills from empty** automatically. Deploy it as a follower with the peer
configured and watch replication age fall. Adding a second node to a running
install is the same path as recovering one that aged out.

---

## Version skew between the pair

During a rolling upgrade the two nodes run different versions for a while, and
this is supported rather than merely tolerated:

- **Upgrade the follower first.** It is the node nothing is pointed at, so a
  problem there costs nothing, and you learn whether the release is good before
  it is serving anyone.
- **Replication is additive-safe across a skew.** A newer peer replicating a
  class an older node does not know is **skipped** rather than fatal, and a class
  registered in the newer version is backfilled once both sides know it — which
  is why a class added in a release moves no data until the receiving node
  understands it, and then moves all of it.
- **Do not fail over mid-upgrade** unless you have to. Promoting a node running
  the older version is safe, but you then have the newer one as follower, which
  is the reverse of the order above.

The same skew rules that govern runners and listeners against the API apply
here; see [Versioning & support](versioning-and-support.md).

---

## Rehearse the whole thing

```sh
scripts/ha-smoke.sh up      # two full nodes, each with its own Postgres and Redis
scripts/ha-smoke.sh table   # the release-gate table: PASS / FAIL / SKIP-with-reason
scripts/ha-smoke.sh down
```

The table walks planned failover, unplanned failover, fail-back, a peer outage,
catch-up inside retention, fallback to backfill past retention, and the follower
refusing writes through the real API path. It reports honestly, including the
rows it did not exercise and why — because a partial rehearsal reported as a
full one is worse than none.

## Related

- [High availability](high-availability.md) — roles, the peer link, replication,
  what a follower refuses to do
- [HA topologies](ha-topologies.md) — multi-region, multi-cloud, listener
  placement, multi-pool execution routing
- [Disaster recovery](disaster-recovery.md) — backups, restore, break-glass
  state recovery
- [Runbooks](runbooks.md) — operational procedures for the rest of the platform
