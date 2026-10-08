# Ansible inventory

A Terrapod workspace carries **one inventory**, made of the structures ansible's
inventory actually has: hosts, groups, the memberships between them, the
nestings between groups, and variables on a host, on a group or inventory-wide.
Each is its own addressable resource, so each can be created, read and changed
on its own — from the API, from the Go SDK, from OpenTofu or Terraform, from the
MCP tools, or in the web UI.

Terrapod stores those structures and renders them. **Ansible performs the
merge**, the precedence, the group DAG, the derivation of `all` and `ungrouped`,
and the expansion of `--limit`. There is no second implementation of any of it.

> ## Status: the inventory exists; configure operations do not
>
> **This release delivers the inventory — its structures, its two sources, what
> they resolve to, and the surfaces that make all of it observable. It does not
> run playbooks.** There is no configure operation and no playbook reference.
>
> What you can do now is **build** an inventory and **see what it resolves to**,
> including which hosts a `--limit` pattern would select. That ordering is
> deliberate rather than partial: an operator has to be able to answer "what
> would a configure target" before anything is able to target it.
>
> A workspace that declares nothing and binds no repository carries no inventory
> rows at all, so a terraform/tofu-only deployment pays nothing for any of this
> — by data rather than by a feature flag.

---

## The eight structures

| Structure | Id prefix | What it is in ansible's terms |
|---|---|---|
| Inventory settings | the workspace id | Whether the declared rows take part, plus the optional VCS binding |
| Host | `invhost-` | One `inventory_hostname` |
| Group | `invgroup-` | One group. `all` and `ungrouped` are **refused** as declared names |
| Host membership | `invhg-` | One `[groupname]` line. Many-to-many |
| Group nesting | `invgc-` | One `[groupname:children]` entry. Many-to-many |
| Host variable | `invhvar-` | One entry in that host's `host_vars` |
| Group variable | `invgvar-` | One entry in that group's `group_vars` |
| Inventory variable | `invvar-` | One entry in `group_vars/all` |

**A host carries nothing but its name.** There is no `address` field:
`ansible_host` is a variable like any other, because that is what it is to
ansible, and a promoted field would be a second home for one value with a
precedence rule to explain.

**An inventory variable is parented on the workspace, not on a group.** `all`
cannot be a declared group — the rendered inventory document is rooted at
`all:`, so a group of that name would collide with the document's own structure
— and `group_vars/all` is nevertheless an ordinary thing to want. These rows
land at that root's `vars:`, which is the consequence of the refusal rather than
a way round it.

### Why a variable is a row and not a map

Each variable is a row with **one writer**. That is the whole reason the shape is
eight structures rather than a host object carrying its groups in a list and its
variables in a map.

With rows, a second concern can contribute a variable to a host or a group it
does not own, and can add its own hosts to a shared group without owning the
group. With a map, every writer owns the whole map, so two concerns cannot both
contribute without one of them reverting the other on its next apply. The same
argument makes a membership its own row: a Terraform resource needs something it
can address, and a membership that is only a list on one side cannot be one.

### One inventory per workspace

There is no named inventory object, and no collection of them. Disjoint
targeting is what groups and `--limit` are for, which is ansible's own answer, so
nothing has to choose between several.

The settings row is 1:1 with the workspace and may simply be absent — which is
the ordinary state. `GET …/inventory/settings` answers **404** when there is
none, and that is a default rather than an error: it means "no VCS source is
bound, and the declared rows are the whole inventory".

---

## The two sources

| Source | What it holds | Where it comes from |
|---|---|---|
| **platform** | The rows above | Whatever writes them — OpenTofu or Terraform, the API, the UI, the MCP tools |
| **VCS** | A directory of inventory files in a repository | The binding on the settings row |

Either may be absent: the declared rows alone, a repository alone, both, or
neither — and neither is a defined empty result rather than an error.

Set `include-platform` to `false` to resolve the repository alone, which is how
an operator moves a committed inventory in before declaring anything of their
own.

