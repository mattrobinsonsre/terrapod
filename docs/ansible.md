# Ansible on Terrapod

> **This page documents the 2.0 line.** Ansible support ships in **2.0**, which
> is in pre-release; it is **not** in the stable 1.11.x line, so
> `configure_enabled` and everything on this page is refused by a 1.11.x API as
> an unknown attribute. If you installed the stable line, nothing here applies
> yet. See the [Status note in the README](../README.md#terrapod) for which line
> is which.

Terrapod runs Ansible as a **configure operation**: a first-class thing a
workspace does, alongside plan and apply, against an inventory the workspace
declares. This page is the story end to end — what a configure is, what a
workspace looks like that has one, how the inventory is composed, how a run
starts, what actually executes, what comes back, and what to do when it goes
wrong. Each step points at the page that owns the detail rather than repeating
it.

If you run only `tofu` or `terraform`, [skip to the last
section](#a-terraform-only-deployment-pays-nothing-for-any-of-this) — none of
this appears in your deployment, and that is by construction rather than by a
setting you have to find.

## A configure is an operation, not a phase and not an engine

A configure is **not** a stage of an apply, and Ansible is **not** a third
execution engine beside OpenTofu and Terraform. Both of those were considered and
rejected, for one reason that decides it: **the work Ansible still owns has no
apply at all.** Day-2 patching, certificate rotation, ordered restarts, OS
hardening, network kit — none of it follows from a Terraform apply, and most of
it has no Terraform in the picture.

Deriving what to configure *from* an apply fails in both directions, too. A
changed tag implies nothing about whether a host needs configuring; a changed
security group affects hosts the apply cannot enumerate; and an untouched host
may badly need configuring because the **playbook** changed rather than the
infrastructure. So nothing is derived: a configure targets the inventory the
workspace declares, and you say when it runs.

When both operations do run on one workspace, the order is **plan → apply →
configure**, and that order is forced rather than chosen — the declared-inventory
model means the apply is what produces the host rows, so a configure before it
would target a stale set.

## Three workspace shapes, and a fourth that is refused

Two independent booleans on the workspace decide what it can do —
`plan-apply-enabled` (**on** by default) and `configure-enabled` (**off** by
default):

| `plan-apply-enabled` | `configure-enabled` | what you have |
|---|---|---|
| on | off | an ordinary OpenTofu/Terraform (or Pulumi) workspace — the default, unchanged |
| off | on | a configure-only workspace: a playbook and an inventory, no state to manage |
| on | on | both, on one workspace; an apply can hand straight over to a configure |
| off | off | **refused at the write boundary** |

The fourth is refused deliberately rather than stored. A workspace that declares
no operations can never produce a run, so accepting one would push a "nothing to
do" branch into the dispatcher, the reconciler, autodiscovery materialisation and
every UI surface — to represent a state nobody wants. Two booleans permit it;
validation does not.

Both default so that **existing workspaces are untouched**: plan/apply stays on,
configure stays off, and nothing changes until someone turns it on.

## The inventory is declared, and Ansible merges it

A workspace's inventory is modelled as the structures Ansible's inventory
actually has — hosts, groups, memberships, group nesting, and variables on a
host, a group or the inventory as a whole — each one an addressable resource you
can write from the API, the Terraform or Pulumi provider, the MCP tools or the
UI.

There are **two sources**: the rows you declare, and optionally a directory in
your repository. **Ansible itself performs the merge**, the precedence, the group
DAG and the `--limit` expansion — Terrapod renders the inputs and reads the
result, rather than reimplementing any of it.

Inventory **plugins are refused**, and the replacement is better rather than
merely safer: cloud discovery is *declared* instead. A data source asks the cloud
what exists and each machine becomes rows, so the host set is versioned,
diffable, and visible in a plan **before** it is adopted — where a plugin
discovers hosts by running code and shows you the result afterwards.

→ [Ansible inventory](ansible-inventory.md) for the seven structures, why a
variable is a row rather than a map, [the two sources and which one
wins](ansible-inventory.md#the-two-sources), [declaring hosts alongside the
infrastructure](ansible-inventory.md#declaring-hosts-alongside-the-infrastructure),
and [why plugins are refused and how to migrate
one](ansible-inventory.md#inventory-plugins-are-refused).

## How a configure starts, and what its blast radius is

Four ways, **all opt-in and all off by default**: the Configure button (or the
API attribute), a push that touches the workspace's Ansible root, any change to
the declared inventory that did not arrive through git, and "after every apply"
on this workspace.

The blast radius is whatever **`default-limit`** declares, and an empty
`default-limit` means **the whole inventory**. That is a deliberate choice rather
than an oversight, and the reasoning is worth reading before you turn anything on
— it rests on visibility and reversibility, not on prevention. Before enabling a
trigger, `?limit=` answers **what a configure would target**, from the live
resolved inventory, without running anything.

Two properties worth knowing up front: a **destroy never configures**, and only
**one mutating run at a time** executes per workspace — a configure holds the
workspace's slot exactly as an apply does, and a manually locked workspace
refuses a configure outright.

→ [Running a configure](ansible-configure.md) for [turning it
on](ansible-configure.md#turning-it-on), [what `default-limit` means and why
that is acceptable](ansible-configure.md#default-limit-is-the-blast-radius-and-empty-means-everything),
[which pushes imply which operation](ansible-configure.md#which-pushes-imply-which-operation),
[who may](ansible-configure.md#who-may) (apply-equivalent: `run:apply`, never a
runner token), and [the per-run
overrides](ansible-configure.md#per-run-overrides).

## What actually runs

The playbook does not run in the runner container. It runs in an **execution
environment** — a container image carrying one `ansible-core` line — started as a
sidecar beside the orchestrator, which drives it over a loopback-only interface
and shares one volume with it. A workspace picks its line, and Terrapod publishes
an image per supported line; an operator may supply their own instead.

→ [What a configure actually runs](runners.md#what-a-configure-actually-runs) and
[the Job a configure runs in](runners.md#the-job-a-configure-runs-in).

## What comes back: one row per host

Ansible's unit of outcome is the **host**, not the resource, so a configure's
result is a table rather than a single status. The run page grows a **Hosts** tab
whenever a run has host results — nothing to switch on, and a run with no hosts
simply does not have the tab.

One thing about that table is essential and easy to misread: **a host shown as
"not reached" is not a host that passed.** Ansible reports only the hosts it got
to, so under `serial:` with `any_errors_fatal:` a failure in the first batch ends
the play and the later batches appear in no event and in no `PLAY RECAP` at all.
The table is therefore the union of what Ansible reported and what the run's own
resolved inventory named, and a host in the second set only is shown as **not
reached**, with empty counters rather than zeros — zero is a measurement, and
this is the absence of one. Without that, a run that aborted after one host of
fifty would read as a clean one-host success.

→ [What a configure reports](ansible-configure.md#what-a-configure-reports-one-row-per-host),
and [reading the log one host at a time](ansible-configure.md#reading-the-log-one-host-at-a-time-or-all-of-them).

## Recovery is per host, not per run

Because the outcome is per host, so is the retry. A failed or cancelled configure
can be re-run **against its outstanding hosts only**, rather than against
everything — and a run with nothing outstanding is **refused** rather than
quietly re-run, so "retry" cannot silently become "do it all again".

A cancelled configure is a partial configure and retries the same way. On
cancellation Ansible is signalled **once**: it stops at the task it is on and the
run reports that it was interrupted, rather than that the playbook failed.

→ [Running a configure](ansible-configure.md#troubleshooting), and the
[configure runbooks](runbooks.md#a-configure-failed-partway-retrying-only-the-outstanding-hosts)
for the operational procedures.

## Credentials

A configure needs two kinds of secret, and they are ordinary workspace variables
in dedicated categories rather than a new mechanism: `ansible_machine_auth` for
the credential that reaches the hosts, and `ansible_vault_auth` for an Ansible
Vault password per vault ID label.

**Ansible Vault is not OpenBao or HashiCorp Vault** — it is Ansible's own file
encryption, and conflating the two is the most common confusion here. Both
categories are always sensitive, are shareable through a variable set, are
delivered **only** to a configure, and become `0600` files on the volume the
execution environment shares — never a command line.

A configure can also hold its own **federated cloud identity**, so a playbook
that talks to a cloud API need not carry a static key.

→ [Ansible credentials](ansible-credentials.md), and [cloud credentials for a
configure](ansible-configure.md#cloud-credentials-a-configure-gets-its-own-federated-identity).

## One worked example, from nothing to a configure that runs

Everything above is a separate page, and each of those pages is about one piece.
This is the shape the pieces make together: a workspace that declares a configure,
the inventory it targets, the credential that reaches the hosts, and the
placeholder that joins the last two — the smallest complete thing, with nothing
left to click.

It is written as OpenTofu/Terraform because that is the idiom every piece already
has: the seven inventory structures and both credential categories are
first-class resources, so a configure is declarable in full rather than
half-declared and half-configured. The same objects are reachable from the API,
the Pulumi provider, the MCP tools and the UI.

```hcl
# The two objects this hangs off already exist: the pool whose listener will run
# the Job, and the VCS connection the playbook is fetched through. A configure
# runs server-side, so a pool is not optional.
data "terrapod_agent_pool" "fleet" {
  name = "fleet-pool"
}

data "terrapod_vcs_connection" "github" {
  name = "platform"
}

# 1 — the workspace DECLARES the operation. `configure_enabled` is the
# declaration; `config_directory` is the Ansible root inside the repository and
# is what the rest of it is relative to. Nothing here runs anything yet.
#
# `plan_apply_enabled = false` makes this a configure-only workspace: a playbook
# and an inventory, no state to manage. Leave it at its default (`true`) instead
# and you have the both-operations shape, where an apply can hand straight over.
resource "terrapod_workspace" "fleet" {
  name = "web-fleet"

  plan_apply_enabled = false
  configure_enabled  = true

  config_directory = "ansible"  # ansible.cfg, playbooks and any committed inventory
  default_playbook = "site.yml" # relative to config_directory
  default_limit    = "canary"   # the blast radius -- a group declared below

  agent_pool_id     = data.terrapod_agent_pool.fleet.id
  vcs_connection_id = data.terrapod_vcs_connection.github.id
  vcs_repo_url      = "https://github.com/example/web-fleet"
  vcs_branch        = "main"
}

# 2 — the groups. `canary` is the one `default_limit` names, and declaring it is
# not optional: a `--limit` naming a group that does not exist leaves ansible
# with nothing to target and the run fails. See the note under
# `default-limit` in Running a configure.
resource "terrapod_inventory_group" "web" {
  workspace_id = terrapod_workspace.fleet.id
  name         = "web"
}

resource "terrapod_inventory_group" "canary" {
  workspace_id = terrapod_workspace.fleet.id
  name         = "canary"
}

# 3 — the hosts. A host carries its NAME and nothing else; `ansible_host` is a
# variable like any other, so the address is a row rather than a field.
locals {
  web_hosts = {
    web1 = "10.0.1.11"
    web2 = "10.0.1.12"
  }
}

resource "terrapod_inventory_host" "web" {
  for_each = local.web_hosts

  workspace_id = terrapod_workspace.fleet.id
  name         = each.key
}

resource "terrapod_inventory_host_var" "address" {
  for_each = local.web_hosts

  host_id = terrapod_inventory_host.web[each.key].id
  key     = "ansible_host"
  value   = each.value
}

# 4 — membership. Every host is in `web`; only `web1` is in `canary`, which is
# what makes the default limit a canary rather than a synonym for everything.
resource "terrapod_inventory_host_group" "web_members" {
  for_each = local.web_hosts

  host_id  = terrapod_inventory_host.web[each.key].id
  group_id = terrapod_inventory_group.web.id
}

resource "terrapod_inventory_host_group" "canary_member" {
  host_id  = terrapod_inventory_host.web["web1"].id
  group_id = terrapod_inventory_group.canary.id
}

# 5 — the connection settings that are NOT secrets, as group variables. They
# live here rather than on the credential deliberately: each is visible in a
# diff, reviewable in a pull request, and settable per host or per group.
#
# The two placeholders are the join. `${terrapod:<name>.<field>}` is resolved at
# run time against the credential named below, and for `private_key` and
# `known_hosts` it substitutes the PATH of a 0600 file Terrapod writes -- the key
# material itself never enters the inventory document.
resource "terrapod_inventory_group_var" "web_user" {
  group_id = terrapod_inventory_group.web.id
  key      = "ansible_user"
  value    = "deploy"
}

resource "terrapod_inventory_group_var" "web_become" {
  group_id   = terrapod_inventory_group.web.id
  key        = "ansible_become"
  value      = "true"
  structured = true # a YAML boolean rather than the string "true"
}

resource "terrapod_inventory_group_var" "web_key_file" {
  group_id = terrapod_inventory_group.web.id
  key      = "ansible_ssh_private_key_file"
  value    = "$${terrapod:web-key.private_key}"
}

resource "terrapod_inventory_group_var" "web_known_hosts" {
  group_id = terrapod_inventory_group.web.id
  key      = "ansible_ssh_common_args"
  value    = "-o UserKnownHostsFile=$${terrapod:web-key.known_hosts}"
}

# 6 — the credential: secrets only, plus the one public file. Its `key` is the
# credential's NAME -- what ${terrapod:web-key.private_key} above refers to --
# and says nothing about where it is used; the inventory says that, by writing a
# placeholder wherever the value is wanted. At least one of `private_key` or
# `password` is required, and an unrecognised field is refused with a 422 rather
# than silently ignored.
resource "terrapod_variable" "web_key" {
  workspace_id = terrapod_workspace.fleet.id
  key          = "web-key"
  category     = "ansible_machine_auth"
  sensitive    = true

  value = jsonencode({
    private_key = file("~/.ssh/fleet")
    known_hosts = <<-EOT
      10.0.1.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...
      10.0.1.12 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...
    EOT
  })
}
```

Note the `$${` in step 5. A Terrapod placeholder and OpenTofu's own
interpolation (Terraform's too) use the same `${...}` syntax, so the dollar has
to be escaped or the engine tries to resolve it and fails on an unknown name.
Written through the API, the UI or the MCP tools there is nothing to escape — it
is `${terrapod:...}` verbatim.

**What you have after `tofu apply`** is a workspace with a configure operation,
two hosts, two groups, and a credential nothing can read back. What you do not
have is a run: every trigger is off by default and this configuration turns none
of them on, so nothing has happened to the hosts yet.

**Before starting one**, ask what it would target — the limit is doing the job a
plan does elsewhere, so this is the review step rather than a convenience:

```sh
curl -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  "$TERRAPOD_URL/api/v1/workspaces/$WS/inventory/resolved?limit=canary"
```

That should come back with `web1` and nothing else. The Inventory tab's Resolved
view is the same answer, and `data "terrapod_inventory_resolved"` is the same
answer in a plan. **A `host_count` of zero means the pattern matched nothing**,
which is the one result worth stopping on.

Then press **Configure** on the run page, or turn on a trigger
([`auto_configure_enabled`](ansible-configure.md#turning-it-on) for a push,
`configure_after_apply` for an apply). The run goes `queued` → `configuring` →
`applied`, and its **Hosts** tab carries one row per host.

→ Each step in full: [the seven
structures](ansible-inventory.md#the-seven-structures) and [managing them as
code](ansible-inventory.md#managing-it-as-code); [turning a configure
on](ansible-configure.md#turning-it-on) and [what `default-limit`
means](ansible-configure.md#default-limit-is-the-blast-radius-and-empty-means-everything);
[the credential envelope](ansible-credentials.md#the-envelope) and [where the
placeholder may
point](ansible-credentials.md#key-is-the-credentials-name-and-the-inventory-says-where-it-is-used).

## There is no plan for a configure

Ansible has **no separable plan**. There is no artifact that says what a playbook
would do, because a playbook's behaviour depends on facts gathered from the hosts
at the moment it runs. So a configure has no plan phase, no diff to approve, and
no "planned" state to sit in.

**`--check` is not a plan**, and Terrapod does not present it as one. Check mode
skips tasks that cannot report without changing something, silently, so its
output is neither a complete statement of what would happen nor a safe subset —
it is a different run with different coverage. Terrapod adds no `--check` flag
today, and whether a dry-run option should exist at all is still open.

What you review is therefore **the playbook and the limit**, not a diff. That is
what makes `?limit=`, the `default-limit` blast radius and apply-equivalent RBAC
the controls that matter here — they are doing the job a plan does elsewhere.

## What Ansible is for here

Worth being plain about, because the wrong expectation is easy to form:

**It is for** long-lived virtual machines, network devices and appliances,
golden-image pipelines, OS hardening and compliance baselines, and day-2
operational work — patching, certificate rotation, ordered restarts, the things
with no apply.

**It is not** the other half of a modern cloud platform. If your estate is
containers and managed services declared in OpenTofu or Terraform, a configure
has little to do, and that is the right answer rather than a gap. Terrapod is an
OpenTofu/Terraform orchestrator first; Ansible is supported by coercing it into
the same flow — one workspace per unit of work, a reviewed decision then an
executing operation, one RBAC and audit model — as far as Ansible allows. The
governance gates are the part Ansible does not reach, and the next section says
exactly which and why.

## Where Ansible is not coerced, and why

Terrapod coerces Ansible into the Terraform flow as far as Ansible allows, and
this section is the honest list of where that stops. The sibling section for the
other engine is [Where Pulumi is not coerced, and
why](pulumi.md#where-pulumi-is-not-coerced-and-why).

### The four post-plan gates do not fire on a configure

**None of OPA policy sets, IaC security scanning, the AI policy gate or post-plan
run tasks runs on a standalone configure.** All four sit at the post-plan
boundary, and a configure has no plan to sit behind: it goes `queued` →
`configuring` → `applied`, with RBAC at creation as the only check.

A **composite** apply-then-configure is the case to understand, because it looks
governed and is only half governed: the gates run on the **Terraform plan**, the
run is confirmed, and then the playbook executes with no further gate. A green
policy check on that run is a statement about the infrastructure, not about the
playbook.

There is also **no not-evaluated signal**, which is a gap rather than a decision.
A Pulumi preview carries `meta.not-evaluated-reason` so a reader can see that a
gate was skipped; that mechanism keys on the workspace's **engine**, and a
configure is an operation rather than an engine (see the first section), so
nothing emits a reason for it. Until that changes, the absence of a policy result
on a configure means the gates never ran — not that they passed.

### What does govern a configure

Not nothing, and the controls are deliberately the ones a configure can actually
have:

| control | what it does |
|---|---|
| **RBAC at creation** | a configure needs `run:apply`, because it changes real hosts and is apply-equivalent. It is not gated at plan tier |
| **The limit** | `--limit`, from the run or the workspace's `default-limit`, is the blast radius, and the Runs tab states it before you press the button — an empty default means every host in the inventory |
| **The playbook is repository code** | it arrives in a configuration version, so it is reviewed in a pull request like any other change, and a VCS-connected workspace cannot run a playbook that is not committed |
| **Audit** | every run, every credential resolution and every host result is recorded, exactly as for an apply |

As [There is no plan for a configure](#there-is-no-plan-for-a-configure) puts it,
what you review is the playbook and the limit — and those are doing the job a
plan does elsewhere. The difference this section adds is that **the automated
gates are not doing it with them**.

### Why not simply run the gates anyway

Each of the four reads a **Terraform plan JSON** and answers a question about
resource changes. A playbook produces no such document, and `--check` is not a
plan — it is a dry run whose result nothing links to a later execution. Pointing
OPA at a configure would mean either inventing a plan-shaped artefact for it or
writing a second, differently-shaped policy surface; both are real designs and
neither is what "the same policy model" means. Saying so is better than implying
a coverage we do not have.

## A terraform-only deployment pays nothing for any of this

If nobody has turned a configure on, your deployment has no Ansible in it. There
is **no capability flag to set and none to leave unset** — the engine on/off
switch was considered and withdrawn, because the platform should simply work.

What delivers that is **runtime adaptation rather than configuration**: surfaces
appear when there is something to show. A run with no host results has no Hosts
tab. A workspace with no configure operation offers no configure controls. The
Inventory tab is data-gated. Nothing to configure, nothing to mis-set, and
nothing that can go stale — the same way health conditions, drift indicators and
the Cost, Security and policy run tabs already behave.

So: no extra configuration, no navigation for a tool you do not use, nothing
destructive in any combination, and `tofu` / `terraform` behaviour untouched in
every combination.

## See also

| Page | What it covers |
|---|---|
| [Ansible inventory](ansible-inventory.md) | The seven structures, the two sources, the live resolved read and `?limit=`, why plugins are refused, the name rules |
| [Running a configure](ansible-configure.md) | How one starts, the blast radius, who may, per-run overrides, per-host results, troubleshooting |
| [Ansible credentials](ansible-credentials.md) | `ansible_machine_auth` and `ansible_vault_auth`, where the files land, what outranks what |
| [Runners](runners.md#what-a-configure-actually-runs) | The execution environment, the Job a configure runs in |
| [Runbooks](runbooks.md#a-configure-never-started-the-execution-environment-did-not-become-ready) | Four operational procedures: the EE never became ready, hosts "not reached", retrying outstanding hosts, a configure refused before it started |
| [API reference](api-reference.md) | The run and workspace attributes a configure adds |
