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

- **Writes are refused at the service layer** — creating, queueing, confirming,
  discarding, or transitioning a run, and handing work to a listener.
- **Scheduled work does not run.** This is not merely an optimisation: letting
  `vcs_poll` run and fail at the last step would still burn the installation's
  VCS API quota every cycle, advance its own poll cursor, and record a spurious
  poll failure on every VCS-connected workspace.

Four tasks are exempt, all self-maintenance: the role probe (gating it would
make the role permanently sticky, so a follower could never discover it has been
promoted), encryption-key refresh (a follower that stops propagating rotated
keys cannot decrypt anything written afterwards, and finds out at promotion —
during the incident), and the two replication tasks.

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
idempotent re-apply, delete, role-change conflict, plus a monotonic-merge or
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

`use_count` replicates, and it replicates **monotonically** — the larger value
always wins, in both directions and during backfill. Both nodes spend the same
budget (joins land on A before a failover and on B after one), so a stale copy
winning on timestamp would hand a spent token its uses back. Revocation is
one-way for the same reason: a revoked token can never replicate back to usable.

## Performing a failover

1. Confirm the standby is caught up.
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