### A VCS source is a directory, read as one source

The binding names a **directory**, not a file, and ansible reads a directory as
one source in lexical filename order. So a single binding already carries
arbitrarily many inventory files, and the order within it is the operator's
business through filenames — the convention of numeric prefixes
(`00-common.yml`, `10-prod.yml`, …) works exactly as it does outside Terrapod.

**The binding is independent of the workspace's own Terraform VCS binding**, and
that is why these are their own fields rather than a reference to it. Even in one
repository the root directory differs — `terraform/` against `ansible/` — and a
workspace with no infrastructure of its own has no Terraform binding at all.

`working-directory` and `ignore-paths` are the two ways to narrow the source,
and they work at different scales. `working-directory` is the path the
repository is fetched and read from, so it decides what arrives at all.
`ignore-paths` then prunes what did: each entry is a glob matched against paths
**relative to `working-directory`**, so an operator never writes a prefix they
did not choose, and the same matcher an autodiscovery rule's `ignore-patterns`
uses applies here.

Pruning is the only way to leave a file out of this source, because ansible
reads a directory as one source and takes every file in it. The prune happens
before anything else reads the tree, so an excluded file is never scanned and
cannot refuse the resolution on its own account.

### Merge order is fixed: the declared rows win

Terrapod runs `ansible-inventory` with the repository first and the rendered
platform document second, so **a conflicting variable goes to the declared
row**. The committed inventory is the baseline; what the platform declares
overrides it — the same relationship a Terraform variable has to a default coded
in the configuration.

Everything about how that merge behaves is ansible's own: hosts union, group
memberships union, and the merge is per variable rather than per host, so a
non-conflicting variable from the repository survives alongside a declared one.
An operator's existing mental model of `-i` ordering transfers exactly, because
that is literally what this is.

---

## A read is live, and writes nothing

