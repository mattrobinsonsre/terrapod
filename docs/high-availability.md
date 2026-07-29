# High availability

> **A single-node install does none of this.** With the shipped defaults —
> `ha.role: leader`, no `ha.peer.url` — Terrapod records no replication events,
> runs no replication tasks, and never probes. The tables exist and stay empty.
> This is asserted in the test suite, not just intended.
>
> **Status: in development.** Role resolution and the peer link are complete;
> settings replication is being built out class by class. The replication scope
> today covers **agent pools and join tokens** — see [Replication
> scope](#replication-scope) before planning around it. Everything on this page
> is off by default and inert on a single-node install.

Terrapod's high-availability model is a **leader/follower pair**, and the single
most important thing about it is what it does *not* do:

**Terrapod never fails over by itself.** Failover is a deliberate human act —
you move a DNS name. Terrapod does not vote, does not arbitrate, and has no
opinion about whether the other node is really dead. Automated failover between
two stateful control planes is the problem that has produced split-brain
incidents for decades, and the failure mode is worse than the outage it tries to
avoid.

What Terrapod does instead is *notice*. A node discovers its own role by asking
the shared name who it reaches: if the answer is itself, it owns the name and is
the leader. Move the DNS record and the roles swap on their own, with no
credential to rotate, no config to edit, and nothing to remember under pressure.

## Roles

`api.config.ha.role` is one of:

| Value | Meaning |
|---|---|
| `leader` | **The default.** This node writes. Never probes, never transitions. |
| `follower` | This node is passive: it accepts reads and replicates, but originates nothing. |
| `auto` | Derive the role by probing the shared name. |

**`leader` is the default deliberately.** Almost every install is a single node,
and it must never probe, never transition, and never spend a threshold's worth
of time passive after a pod restart. An `auto` default would be actively
dangerous: a single-node install with no probe URL would fail passive and go
inert.

Running both nodes on explicit `leader`/`follower` and setting them by hand at
cutover is a **supported mode, not a degraded one**. Role is configuration;
probing is one convenient way to derive it.

### How `auto` resolves

```yaml
api:
  config:
    ha:
      role: auto
      node_name: node-a          # must be unique across the pair
      probe_url:
        internal: https://terrapod.example.com
```

The node calls `GET /api/terrapod/v1/ha/whoami` against the probe URL and
compares the answer to its own `node_name`.

Two details matter:

- **Never point `probe_url` at an in-cluster Service address.** That always
  resolves to self, so the node would always declare itself the leader — both of
  them. Use the real shared name.
- `internal` is preferred over `external` because it avoids hairpin NAT and any
  CDN or WAF in front of the public name, either of which can cache or filter
  the probe.

Timing is minutes-scale on purpose (`probe_interval_seconds: 60`,
`probe_threshold: 3`). Nothing is racing to detect a failover, because a human
performed it — and a slow probe ignores the transient resolution blips a fast one
would chase. **This does not govern the cutover overlap**; DNS cache does, and it
is longer than the probe window anyway.

A node that cannot determine its role **fails passive** (follower). Not knowing
whether you are the leader is not a licence to act like one.

## What a follower will not do

A follower refuses to originate change:

- **Every mutating request is refused, by default.** `POST`, `PUT`, `PATCH` and
  `DELETE` return `503` on a follower unless the path is on a small allow-list
  (below). This is a single chokepoint rather than a guard per endpoint, so a
  surface added later is covered without anyone remembering to cover it.
- **Writes are refused at the service layer too** — creating, queueing,
  confirming, discarding, or transitioning a run, and handing work to a
  listener. The service-layer guards are not redundant: the scheduler and the
  triggered-task consumer write without an HTTP request anywhere in sight, and
  no request-level gate can see them.
- **Scheduled work does not run.** This is not merely an optimisation: letting
  `vcs_poll` run and fail at the last step would still burn the installation's
  VCS API quota every cycle, advance its own poll cursor, and record a spurious
  poll failure on every VCS-connected workspace.

Four tasks are exempt, all self-maintenance: the role probe (gating it would
make the role permanently sticky, so a follower could never discover it has been
promoted), encryption-key refresh (a follower that stops propagating rotated
keys cannot decrypt anything written afterwards, and finds out at promotion —
during the incident), and the two replication tasks.

**Refusing is kinder than allowing.** Replication flows leader → follower only,
so a change made on a follower is not a change that goes anywhere: the next
backfill reconciles against the leader and the row is silently reverted. The
operator would watch the write succeed and then watch it disappear. A `503` —
well-formed request, authorised caller, wrong node — says what is actually true.

### What a follower still accepts

Only what **records or reduces access on this node**, never what changes
platform state:

| Path | Why |
|---|---|
| `POST /auth/local/authorize`, `/auth/local/login`, `/auth/saml/acs`, `/auth/token` | An operator has to open the standby's UI and read its HA status *before* deciding to move DNS. Sessions live in this node's own Redis; the only Postgres side effect is `last_login_at` |
| `POST /auth/logout`, `/auth/logout/all`, `DELETE /auth/sessions/user/{email}` | Node-local, and they only ever remove access |

`POST /oauth/token` is deliberately **not** on that list. The `terraform login`
flow mints an API token, and API tokens are replicated — so a token minted on a
follower is erased at the next reconciliation. Handing out a credential that
later vanishes is worse than refusing it. Point `terraform login` at the shared
name.

On a single node none of this is reachable: `role: leader` is the default, the
check is a configuration read with no I/O, and it always passes.

## The peer link

The two nodes authenticate to each other with the OAuth 2.0
`client_credentials` grant. Each node registers a client representing its peer
and hands over those credentials; the peer exchanges them for a short-lived
token.

The issued identity is its **own class** — not a reuse of the runner or user
token paths. A peer is entitled to read things an ordinary user is not, and that
visibility must not be grantable to a person by accident. A peer token resolves
to no roles and is refused by every endpoint except the replication surface.

```yaml
api:
  config:
    ha:
      peer:
        url: https://node-b.example.com
        client_id: peer-b
        existingSecret: terrapod-peer     # the client secret, from a K8s Secret
      replication:
        enabled: true
```

The client secret is **never** set in `values.yaml`. It is injected as
`TERRAPOD_HA__PEER__CLIENT_SECRET` from a Kubernetes Secret, like every other
credential.

## Replication

**The follower pulls.** The leader records what changed and otherwise does
nothing. This is what makes "a peer outage must never block a healthy leader"
true by construction rather than by careful coding: a dead follower simply stops
asking, and the leader neither knows nor cares.

Asynchronous replication is only safe because a follower that falls behind can
converge again. Inside the retained event window it replays deltas; beyond it,
it **backfills** automatically. That fallback is what lets the leader bound
`retention_days` and discard old records freely, instead of choosing between
unbounded growth and blocking on a dead peer.

Backfill is also the *ordinary* path, not an error path: adding a second node to
a running install has no deltas at all.

A complete backfill also **removes rows the peer no longer has**. That is not
tidiness — without it a deletion can never converge through the recovery path,
and the concrete failure is a revoked API token surviving on the follower and
working again after a failover. It is safe because everything replicated
originates on the leader and a follower originates nothing, so a row the
follower holds and the leader does not is a row the leader deleted.

It only happens on a complete, error-free pass, and every removal is logged. A
node that has just stopped being the leader may hold writes from the moments
before cutover that never replicated; those are lost either way under
asynchronous replication, but the log is what lets an operator see exactly what
was discarded to reach convergence.

**Encryption is per-node.** Values are decrypted by the sender, cross the
authenticated peer link, and are re-encrypted under the receiving node's own
key. Neither node holds the other's key.

**There is no conflict resolution, deliberately.** Only the leader writes, so
the peer's row is authoritative — applying it is the whole rule. Per-field merge
semantics would only be needed in an active-active design, which this is not.

### Replication scope

| Class | Status |
|---|---|
| Agent pools, join tokens | Replicated |
| Users, roles, role assignments, platform role assignments, API tokens | Replicated |
| Workspaces, variables, VCS connections, policy sets, registry, the CA | In development |

Identity comes first deliberately. A node holding the whole estate but no users,
roles or assignments has nobody able to touch it — and API tokens are the class
where a missed deletion is a revoked credential that starts working again.

Runs and state versions are a separate phase.

Each class carries a full test matrix — backfill from empty, delta apply,
idempotent re-apply, delete, deletion converging through a backfill, plus an
encrypted-column row where the class has one. The required rows are derived from
the class definition rather than declared, and CI fails until they exist. A class
that is registered but not tested does not converge, and the symptom appears at a
failover rather than in a build.

## Join tokens and the two listener topologies

Both listener topologies work, and neither needs anything beyond a shared CA —
which the pair has, because the follower adopts the leader's.

**Per-node fleets.** Each node has its own listeners, pointed at its own name.
Nothing ever crosses the boundary.

**One shared fleet following the shared DNS name.** At cutover the fleet
re-points at the promoted node. The old certificates stop authenticating, each
listener re-joins, and it heals itself — because join tokens replicate, so the
promoted node accepts the token the fleet already holds.

> **Size join tokens for the shared-fleet topology accordingly: long-lived, and
> generously reusable.**
>
> Every failover costs **one use per listener**. A token with a tight `max_uses`
> sized for the initial rollout, or a short expiry, is therefore exhausted by the
> first real failover — at precisely the moment nobody wants to be minting
> credentials. Prefer a long expiry and a high or unlimited `max_uses` here.
>
> The per-node topology never crosses a boundary and does not have this
> constraint.

`use_count` replicates like any other column. There is no merge rule and none
is needed: only the leader writes, so its count is authoritative and the
follower simply takes it.

## Is the follower caught up?

`GET /api/terrapod/v1/ha/status` (admin or audit), `terrapod_ha_status` over MCP,
or `GetHAStatus` in go-terrapod. Answered entirely from local state, so it still
works when the peer is the thing that has broken — which is when you are reading
it.

On the **follower**, `in-sync` is the summary. It is false unless a pull has
succeeded *and* no class is mid-backfill: **a node backfilling is not in sync
however recent its last cycle**, and reading only the timestamp would give a
green light to a node still pulling a whole class.

On the **leader**, compare `oldest-event-age-seconds` against
`retention-seconds`. As those converge the follower is close to falling off the
end of the retained event window and having to backfill from scratch. That is
the early warning — it costs nothing to watch and it precedes the problem by
days.

There is deliberately no "N events behind". That needs the peer's latest event
id, and seconds-since-the-last-successful-pull is the more honest number: a pull
that returned nothing means caught up *as of then*, which is the question being
asked.

## The other half: is any component a single point of failure?

A pair that replicates flawlessly is still not highly available if it is serving
from one API pod. The same status endpoint reports in-cluster readiness:

| Field | Meaning |
|---|---|
| `components` | Ready **and desired** replicas per component. Desired comes from the Deployment — without it, `1` cannot be told apart from `1 of 3, mid-incident` |
| `single-replica-components` | Components on exactly one ready replica, named directly |
| `ha-findings` | Specific, named gaps — see below. An **empty list means nothing avoidable was found**, not that nothing was checked |
| `components-unavailable-reason` | Set when the API could not read its namespace |

**API and web come from Kubernetes; listeners do not.** A listener may be in a
different cluster entirely — that is the point of the design — so its replica
count comes from the Redis heartbeats that already cross that boundary.
Reporting a listener as absent because it is merely elsewhere would be worse
than not reporting it.

This needs a namespace-scoped Role (`pods` and `apps/deployments`, read-only),
created by the chart alongside `api.config.ha.component_status.enabled`.
**Declining it is supported**: set that to `false` and both the Role and the
sampling go away, and the endpoint reports `unknown` rather than failing. An
empty `components` with a reason set means *"I cannot see"*, never *"nothing is
running"*.

It reports **readiness, not a verdict**. "2 of 3 API replicas ready" is a fact;
"HA is configured correctly" is not — so the endpoint never claims it. What it
does claim is specific, named gaps.

### Findings: disruption budgets and placement

Replica counts alone miss the two ways a well-replicated component still dies as
one: a node drain that evicts every replica at once, and every replica sitting on
one node or in one zone. Both are reported as `ha-findings`:

| `kind` | What it means |
|---|---|
| `no-pdb` | The component has several replicas and no PodDisruptionBudget, so a node drain can take all of them together |
| `pdb-blocks-eviction` | The *opposite* trap, and the easier one to miss: a PDB that permits **no** voluntary eviction. Nothing is evicted, so a node drain stalls instead of proceeding — a cluster upgrade hangs on it |
| `node-concentration` | Every replica is on one node, **and more than one node was available** |
| `zone-concentration` | Every replica is in one zone, **and more than one zone was available** |
| `single-zone-cluster` | Every node reports the same availability zone. Cluster-level, not a component's fault — no placement of replicas survives losing that zone |

**The qualifiers are the point.** A finding is raised only where the cluster
could have done better. A single-node k3s or kind cluster puts every replica on
one node necessarily; an on-prem cluster with no zone labels cannot spread across
zones. Neither produces a finding, because neither is a mistake anyone made. A
readout that cries wolf on a laptop cluster is one an operator learns to ignore,
which costs more than reporting nothing at all.

That distinction — avoidable versus inevitable — is the only thing that needs
cluster scope, because node and zone labels live on `Node` objects. The chart
therefore also creates a **read-only ClusterRole** for `nodes` (`get`, `list`),
on by default under `api.config.ha.component_status.read_nodes`. It is among the
least sensitive cluster-scoped reads there is: node names, labels and conditions,
no secrets and no workloads from other namespaces.

**Declining it is supported.** Set `read_nodes: false` and the ClusterRole is not
created. Placement is still reported — node concentration is still found wherever
Terrapod's *own* pods prove more than one node exists — and only zone spread goes
unknown. What is never done is guess: with nothing to prove spread was possible,
no finding is raised.

## Performing a failover

1. Confirm the standby is caught up (above).
2. Move the DNS record to the standby.
3. Wait for DNS cache to expire — this, not the probe interval, is what governs
   the overlap.
4. On `auto`, both nodes converge on their own once the probe threshold is met.
   On explicit roles, set `ha.role` on each node and roll them.

There is no step where Terrapod decides anything. That is the point.

## Related

- [Disaster recovery](disaster-recovery.md)
- [Agent pools and runners](runners.md)
- [Encryption at rest](encryption-at-rest.md)
- [Production checklist](production-checklist.md)
