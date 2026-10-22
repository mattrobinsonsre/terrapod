# Running a Configure

A [configure operation](runners.md#what-a-configure-actually-runs) runs the
workspace's playbook against the hosts in its
[inventory](ansible-inventory.md). This page is about how one **starts**.

A configure is an ordinary **run** carrying a flag, not a second kind of
object — so it has a run id, a log, a status, notifications, an audit trail and
a cancel button, exactly as a plan or an apply does. `applied` means applied
**and** configured.

Four routes start one. The manual route needs only the operation declared; every
automatic one is off until you turn it on:

| Route | What triggers it | What it runs |
|---|---|---|
| **Manual** | the Configure button on the workspace's Runs tab, `POST /api/v1/runs` with `is-configure: true`, or the `terrapod_run_create` MCP tool | the workspace's `default-playbook` against its `default-limit`, or whatever the invocation overrides |
| **On push** (`auto-configure-enabled`) | a commit that changes a file under the workspace's `config-directory` | the declared defaults — a push carries no run options |
| **After an apply** (`configure-after-apply`) | every apply on this workspace, on the **same run** | the declared defaults |
| **On an inventory change** (the same `auto-configure-enabled`) | any change to the declared inventory that did **not** arrive through git — a host, group, membership or variable written through the API, the MCP tools, the UI, or by a Terraform apply declaring `terrapod_inventory_*` rows | the declared defaults |

There is deliberately **no scheduled configure**. An operator who wants a
periodic one drives `POST /api/v1/runs` from their own scheduler — anything
that can make an HTTP request on a timer — rather than Terrapod growing a
second scheduling vocabulary beside drift detection's.

## Turning it on

```hcl
resource "terrapod_workspace" "fleet" {
  name              = "fleet"
  configure_enabled = true          # the workspace HAS a configure operation
  config_directory  = "ansible"     # where its ansible.cfg and playbooks live
  default_playbook  = "site.yml"
  default_limit     = "canary"      # ← the blast radius, and see below

  auto_configure_enabled = true     # a push to ansible/ configures
  configure_after_apply  = false    # an apply does not
}
```

`configure_enabled` is the declaration; the other two are triggers and mean
nothing without it. All three default to **false** for an existing workspace,
so upgrading changes nothing about what runs.

## An inventory change configures, and the debounce is a settle

The on-push trigger watches `config-directory`, so it sees a playbook or an
inventory **file** change. An inventory change written any other way — the API,
the MCP tools, the UI, or a Terraform apply declaring `terrapod_inventory_*`
rows — fires the same trigger, under the same `auto-configure-enabled` flag.
There is no second switch.

**Any change counts, including a deletion.** The signal is an equality token over
the declared rows: a count *and* a latest-modification time per table. The count
is what makes a removal visible, so taking a host out of a group fires a
configure — which matters, because every pattern naming that group now resolves
to a different set.

**A burst becomes one run.** A single `for_each` over fifty instances writes
fifty hosts, fifty host variables and fifty memberships. A configure is queued
only once the inventory has been **unchanged for a full cycle**, so that apply
produces one run rather than one per cycle while it is still writing. The cycle
is 60 seconds, which is therefore both the longest a change waits and the
shortest gap between two consecutive auto-configures.

**Enabling it does not configure anything.** The first cycle after you turn it on
records the inventory as it stands and queues nothing; a configure follows the
first change *after* that. So switching the flag on for a fleet that already has
hosts is safe — it is not a licence for an immediate estate-wide run.

**It runs the declared defaults, like every other automatic route** — the
workspace's `default-playbook` against its `default-limit`. Narrowing a derived
configure to just the hosts a change actually affected is a separate question,
tracked in #2229: this trigger knows that *something* changed, never *what*, so
narrowing needs a comparison of two resolved inventories rather than a token.

A **locked** workspace is skipped, and a configure is never
queued behind one that is already on its way — the change is simply picked up by
a later cycle rather than dropped.

### With `configure-after-apply` on the same workspace, one apply configures twice

If a workspace has **both** triggers and an apply writes inventory rows — a
`for_each` over `terrapod_inventory_host`, say — it gets two configures: the one
chained to that apply, and another when the next cycle notices the inventory
moved.

**The second run is now redundant rather than load-bearing.** It used not to be:
a composite resolved its inventory when the run was created, before its own
plan, so the configure chained to an apply could not see the hosts that apply
had just created, and the cycle's run was what actually configured them. A
composite now re-resolves at the hand-over from its apply to its configure, so
the chained configure targets its own apply's hosts and the second run repeats
work rather than completing it.

Suppressing it wants #2229, which can tell whether the configure that just ran
already covered the hosts that moved — something this trigger cannot know,
because its signal says *something* changed and never *what*. Until then, a
workspace with both triggers configures twice for one apply; ansible is
idempotent, so the cost is time rather than correctness, and picking one trigger
avoids it.

## `default-limit` is the blast radius, and empty means everything

Ansible's `--limit` is what narrows a configure, the way `-target` narrows a
plan. A triggered configure has no invocation to carry one, so it uses the
workspace's `default-limit` — and **an empty `default-limit` means every host
in the inventory**.

That is deliberate rather than an oversight: at scale the hazard is hosts left
*unconfigured* because somebody has to remember to run a playbook, not hosts
getting configured. So the controls are **visibility** and **reversibility**
rather than prevention:

- **See what it would target before it runs.** `GET
  /workspaces/{id}/inventory/resolved?limit=<pattern>` is ansible's own
  expansion of the pattern, nesting and `~regex` included — not Terrapod's
  guess at it. The Inventory tab's Resolved view is the same answer.
- **Stop one in flight.** A configure can be cancelled from `configuring` like
  an apply from `applying`: the Job is deleted and the run's terminal status
  comes from what the Job actually did, because a playbook that was part-way
  through a fleet has already changed the hosts it reached.

A workspace whose `default-limit` names a canary group **cannot be made to fire
wider by a git push**, because a push carries no run options. If you want an
estate-wide auto-configure, leave the limit empty on purpose.

## A configure the platform derived targets only the hosts that changed

A configure **you** queue runs against the limit you gave it, or the workspace's
`default-limit`. A configure **the platform derived** — one an inventory change
produced, or one chained onto an apply — is narrowed to the hosts whose resolved
configuration actually moved.

The condition is the thing that justifies breadth. New playbook or role code has
to reach every host; a changed *host* is no reason at all to touch the others.
So a derived configure narrows when the Ansible code under `config-directory` is
unchanged since the last successful configure, and goes wide when it moved.

**`default-limit` is still the outer bound.** The narrowed set is intersected
with it, so a workspace whose limit names a canary group can never be made to
reach beyond that group by this — only to reach less of it.

### What counts as changed, and why the diff is over the resolved inventory

The comparison is between the **resolved** inventory this run targets and the
one the last successful configure targeted — ansible's own output, not the rows
Terrapod stores. That matters because the resolved form has already attributed
every value to every host: a host variable, a group variable and a
`group_vars/all` variable all come out as "these hosts differ", with no group
DAG for Terrapod to walk and no second implementation of ansible's precedence.
A change under `all` therefore returns every host and the run is not narrowed at
all, which is correct and needs no special case.

A host counts as changed when its effective variables moved, when its group
membership moved, or when it is new. Group membership is included because moving
a host into a group that carries no variables is an ordinary way to bring it into
scope — what reaches it is `hosts: webservers`, not a variable.

### When it goes wide anyway

| reason | why |
|---|---|
| `configure-all-hosts` is set | the operator's override |
| there is no baseline | the first configure, or the last one failed — hosts may be unconfigured for reasons no diff can see |
| the Ansible code under `config-directory` changed | new code has to reach every host |
| the group *nesting* changed | a `children` edit changes what a pattern resolves to without necessarily changing any host's own membership |
| nothing changed | a `--limit` matching nothing exits 1, so a run with nothing to do runs the workspace's default rather than failing |

Every one of those falls back to the workspace's `default-limit`, so the failure
direction is always "configure more", never less.

### The override

`configure-all-hosts` on the workspace makes every derived configure sweep the
whole inventory. It is settable on the workspace, in bulk, and on an
autodiscovery rule, so a fleet can declare it once.

It **widens**, deliberately: a flag someone forgets leaves them configuring
more, not less.

## A destroy never configures

An apply that is tearing infrastructure down is never followed by a configure,
whatever `configure-after-apply` says: the hosts a configure would reach are
the ones the destroy has just removed.

## Why "after an apply" is a workspace setting and not a run option

Because the `terraform`/`tofu` CLI has no way to ask for one. An operator
running `tofu apply` against Terrapod as their backend gets the configure's log
**streamed on the same connection**, which only works while the configure
belongs to the run the CLI is attached to — so the workspace is the only place
that answer can live.

This is not apply-*derived* targeting. The host set still comes from the
inventory and the limit from `default-limit`; what the apply decides is only
*when*. And that ordering is forced by the data flow rather than chosen: an
apply may create the very inventory rows a configure reads, so a configure
before it would target a stale host set.

A composite run moves `pending → queued → planning → planned → confirmed →
applying → configuring → applied`. It does **not** go terminal between the two
halves — that is the whole mechanism, because a terminal run closes the log
stream the CLI is reading.

## Which pushes imply which operation

Three roots, two operations, and they are independent:

| A commit touching… | creates |
|---|---|
| `working-directory` (or a `trigger-prefixes` entry) | a plan/apply run |
| `config-directory` | a configure run, if `auto-configure-enabled` |
| both, in one push | both — the plan/apply first, and the configure queues behind it |
| neither | nothing |

`inventory-paths` needs no entry of its own: its entries are relative to
`config-directory` and may not escape it, so they are inside that scope
already.

Where `configure-after-apply` is on and the push implies a plan/apply, that run
*is* the configure — you get one composite run rather than two, because
otherwise the playbook would run twice for one commit, once before the apply had
written the inventory it reads.

A workspace that declares no plan/apply operation creates no plan run, and one
whose trigger you have not turned on does no work at all on a push: the decision
is made before the repository is fetched.

## Who may

A configure runs a playbook against real hosts — it can restart services,
rotate credentials and reboot machines — so it is **apply-equivalent**:

| Capability | May start a configure |
|---|---|
| `run:plan` (plan tier) | no |
| `run:apply` (write tier) | **yes** |

There is no separate `run:configure` axis. The control over what a given person
can configure is **which workspaces they hold write on** — and where different
people should be allowed to run different playbooks, those are different
workspaces with different label RBAC, exactly as two Terraform workspaces over
one repository already are. A **runner token** cannot start one at all.

A triggered configure has no human principal, and none is resolved: turning the
trigger on requires write on the workspace, and that write **is** the
authorisation — the same arrangement `auto-apply` has always had.

## Per-run overrides

A manual invocation may name a different playbook or a different limit for that
run only:

```sh
curl -X POST "$TERRAPOD/api/v1/runs" \
  -H 'Content-Type: application/vnd.api+json' \
  -d '{"data":{"attributes":{"is-configure":true,"playbook":"patch.yml","limit":"web"},
       "relationships":{"workspace":{"data":{"type":"workspaces","id":"ws-…"}}}}}'
```

Both are recorded on the run, so "what did this configure actually target" is
answerable afterwards even once the workspace's defaults have moved. Empty
means the workspace's default.

The plan/apply run options are **refused** on a standalone configure rather
than ignored — `target-addrs`, `replace-addrs`, `refresh-only`, `save-plan`,
`allow-empty-apply` and `is-destroy` all narrow or alter a plan, and a
configure has neither. `plan-only` is refused too: ansible's dry run is
`--check`, which is its own option, not this one.

## A play that targets the execution environment itself needs `ansible_remote_tmp`

Most configures target managed nodes over SSH and need nothing here. But a play
that targets the execution environment — `connection: local`, `hosts: localhost`,
or anything driving a cloud API from the controller — needs one host variable, and
without it **every task fails as UNREACHABLE** with a message about permissions
that points away from the cause:

```
Failed to create temporary directory ... Failed command was:
( umask 77 && mkdir -p "` echo /home/ansible/.ansible/tmp `" ...
```

Ansible resolves `remote_tmp`'s leading `~` against the **target user's passwd
entry**, not `$HOME`. Inside the execution environment that is `/home/ansible`,
which sits on the EE's read-only root filesystem, so the directory cannot be
created. The `$HOME` the Job sets (`/tp/home`, on the writable shared volume) is
not consulted for this.

Point it at the shared volume instead:

```yaml
all:
  hosts:
    localhost:
      ansible_connection: local
      ansible_remote_tmp: /tp/tmp/ansible-remote
```

It can equally go in `group_vars`, or in your own `ansible.cfg` as
`remote_tmp` — Terrapod never overwrites that file, so a value you set there is
honoured. What Terrapod does **not** do is set `ANSIBLE_REMOTE_TEMP` to a shared
volume path globally: that variable names a path on the *target*, and `/tp` does
not exist on a managed node, so a global value would break every SSH-targeted
play to fix the local case.

## Troubleshooting

| What you see | Why |
|---|---|
| 422 naming `configure-enabled` | the workspace declares no configure operation |
| 422 about a speculative configuration version | a configure cannot run an unmerged branch's playbook against real hosts |
| 403 naming `run:apply` | plan tier is not enough for a configure |
| A push to the playbook does nothing | `auto-configure-enabled` is off, or the changed file is outside `config-directory` |
| A configure ran against more hosts than expected | `default-limit` is empty, which means the whole inventory — check the Resolved view with the limit you intended |
| An apply did not configure | `configure-after-apply` is off, or the apply was a destroy |