**Every source is static.** Dynamic inventory — an external, AWX-style source
that executes a script or a plugin to discover hosts — was considered and
declined; the reasoning is recorded on
[#1970](https://github.com/mattrobinsonsre/terrapod/issues/1970), closed as
not-planned. A repository file at a resolved commit is static too: it is a file,
not a program that runs.

So resolving an inventory is a function of the rows and the commit, and a read is
**live**: Terrapod resolves to answer the request. There is no
cached-versus-fresh distinction, no timestamp beside the result, no freshness
field and no refresh action — because there is no other resolution the answer
could be.

A Redis entry does back it, and it is deliberately invisible. The key is
content-addressed — an equality token over the rows, plus the resolved commit —
so a write **supersedes** the entry rather than needing to invalidate it. There
is nothing for an operator to tune and nothing to flush. Invalidating on write
would instead mean a call site in every one of the eight structures' writers, and
missing one is a silent stale read.

**It fails closed.** A resolution that cannot be performed is refused, never
degraded into an empty or partial host list: a silently short target set is the
failure mode this whole surface exists to prevent, and unlike a policy gate there
is no later evaluation to catch it.

### Two behaviours of ansible's output worth knowing precisely

Both were measured against ansible-core 2.21.5, and neither is documented
upstream.

**A group's host list is DIRECT membership only.** Ansible does not flatten
nesting into it, so a parent group whose members all arrive through a child
reports no hosts of its own. The nesting is carried separately, in
`group-children`, rather than resolved into the lists — taking the transitive
closure would be Terrapod computing the group DAG, which is ansible's job.

**So do not read an empty host list on a parent as "this group targets
nothing".** The authoritative answer to "what would this target" is
`?limit=<group>`, which ansible expands **through** the nesting — measured,
limiting to a parent whose members are all inherited returns exactly those hosts.
That is why the limit is a parameter on the resolved read rather than something a
consumer assembles.

### The `--limit` preview

```
GET /api/v1/workspaces/{id}/inventory/resolved?limit=<pattern>
```

The pattern is passed straight to `ansible-inventory --limit`, so the expansion
is ansible's own: host names, group names, `all` and `*`, globs, comma- or
colon-separated terms, `!` exclusion, `&` intersection — and **`~regex` works**,
because ansible applies the expression and Terrapod never has to decide what it
would have matched.

This is the safety surface rather than a convenience. Auto-configure is
deliberately broad — at scale the hazard is hosts left unconfigured, not hosts
configured — so visibility is the control, and "what would this target" has to be
answerable before anything runs.

---

## Inventory plugins are refused

An operator onboarding an existing inventory directory will meet this, so it is
worth knowing before the refusal arrives rather than after.

A file whose top level carries a `plugin:` key is an inventory **plugin
configuration**, not a static inventory. Terrapod refuses it, naming the file and
the plugin it asks for, and says what to do instead. Three layers close it, and
they do different jobs:

1. **The plugin is not installed.** `ansible-core` ships no collections, so a
   cloud inventory plugin does not exist in the environment at all. Decisive, and
   completely silent.
2. **`enable_plugins = yaml, ini`** in the generated `ansible.cfg` — no `auto`,
   which is the generic dispatcher that reads a file's `plugin:` key and runs
   whatever it names, and no `script`, which would execute a source.
3. **An explicit pre-scan**, which is the layer that matters. Disabling the
   plugin does not make the file inert; it makes it **garbage input**. A file
   named `*.aws_ec2.yml` still ends in `.yml`, so the `yaml` plugin claims it
   whatever is inside and then treats `plugin:`, `regions:` and `filters:` as
   **group names**. An operator would get a confusing parse result rather than an
   answer, so the file is named and refused instead.

The same pass **strips the executable bit** from everything fetched, which is the
whole decision for a `script`-shaped source: a static file beside an executable
one otherwise resolves to the union of both.

And `any_unparsed_is_failed = True` is set, which is the property that matters
most: a source that cannot be read **fails the resolution** rather than silently
contributing nothing. Without it an unreadable file is a warning, and the answer
is a target set quietly missing whatever it held.

### Migrating a cloud inventory plugin

A plugin source becomes a declared one, and the platform source **is** the
dynamic inventory — except versioned in git, reviewed in a pull request, and
visible in a plan before it takes effect.

```hcl
# Was: an inventory plugin configuration asking the cloud for instances.
# Now: a data source asks, and each machine becomes rows.

data "aws_instances" "app" {
  instance_tags        = { Role = "app" }
  instance_state_names = ["running"]
}

data "aws_instance" "app" {
  for_each    = toset(data.aws_instances.app.ids)
  instance_id = each.key
}

resource "terrapod_inventory_group" "app" {
  workspace_id = var.workspace_id
  name         = "app"
}

resource "terrapod_inventory_host" "app" {
  for_each     = data.aws_instance.app
  workspace_id = var.workspace_id
  name         = each.value.tags["Name"]
}

resource "terrapod_inventory_host_var" "app_address" {
  for_each = terrapod_inventory_host.app
  host_id  = each.value.id
  key      = "ansible_host"
  value    = data.aws_instance.app[each.key].private_ip
}

resource "terrapod_inventory_host_group" "app" {
  for_each = terrapod_inventory_host.app
  host_id  = each.value.id
  group_id = terrapod_inventory_group.app.id
}
```

That workspace creates nothing, changes nothing, and adopts nothing into the
configuration's management. It is ansible targeting over an existing fleet of
long-lived machines, with no infrastructure change and no migration — which is
the case that would otherwise have to wait for the whole estate to be brought
under IaC first.

---

## Declaring hosts alongside the infrastructure

The other pattern, and the one to reach for when the workspace owns the machines
rather than merely reading them. The host is created and declared together, so
the ordering is **implicit**: the host variable references the instance, so the
engine builds the instance first and the apply is what writes the inventory.

```hcl
resource "aws_instance" "web" {
  for_each = var.web_nodes
  # ...
}

resource "terrapod_inventory_host" "web" {
  for_each     = aws_instance.web
  workspace_id = var.workspace_id
  name         = each.key
}

resource "terrapod_inventory_host_var" "web_address" {
  for_each = terrapod_inventory_host.web
  host_id  = each.value.id
  key      = "ansible_host"
  value    = aws_instance.web[each.key].private_ip
}
```

No `depends_on` is needed or wanted. The reference to `private_ip` is the
dependency, and it is the reason the inventory cannot be written before the host
it describes exists.

---

## Nothing here is read-only

Every structure is an ordinary resource on every surface: readable and writable
through the API, go-terrapod, the OpenTofu/Terraform provider, the MCP tools and
the web UI alike. There is no platform-imposed read-only tier.

What ownership means is **OpenTofu's (or Terraform's) own semantics**, which are
the ones an operator already knows:

