# Ansible on Terrapod

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
executing operation, one RBAC, policy and audit model — as far as Ansible allows.

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
