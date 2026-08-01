# High availability

> **A single-node install does none of this.** With the shipped defaults —
> `ha.role: leader`, no `ha.peer.url` — Terrapod records no replication events,
> runs no replication tasks, and never probes. The tables exist and stay empty.
> This is asserted in the test suite, not just intended.
>
> **Status: in development.** Role resolution, the peer link and **settings
> replication** are complete — everything a workspace's runs depend on carries.
> Object-store content has a per-class policy (verify or copy); run and artifact
> *history* is a later phase. See [Replication scope](#replication-scope) before
> planning around it. Everything on this page is off by default and inert on a
> single-node install.

> **Looking for layouts rather than mechanism?** [HA topologies](ha-topologies.md)
> covers multi-region, multi-cloud and air-gapped pairs, dedicated versus shared
> listener fleets, and how multi-pool execution routing composes with the pair.

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

### Two credentials, not one

Each node has **two**, and they are treated differently because their ownership
differs:

| | What it is | Where it lives |
|---|---|---|
| **Outbound** | The client id + secret this node **presents** when it pulls | `ha.peer.client_id` + a Secret. A credential it holds but does not own, so there is nothing to persist — the same treatment an SSO client secret gets |
| **Inbound** | The client this node **accepts** when its peer pulls from it | `ha.peer.inbound.*` + a Secret. Also not persisted — the running config *is* the accepted credential, compared in constant time on each token request |

The follower pulls, so node B presents a credential that node A accepts: one
secret configured on A as inbound, the same secret held by B as outbound.

### Both directions, always

`role: auto` swaps the roles when DNS moves — that is the entire point — so
**every node carries both halves from the start**. Configure only one direction
and the pair works perfectly right up until you fail over, at which moment the
new follower has no outbound credential and the new leader no inbound row, and
replication stops silently at exactly the wrong time.

So: **two secrets, each held by both nodes.**

| Secret | On node A | On node B |
|---|---|---|
| `secret-ab` | outbound — what A presents | inbound — what B accepts |
| `secret-ba` | inbound — what A accepts | outbound — what B presents |

### Setting it up

Generate both secrets (`openssl rand -base64 32`), put each in a Kubernetes
Secret on both nodes, and reference them:

```yaml
# Node A
api:
  config:
    ha:
      role: auto
      node_name: node-a
      probe_url:
        internal: https://terrapod.example.com   # the SHARED name
      peer:
        url: https://node-b.example.com          # B's DIRECT address
        client_id: a-to-b
        existingSecret: secret-ab                # presented when A follows
        inbound:
          client_id: b-to-a
          name: "Node B"
          existingSecret: secret-ba              # accepted when A leads
```

Node B is the mirror image: `peer.url` points at A's direct address, its
outbound is `b-to-a`/`secret-ba`, its inbound `a-to-b`/`secret-ab`.

> **`peer.url` must be the peer's direct per-node address, never the shared
> name.** The shared name resolves to whichever node currently leads — which may
> be this one, in which case a node would try to replicate from itself. Same
> trap as `probe_url`, opposite direction: the probe wants the shared name, the
> peer link wants the specific one.

That is the whole setup. No CLI, no UI, no manual step, and nothing to remember
at cutover — the credentials for the reversed direction are already in place, so
moving DNS is genuinely the only act.

**Rotating** is editing the Secret and restarting. The new value takes effect
immediately and the previous one stops working, because there is nothing stored
to fall out of step with it.

**Tearing down** is removing the config. There is no credential anywhere else —
no row, no minted secret, nothing left behind to forget about — so withdrawing
the configuration genuinely withdraws the capability. No peer token is issued
and none is accepted.

There is deliberately **no CLI and no admin UI** for any of this. The credential
is two strings in a values file; a second way to set it could only disagree with
the first.


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
| VCS connections | Replicated |
| Registry modules, provider templates, catalog items, autodiscovery rules | Replicated |
| Workspaces, their agent-pool set, their module links | Replicated |
| Variables and variable sets | Replicated |
| Policy sets, notifications, run tasks, execution hooks, run triggers, remote-state consumers | Replicated |
| Object-store content (state, registry, caches, logs) | Per-class policy — see [Choosing verify or copy](#choosing-verify-or-copy-per-class) |
| The CA, the encryption keys | **Never** — node-local by design (see below) |

**Settings replication is now complete**: everything a workspace's runs depend on
carries. What remains is run and artifact *history* — a separate phase. The
objects those rows name are a different question with its own answer, below.

Three of the classes above are **gates**, and for a gate the failure mode of
partial replication is not an error but a silently weaker posture: a mandatory
policy set that lost its enforcement level is an advisory note, a mandatory run
task likewise, and the remote-state consumer list is access control whose
*deletions* matter as much as its rows. Each of those has its own test rather
than relying on "the row arrived".

Identity comes first deliberately. A node holding the whole estate but no users,
roles or assignments has nobody able to touch it — and API tokens are the class
where a missed deletion is a revoked credential that starts working again.

Nothing is excluded from a workspace, which is a decision rather than a
shortcut. `vcs_last_commit_sha` is the one that matters most: it is the poller
cursor, and a promoted node that has not seen it treats every tracked branch as
changed — queueing a plan *and apply* on every VCS-connected workspace at once.
Likewise a held state lock, `state_diverged`, and a `pending_deletion` lifecycle
state all carry, because losing any of them is worse than carrying it stale.

Variables matter for a blunter reason: a promoted node with workspaces but no
variables is terraform with no inputs and no credentials, so every run fails at
plan. The subtle half is precedence — *priority set → workspace variable →
non-priority set*. A node that carries every value but disagrees about `priority`
hands a run a **different** value than the leader would, and nothing reports it.

### Private keys never travel

Two classes are deliberately outside the replication scope, for the same reason.

**`crypto_keys`** holds this node's data-encryption key wrapped by *this node's*
KEK. It is meaningless to the peer, and putting it on the link would leak key
material for no benefit — which is the whole point of decrypting on send and
re-encrypting under the receiver's own key.

**The CA** (#1143) is node-local by decision, not omission. It signs only
listener certificates, and those are short-lived and auto-renewed, so no
artifact's long-term verifiability depends on the pair sharing a CA. A failover
rebuilds the trust chain by re-joining (above), so replicating the CA would put a
private key on the peer link and resident on both nodes while buying nothing the
re-join path does not already deliver.

The one real consequence: every listener re-joins at a failover, spending one
join-token use each. That is exactly why join tokens for a shared fleet should be
long-lived and generously reusable.

A test fails if either class is ever registered — and another fails if the CA
gains a new issuing method, since "everything it signs is short-lived" is the
premise the decision rests on.

One field is knowingly imperfect: `drift_latest_run_id` points at a run, runs are
a later phase, and the column is deliberately not a foreign key (so artifact
retention cannot cascade into workspace deletion). On a promoted node the drift
badge can therefore link to a run that is not there. The UI handles the 404.

VCS connections come earlier because withholding them fails *quietly*: the
promotion looks successful, and then every VCS-connected workspace simply stops
seeing pushes and pull requests, because the poller on the promoted node has no
credentials. They are also what workspaces, autodiscovery rules and the registry
all hold a foreign key to, so nothing further can replicate before them.

They are the first class carrying credentials, which is where the per-node
encryption design earns its keep: an encrypted column is read through the ORM
already decrypted, crosses the authenticated peer link, and is re-encrypted
under the **receiving** node's own key. Neither node holds the other's key,
which is what lets a pair span two clouds or two KMS tenancies.

Runs and state versions are a separate phase.

Each class carries a full test matrix — backfill from empty, delta apply,
idempotent re-apply, delete, deletion converging through a backfill, plus an
encrypted-column row where the class has one. The required rows are derived from
the class definition rather than declared, and CI fails until they exist. A class
that is registered but not tested does not converge, and the symptom appears at a
failover rather than in a build.

## Join tokens and the two listener topologies

Both listener topologies work, and **neither needs a shared CA**. Each node
generates and keeps its own; `certificate_authority` is deliberately not
replicated (#1143). The shared-fleet topology below heals by re-joining rather
than by reusing certificates, so the trust chain is rebuilt at the failover
instead of carried through it.

**Per-node fleets.** Each node has its own listeners, pointed at its own name.
Nothing ever crosses the boundary.

**One shared fleet following the shared DNS name.** At cutover the fleet
re-points at the promoted node. The old certificates stop authenticating, each
listener re-joins, and it heals itself — because join tokens replicate, so the
promoted node accepts the token the fleet already holds. This is what makes the
CA question above a design choice rather than a blocker: the trust chain is
rebuilt at the failover, not carried through it.

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

## Reading all of this in the UI

The **HA indicator in the nav bar** is on every page, for every signed-in user,
and clicking it opens the HA page (`/ha`). Which node you are talking to, and
whether it is converging, is context rather than an administrative task — and
the person whose next write is about to be refused by a follower is precisely
the person who needs it.

The page shows the node's role, whether it is converging with its peer, and
in-cluster readiness. That last part — ready-vs-desired per component, node and
zone concentration, the disruption-budget findings — describes the *deployment*
rather than this node's own posture, so it stays `admin`/`audit`. Everyone else
is told plainly that it is withheld, rather than shown an empty list that would
read as "nothing is running".

It is **read-only on purpose**. There is no promote button, no failover button.
A failover is moving DNS (see [Performing a failover](#performing-a-failover));
a control that looked like it could do it from here would be actively dangerous,
because the one thing this page must never do is invite an action that leaves
two leaders.

Two things it is careful about, because they are the ways a status screen
misleads:

- A **follower** is stated plainly, with what it means — a node that replicates
  and originates nothing. Reading the wrong node's UI as the leader is the
  mistake worth designing against.
- A class still backfilling shows **not in sync** even beside a fresh
  timestamp, matching the rule below.

It also shows **runner readiness**: the agent pools you can see, how many live
listeners each has, and any pool with none called out by name. That duplicates
the agent-pools page on purpose — "can this node actually run anything" is part
of the failover decision, and a node that replicates flawlessly with no listener
anywhere cannot execute a single plan. The count comes from the same predicate
as the pool's online/offline status, so the two can never disagree: a listener
that heartbeats with an expired certificate 401s every authenticated call, and
counting it would report capacity that does not exist.

Object-store readiness is **not** on this page. It makes real requests to the
store, which is why it is a deliberate command rather than something a page load
triggers — run it from the API, MCP or go-terrapod when you want it (see [The
other data plane](#the-other-data-plane-is-the-object-store-actually-there)).

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

### How far behind, concretely

`events-behind` and `behind-seconds` answer the question the sync timestamp
cannot: not "when did it last work" but **how much is outstanding**.

The follower cannot work this out alone — its page from the leader is capped at
the batch size, so a full page means "there is more" and nothing about how much
more. So the leader says: every events response now carries its newest event id
and the timestamp of the oldest event it is handing back, and the follower keeps
both alongside its cursor.

Two properties to read them by:

- **Null is not zero.** A node that has never pulled, or whose peer runs older
  code, reports `null` — *unknown*. Zero means *caught up*. An operator reads a
  zero as "fine" and would be right only half the time if the two were
  conflated.
- **They describe the last successful pull, not this instant.** That is
  deliberate: the status endpoint answers from local state so it still works
  when the peer is the thing that has broken, which is when it is read. Pair
  them with seconds-since-last-pull to know how old the answer is.

A node mid-backfill is still **not in sync** whatever these say — the rule above
is unchanged, and it takes precedence.

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

## The other data plane: is the object store actually there?

Replication carries database rows. State files, configuration tarballs, module
and provider artifacts live in the **object store**, which is a second data plane
— and the dangerous state a pair can reach is **rows present, blobs absent**.

A node in that state looks entirely healthy. The workspace list renders, run
history is there, the registry lists every module. Then somebody queues a run and
`terraform init` 404s, several layers from the cause.

`GET /api/terrapod/v1/ha/blob-readiness` detects it cheaply, because the database
row already names the key — so the check is a HEAD:

| Field | Meaning |
|---|---|
| `irreplaceable-missing` | **The list that should stop a failover.** Classes whose absence is permanent: state, the recovery index, configuration versions, module and provider artifacts |
| `irreplaceable-unchecked` | The counterpart that makes the list above trustworthy. A class that is switched off, or that no row guarantees, produces zero missing objects — indistinguishable from a pass unless it is named |
| `classes[]` | Per class: `tier`, `mode`, `verifiable`, `note`, `total-rows`, `checked`, `missing`, a few `missing-examples`, and `complete` |
| `sampled` | Whether this checked everything or a sample |
| `unavailable-reason` | Set when the store could not be read at all — *"I could not look"*, not *"all is well"* |

**A sample is reported as a sample.** By default each class is sampled to its 25
newest rows, and `checked` against `total-rows` says how much of the class the
answer covers. `complete` is true only when nothing was held back. Reading a clean
sample as a clean estate is precisely the false confidence the check exists to
remove, so the response is built to make that misreading impossible rather than
merely discouraged. `?full=true` verifies everything — thousands of round trips on
a real estate, which is why it is opt-in.

**Worth running whoever does the replicating.** Under provider-native bucket
replication it catches a misconfigured prefix, or a lifecycle rule that expired
objects out from under the rows. Under Terrapod-side copying it catches a class
that never ran. It is the same instinct as the restore-verification DR drill: a
real check beats a documented intention.

**Two things a class can be, and the difference is stated rather than hidden.** A
class is *verifiable* only when a database row **guarantees** the object exists —
that promise is what makes an absent object a finding rather than a guess. Where no
row makes it (a run writes its logs only once it reaches the phase that produces
them; a pull-through cache holds whatever it happens to hold), presence cannot be
derived from the database. Those classes are still listed, with `verifiable: false`
and the reason in `note`, because a class quietly left out of a clean report reads
as a class that passed.

### Choosing verify or copy, per class

The object store often already has a replicator — provider-native cross-region
replication, a storage-level mirror. Where that is true, Terrapod's job is to
**prove it happened**, not duplicate it. Where it is not — cross-CSP, on-prem,
air-gapped — Terrapod copies.

That choice is made **per class**, because within one deployment it genuinely
differs: copying state and configuration versions across a region boundary while
letting a re-warmable provider cache stay cold is a coherent position that no
bucket-level replication policy can express.

```yaml
api:
  config:
    ha:
      blobs:
        # off | verify | copy — the default for every class.
        mode: verify
        classes:
          state: copy
          configuration_versions: copy
          registry_providers: copy
          run_logs: "off"
```

| Mode | What Terrapod does |
|---|---|
| `off` | Neither checks nor copies. Reported as skipped, never quietly dropped |
| `verify` | Checks presence; does not copy. **The default** |
| `copy` | Copies to the peer, and verifies |

The default is `verify` everywhere: it observes the whole store, costs nothing
until this endpoint is called, and commits you to nothing.

The classes, in the order a report lists them:

| Tier | Classes |
|---|---|
| Irreplaceable | `state`, `state_index`, `configuration_versions`, `registry_modules`, `registry_providers` |
| History | `run_logs`, `run_plans`, `run_vars` |
| Re-derivable | `provider_cache`, `binary_cache`, `platform_provider_cache`, `cost_pricesheet`, `vcs_archives`, `module_overrides` |

An unknown class name is **rejected** — by `helm lint` before install, and again at
startup for the paths the chart never sees. A typo that silently left a class on
the default would be the worst outcome available.

**The tier is a property of the deployment as much as the artifact, and Terrapod
derives it rather than asking you to remember it.** A cold provider cache re-warms
itself on first use — unless the node is sealed (`registry.cache_only`), where
reaching upstream is exactly what is forbidden, so a promoted node with a cold cache
has no terraform binary and no providers and can never run anything again. On a
sealed node the four upstream-fed cache classes are therefore reported as
`irreplaceable`. `vcs_archives` and `module_overrides` are not: `cache_only` seals
upstream *registries*, not your own git, so those genuinely do re-derive.

Sealing escalates the **tier**, never the **mode**. The tier is a fact about the
deployment; whether to spend bandwidth copying is a decision about your topology,
and Terrapod does not make it for you.

### What copying actually does

For each class set to `copy`, the follower asks the leader what it has, checks its
own store, and streams across what is missing. Same direction as settings
replication: **the follower pulls**, so a follower that is down, slow, or
throttled simply stops asking.

**Irreplaceable classes go first.** A cycle that runs out of its byte budget runs
out of it having copied state and configuration versions, not run logs.

**It is resumable because it is diff-driven.** Each key is checked against this
node's store and skipped if present, so a cycle that dies halfway leaves the
copied objects copied and the next cycle re-diffs. There is no cursor to corrupt.
An object that arrives at the wrong size is deleted rather than left where the
next `exists()` would call it present — a partial object would otherwise outlive
every retry.

**It is streamed** (these are the large objects), and one unreadable object is
counted and skipped rather than abandoning the class it was in.

### Bandwidth

Copying a registry or an estate's state history is not free, so it is measured,
capped, and reported:

```yaml
api:
  config:
    ha:
      blobs:
        interval_seconds: 300          # objects are immutable; nothing to converge on quickly
        concurrency: 4                 # shares the peer link with a leader serving users
        max_bytes_per_second: 25000000 # 0 disables the throttle
        max_bytes_per_cycle: 0         # 0 = no per-cycle cap
```

| Metric | What it says |
|---|---|
| `terrapod_blob_copy_objects_total{blob_class}` | Objects copied, per class |
| `terrapod_blob_copy_bytes_total{blob_class}` | Bytes copied, per class — "40 GB" and "40 GB of provider cache while state fell behind" are different situations |
| `terrapod_blob_copy_failures_total{blob_class}` | Objects that could not be copied. A few are expected (deleted between listing and fetch); a rising count is not |
| `terrapod_blob_copy_classes_stopped_early` | **Classes whose last cycle did not finish.** Non-zero means the copy is behind, however many bytes moved |

That last one is the point of the section. A cycle that stops at its budget has
copied a great deal *and* is not finished, and only one of those shows up in a
byte count — so it is named, in the metric and in the cycle's log line. A silent
cap would read as "finished", which is exactly the false confidence the whole
phase exists to remove.

The copier is registered as a scheduled task **only** when at least one class is
set to `copy` and a peer URL is configured. On a default install it does not
exist.

## Performing a failover

The full procedures — including failback, maintenance, adding and removing a
node, and the naming model you need right before you touch DNS — are in
[HA operations](ha-operations.md). The short form:

1. Confirm the standby is caught up (above).
2. Check `GET /api/terrapod/v1/ha/blob-readiness` — `irreplaceable-missing` must
   be empty. Rows without blobs is the failure that looks like success. Read
   `irreplaceable-unchecked` in the same breath: it names any irreplaceable class
   the check made no claim about, which is what stops an empty
   `irreplaceable-missing` being mistaken for a verified store.
3. Move the DNS record to the standby.
4. Wait for DNS cache to expire — this, not the probe interval, is what governs
   the overlap.
5. On `auto`, both nodes converge on their own once the probe threshold is met.
   On explicit roles, set `ha.role` on each node and roll them.

There is no step where Terrapod decides anything. That is the point.

## Related

- [HA topologies](ha-topologies.md) — multi-region, multi-cloud, listener
  placement, and multi-pool execution routing
- [HA operations](ha-operations.md) — the naming model, failover, failback,
  maintenance, and version skew

- [Disaster recovery](disaster-recovery.md)
- [Agent pools and runners](runners.md)
- [Encryption at rest](encryption-at-rest.md)
- [Production checklist](production-checklist.md)