- a row that a configuration manages and someone edits by hand is **reverted by
  the next apply**, because the configuration is the desired state;
- a create that collides with a row that already exists gets a **409**, and the
  practitioner **imports** it — exactly as for every other resource the
  configuration manages.

So a hand edit is not forbidden, it is simply not durable against a configuration
that owns the row. A row nothing declares — a group an operator added in the UI, a
variable set through the API — is as permanent as any other.

---

## Variables: encryption, masking, and typed values

**`sensitive` is a display flag and nothing more.** It decides what a reader
sees: a sensitive value reads back from the API as a fixed mask rather than its
own length or shape, because either would leak something about it.

**That is orthogonal to encryption at rest**, and worth stating in both
directions, because the two are easy to conflate. All three variable surfaces are
registered for Terrapod's [app-layer encryption](encryption-at-rest.md) — a host
variable is an ordinary place for an `ansible_become_password`, and a column
cannot be conditionally encrypted, so the column is covered rather than the flag
deciding it. Where a deployment has that encryption enabled, every value is
enveloped whatever its flag says, and a key rotation re-keys all three. Where it
is not enabled — which is the default — the column is plaintext in the database
and the protection is the datastore's own at-rest encryption, exactly as for a
workspace variable's value.

**`structured` says the stored text is a typed expression** rather than a plain
string — a list, a number, a nested object. That is the same question
`structured` answers on a workspace variable, and the same thing ansible's own
`group_vars` carries natively. It is parsed as YAML rather than JSON, because
YAML is a superset and the value is going straight back out as YAML: both
`[80, 443]` and a block sequence work, so an operator writing either gets what
they meant.

---

## Names: what is accepted, and why the rest is refused

### Group names

`all` and `ungrouped` are **derived by ansible**, not declared — `all` holds
every host and `ungrouped` holds the hosts in no other group — so a source
declaring either would be asking for a group whose membership it does not
control. `all` is the sharper case, because the rendered document is rooted at
`all:`.

Otherwise a group name must start with a letter or underscore and contain only
letters, digits and underscores. Ansible tolerates more and warns, but a name it
warns about cannot be used reliably in a `--limit` pattern or as a `group_vars`
filename, so it is refused at the write — where the message names the value —
rather than stored and regretted.

A nesting that would close a **cycle** is refused too. The one-step case is a
database constraint; a longer one is checked before the write, because a
constraint cannot walk a graph. Terrapod does not resolve the graph itself —
ansible does — so this is refused for the operator's sake rather than to protect
anything here from looping on it.

### Host names

`--limit` has its own operators and separators, and a host named with one is not
merely ugly — it is unusable, or worse:

| Character | What `--limit` does with it |
|---|---|
| `,` `:` | Separate patterns |
| `!` | Exclude |
| `&` | Intersect |
| `~` | Introduce a regular expression |
| whitespace | Splits terms |

So a host called `!web` can never be selected **and silently excludes `web` from
any pattern that names it**. All of the above are refused at the write, with a
message naming the offending character and the reason.

Everything else is fine, which includes the shapes an operator actually wants:
letters, digits, dots, hyphens and underscores. Name hosts the way you would in
an inventory file — `web-01`, `db.internal`, `app_eu_west_1a` — and nothing here
applies.

### Variable names

Deliberately laxer: a variable name only has to be a non-empty string with no
leading or trailing whitespace. Ansible stores a variable whose name is not an
identifier and only warns that it is unreachable as a bare `{{ name }}` — it is
still readable through `hostvars`, which some roles do on purpose. Refusing more
would block working configurations to prevent a warning. The whitespace rule is
there because such a name is almost certainly a mistake and is invisible in every
surface that displays it.

---

## `provider "terrapod" {}` needs nothing in it

Inside an agent-mode run, that empty block is the whole provider configuration.
The runner exports what the provider reads before the engine starts:

| Variable | What the runner sets it to |
|---|---|
| `TERRAPOD_HOSTNAME` | The **internal** API URL the Job already holds |
| `TERRAPOD_TOKEN` | **The run's own token.** Nothing new is minted |

The internal URL rather than the public one on purpose: the public hostname may
not resolve from inside the cluster at all, and a provider pointed at it could
end up dialling the runner pod's own loopback. `TERRAPOD_HOSTNAME` is named for a
hostname and accepts a full URL, so a complete URL passes through untouched.

What makes the run's own token sufficient is a single implicit grant: **a runner
token may manage the inventory of its own run's workspace**, and nothing else. It
is the same shape as the implicit registry read a runner token already carries,
and for the same reason — an apply cannot work without it. Reads are unphased,
because a plan reads the inventory to diff it; **writes are bound to the apply
phase**.

### Two exceptions, both deliberate

**Local execution mode is untouched.** There is no runner token and nothing in
the runner runs, so the provider needs a host and a token configured exactly as it
does today. The implicit grant is a property of running *inside* a run.

**If you set either variable yourself, Terrapod exports neither.** A workspace
`env` variable named `TERRAPOD_HOSTNAME` or `TERRAPOD_TOKEN` is a legitimate
thing to have, so Terrapod stands aside and logs that it has done so. The two
decisions are taken **together**, never separately: supplying one while deferring
to a workspace variable for the other is the arrangement that would send the
run's real credential to a host the variable chose. So it is all-or-nothing by
design, not by omission.

---

## Permissions

Two capabilities in the workspace axis:

| Capability | Tier | Grants |
|---|---|---|
| `inventory:read` | **read** | Read every structure, the settings, and the resolved view including a `--limit` |
| `inventory:write` | **write** | Create, change and remove every structure, and bind or clear the VCS source |

Both are granted by the existing presets, so no role became more or less powerful
when they were introduced.

**`inventory:write` is in the write tier, not admin, and that is deliberate.**
The Terraform that declares a host runs under an apply, and `run:apply` is a
write-tier capability — so an API gate stricter than the path every row actually
arrives by would be incoherent. Building an inventory is ordinary workspace write
work, not an administrative act.

A runner token is handled separately, as above: scoped to its own run's
workspace, and enforced per request rather than as a capability floor, because a
capability resolver has no way to know which workspace a run belongs to.

---

## Limits

Defensive ceilings, all **per workspace**, all answering `422` with the limit
named in the message:

| Limit | Value |
|---|---|
| Hosts | 5000 |
| Groups | 1000 |
| Variables per host, and per group | 500 |
| Inventory variables (`group_vars/all`) | 500 |
| Host memberships | 20000 |
| Group nestings | 2000 |

They are generous on purpose: a few hundred hosts is an ordinary fleet and
`for_each` over them is the documented shape. They exist so that a runaway
`for_each` fails with a message rather than filling a table one API call at a
time.

Beyond the ceilings: a **duplicate** — a second host of the same name in a
workspace, a second variable with the same key on the same parent, a membership
that already exists — answers **409**, which is what makes a Terraform import the
next step. A **parent that is absent, or in another workspace**, answers **422**:
the foreign keys are composite, so a cross-workspace link is structurally
impossible rather than merely checked for.

Deleting a host takes its memberships and its variables with it, and deleting a
group takes its memberships, its nestings and its variables. That is the
database's own cascade rather than an enumeration in code.

---

## Endpoints

The canonical native prefix is `/api/v1`; `/api/terrapod/v1` is the deprecated
alias and serves all of these too. None of it is on the TFE-compatible prefix —
no `terraform`, `tofu` or `tfci` invocation consumes it.

```
GET  PUT  PATCH  DELETE   /api/v1/workspaces/{id}/inventory/settings
GET  POST                 /api/v1/workspaces/{id}/inventory/hosts
GET  PATCH  DELETE        /api/v1/inventory-hosts/{id}
GET  POST                 /api/v1/inventory-hosts/{id}/vars
GET  PATCH  DELETE        /api/v1/inventory-host-vars/{id}
GET  POST                 /api/v1/inventory-hosts/{id}/groups
GET  POST                 /api/v1/workspaces/{id}/inventory/groups
GET  PATCH  DELETE        /api/v1/inventory-groups/{id}
GET  POST                 /api/v1/inventory-groups/{id}/vars
GET  PATCH  DELETE        /api/v1/inventory-group-vars/{id}
GET  POST                 /api/v1/inventory-groups/{id}/hosts
GET  POST                 /api/v1/inventory-groups/{id}/children
GET  POST                 /api/v1/inventory-groups/{id}/parents
GET  DELETE               /api/v1/inventory-host-groups/{id}
GET  DELETE               /api/v1/inventory-group-children/{id}
GET  POST                 /api/v1/workspaces/{id}/inventory/vars
GET  PATCH  DELETE        /api/v1/inventory-global-vars/{id}
GET                       /api/v1/workspaces/{id}/inventory/resolved
```

A membership and a nesting are **symmetric**, so both sides can create one — a
loop over a group's intended members wants the group route, a loop over a host's
groups wants the host route, and forcing either to invert its loop buys nothing.
The row, the constraint and the response are identical.

Both are created with a **relationship**, not an `*-id` attribute, because they
are links and the house style says a link is a relationship:

```json
POST /api/v1/inventory-groups/invgroup-.../hosts

{"data": {"relationships": {
  "host": {"data": {"id": "invhost-...", "type": "inventory-hosts"}}}}}
```

Request and response shapes for every route are in
[API Reference → Ansible Inventory](api-reference.md#ansible-inventory).

---

## Managing it as code

Eight resources and one data source, in
[the Terrapod provider](terraform-provider.md):

| Resource | What it declares |
|---|---|
| `terrapod_inventory_settings` | The VCS binding, and whether the declared rows take part |
| `terrapod_inventory_host` | One host |
| `terrapod_inventory_group` | One group |
| `terrapod_inventory_host_group` | A host's membership of a group |
| `terrapod_inventory_group_child` | One group nested inside another |
| `terrapod_inventory_host_var` | One `host_vars` entry |
| `terrapod_inventory_group_var` | One `group_vars` entry |
| `terrapod_inventory_global_var` | One `group_vars/all` entry |

The data source **`terrapod_inventory_resolved`** is the merged view. It is a
data source rather than an attribute on a resource because a data source has no
round-trip requirement, so a derived value belongs there — the same reasoning
that keeps a workspace's effective cloud-identity audiences off its resource.

---

## See also

- [API Reference → Ansible Inventory](api-reference.md#ansible-inventory) — request and response shapes
- [Terraform provider](terraform-provider.md) — managing Terrapod's own objects as code
- [RBAC capabilities](rbac-capabilities.md) — the full gate → capability table
- [VCS integration](vcs-integration.md) — the connections a VCS source is bound through
- [Encryption at rest](encryption-at-rest.md) — what the variable columns are covered by
- [Language package proxies](package-cache.md) — how `ansible-core` reaches a sealed deployment
- [Runners](runners.md) — the Job a configure will run in
- Original feature requests:
  <https://github.com/mattrobinsonsre/terrapod/issues/1967> (the inventory),
  <https://github.com/mattrobinsonsre/terrapod/issues/1968> (declaring it as code)
  and <https://github.com/mattrobinsonsre/terrapod/issues/1969> (editing it in the UI)
