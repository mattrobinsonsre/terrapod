# Ansible inventory

A Terrapod workspace can carry an **inventory**: an ordered set of sources that
resolve into one host and group set, in the shape ansible consumes. The primary
source is the workspace's **own Terraform**, which declares each host as a
`terrapod_inventory_item` resource — so the inventory is produced by the apply
that produced the infrastructure, and nothing has to infer it afterwards.

> ## Status: the inventory exists; configure operations do not
>
> **This release delivers the inventory object, the declared hosts, the merge,
> what they resolve to, and the surfaces that make all of it observable. It does not run
> playbooks.** There is no configure operation, no playbook reference, and no
> way to execute ansible from Terrapod today.
>
> What you can do now is **declare** an inventory and **see what it resolves
> to**, including which hosts a `--limit` pattern would select. That ordering is
> deliberate rather than partial: an operator has to be able to answer "what
> would a configure target" before anything is able to target it.
>
> A workspace that declares no host carries **no inventory rows at all**. The
> `default` inventory is created lazily on the first write, so a
> Terraform/OpenTofu-only deployment pays nothing for any of this — by data
> rather than by a feature flag.

---

## Why Terraform declares the hosts, rather than Terrapod deriving them

The obvious design is the other way round: a workspace has state, state names
resources, so Terrapod could read the state and work out the hosts. That was
considered and **withdrawn**, and the reason is worth stating because it decides
the whole shape of the feature.

Deriving hosts from state needs a mapping — which resource *types* are hosts,
which *attribute* on each is the address, and how a host lands in a *group*. That
mapping is unbounded (every provider, every resource type, every release),
permanently incomplete, and wrong in ways an operator **cannot fix without
Terrapod shipping a change**. The failure mode is the worst one available: a
target set that is silently too small, because the resource type carrying the
hosts was not in a table nobody could see.

Declaring in HCL deletes the problem. The operator already has `for_each`,
`count`, conditionals, every attribute of every resource, module outputs and
real dependency ordering — all of which are more expressive than any table
Terrapod would write. There is no "that resource type is not supported yet",
because Terrapod is not the one reading it.

The cost is a resource per host, which `for_each` makes one block.

---

## The two patterns

Both are first class. The first is the on-ramp, and it is the one most likely to
be wanted first.

### Pattern A — inventory from existing machines, with no changes at all

A workspace whose configuration is **nothing but data sources**. It reads the
machines that already exist, and declares inventory from what it finds. It
creates nothing, changes nothing and adopts nothing into Terraform management.

```hcl
# No resources. Nothing is created, changed or adopted.
provider "terrapod" {}

data "aws_instances" "app" {
  instance_tags   = { Role = "app" }
  instance_state_names = ["running"]
}

data "aws_instance" "app" {
  for_each    = toset(data.aws_instances.app.ids)
  instance_id = each.key
}

resource "terrapod_inventory_item" "app" {
  for_each = data.aws_instance.app

  name    = each.value.tags["Name"]
  address = each.value.private_ip
  groups  = ["app", "linux"]

  vars = {
    ansible_user = "ec2-user"
  }
}
```

An apply of that workspace writes inventory rows and touches no infrastructure.
It is ansible targeting over an existing fleet of long-lived machines, with no
Terraform adoption and no migration — which is the case that would otherwise
have to wait for the whole estate to be brought under IaC first.

### Pattern B — provision, then declare, in one configuration

The host is created and declared in the same configuration, so the ordering is
**implicit**: the inventory item references the instance, so Terraform builds the
instance first, and the apply is what writes the inventory.

```hcl
provider "terrapod" {}

resource "aws_instance" "web" {
  for_each = var.web_nodes
  # ...
}

resource "terrapod_inventory_item" "web" {
  for_each = aws_instance.web

  name    = each.key
  address = each.value.private_ip
  groups  = ["web"]
}
```

No `depends_on` is needed or wanted. The reference to `each.value.private_ip` is
the dependency, and it is the reason the inventory cannot be written before the
host it describes exists.

---

## Host variables are not a secret store

**A host variable is stored in the clear and returned in full to anyone holding
`inventory:read` on the workspace.** There is no `sensitive` flag, no encryption
at rest and no masking on read. `inventory:read` is in the **read** tier, so
every read-level user of that workspace can see every host variable.

This is unlike a [workspace variable](api-reference.md#variables), which can be
marked sensitive, is encrypted at rest and is never returned once it is.

**So do not put a credential in a host variable.** The reach for it is natural —
`ansible_password`, `ansible_become_password` and `ansible_ssh_pass` are ordinary
host vars in any ansible tutorial — and that is exactly why it is worth stating
before you design an inventory rather than after.

Credential delivery for a configure is a separate mechanism and it is **not
available yet** — it lands with the configure operation itself. There is no way
today to reference a workspace variable from a host variable, so treat host
variables as what they are: non-secret inventory metadata — users, ports,
connection settings, role parameters.

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
end up dialling the runner pod's own loopback. `TERRAPOD_HOSTNAME` is named for
a hostname and accepts a full URL, so a complete URL passes through untouched.

What makes the run's own token sufficient is a single implicit grant: **a runner
token may manage the inventory items of its own run's workspace**, and nothing
else. It is the same shape as the implicit registry read a runner token already
carries, and for the same reason — an apply cannot work without it. Reads are
unphased, because a plan reads the inventory to diff it; **writes are bound to
the apply phase**.

### Two exceptions, both deliberate

**Local execution mode is untouched.** There is no runner token and nothing in
the runner runs, so the provider needs a host and a token configured exactly as
it does today. The implicit grant is a property of running *inside* a run.

**If you set either variable yourself, Terrapod exports neither.** A workspace
`env` variable named `TERRAPOD_HOSTNAME` or `TERRAPOD_TOKEN` is a legitimate
thing to have — it is how an operator drove the provider from inside a run
before this existed — so Terrapod stands aside and logs that it has done so.
The two decisions are taken **together**, never separately: supplying one while
deferring to a workspace variable for the other is the arrangement that would
send the run's real credential to a host the variable chose. So it is
all-or-nothing by design, not by omission.

If the provider is then left without what it needs — you set the hostname and
not the token, say — it fails with its own missing-credential error naming the
variable it wants, which is the right place for that to surface.

---

## Sources, and the order they resolve in

An inventory holds an ordered list of sources. Only one kind is implemented:

| Kind | What it resolves to | Resolvable by the API? |
|---|---|---|
| `terraform` | Every inventory item this workspace declares | **Yes** — it is a database query |

Every inventory is created with a `platform` source at **position 0**. Others
are appended after it, and that is also their precedence order.

**The kind names the source, not whoever writes it.** `platform` is the static
inventory Terrapod holds — the `inventory_items` rows. Terraform is the first
writer of those rows and a hand editor in the UI writes the same ones, so a
second writer is not a second source. **There are two sources in total**: this
and git-supplied inventory files, which are separate work (external and dynamic
inventory was declined). The source table exists now — rather than the
`platform` source being implied — precisely so git has a position to occupy
without a migration that reorders anything.

**Every kind Terrapod implements is static.** An external, AWX-style source that
executes a script or a plugin to discover hosts is not among the kinds to come:
it was considered and declined, and
[the reasoning is recorded](https://github.com/mattrobinsonsre/terrapod/issues/1970).
That is what makes a read of the resolved view live.

**Declared items are workspace-scoped, not inventory-scoped.**
`terrapod_inventory_item` carries no `inventory` attribute, so an item belongs to
the workspace and every inventory's `platform` source draws on the same pool.
Several inventories per workspace are supported because a configure definition
references *an* inventory: "the declared items plus file A" and "the declared
items plus file B" are two different target sets over one workspace.

### Merge semantics are ansible's own

**Terrapod implements no merge scheme of its own.** The ordered source list is an
`-i` ordering and nothing more. The rules below were measured on **ansible-core
2.18.3** with a YAML file, an INI file and an executable script supplied
simultaneously:

- **Hosts union** across sources.
- **Group membership unions** — a group named by two sources holds the hosts
  from both.
- **A conflicting host variable goes to the later source.** Proven in both
  directions: `-i a -i b` gave B's value, `-i b -i a` gave A's.
- **A non-conflicting variable from an earlier source survives** alongside the
  later source's values. The merge is per variable, not per host — a later source
  does not replace a host's variables wholesale.

Position 0 resolves first, so **a higher position wins** a conflict. An
operator's existing mental model of `-i` ordering transfers exactly.

---

## `address` and `ansible_host`

`address` is a convenience for the `ansible_host` variable, and it is optional —
a host whose own name already resolves needs none.

**An explicit `ansible_host` in `vars` wins.** An operator writing the raw
variable is deliberately reaching past the convenience field, and silently
overriding them would make `vars` a lie:

```hcl
resource "terrapod_inventory_item" "jump" {
  name    = "jump"
  address = "10.0.0.5"           # ignored, because vars is more specific
  vars    = { ansible_host = "jump.internal" }
}
```

The fold happens **at resolution**, not at write time, so the stored row keeps
saying exactly what the operator declared and the derived variable cannot drift
from it. Read the item back and `address` is still `10.0.0.5`; read the resolved
inventory and `ansible_host` is `jump.internal`.

Terrapod gives `ansible_host`, `ansible_user` and the rest no other special
meaning, because ansible does not either.

---

## Names: what is accepted, and why the rest is refused

### Group names

`all` and `ungrouped` are **derived by ansible**, not declared — `all` holds
every host and `ungrouped` holds the hosts in no other group — so a source
cannot control either one's membership and both are refused as declared names.
Terrapod computes them when it renders ansible's shape.

Otherwise a group name must start with a letter or underscore and contain only
letters, digits and underscores. Ansible tolerates more than that and warns, but
a name it warns about cannot be used reliably in a `--limit` pattern or as a
`group_vars` filename, so it is refused at the boundary instead of stored and
regretted.

### Host names

`--limit` has its own operators and separators, and a host named with one is
not merely ugly — it is **unusable, or worse**:

| Character | What `--limit` does with it |
|---|---|
| `,` `:` | Separate patterns |
| `!` | Exclude |
| `&` | Intersect |
| `~` | Introduce a regular expression |
| whitespace | Splits terms |

So a host called `!web` can never be selected **and silently excludes `web`
from any pattern that names it**. All of the above are refused at the write, with
a message naming the offending character and the reason.

Everything else is fine, which includes the shapes an operator actually wants:
letters, digits, dots, hyphens and underscores. Name hosts the way you would
name them in an inventory file — `web-01`, `db.internal`, `app_eu_west_1a` — and
nothing here applies.

### Host variable names

Deliberately laxer: a variable name only has to be a non-empty string. Ansible
stores a variable whose name is not an identifier and only warns that it is
unreachable as `{{ name }}` — it is still readable via
`hostvars['h']['odd-name']`, which some roles do on purpose. Refusing more would
block working configurations to prevent a warning.

**A declared host variable's value must be a string, and the API refuses one
that is not** with a `422` naming the key. That is narrower than ansible's own
rule, deliberately, and for a reason that is worth knowing rather than working
around:

- a declared item is the flat surface the managing Terraform owns, and a
  Terraform map is homogeneous — `map(string)` — so the provider has no shape
  for a nested object;
- every client decodes these as strings, and a decoder fails the **whole** map
  on one non-string value. Storing `{"role": "frontend", "port": 8080}` would
  make the SDK, the provider and the MCP tools report **no variables at all**
  for that host — not the one odd entry, all of them — with nothing saying why.

So the refusal replaces a silent whole-set disappearance with a message naming
the key. A variable that genuinely has to be a list, a number or a nested
object belongs in the playbook repository's `group_vars` or `host_vars`, which
is where an ansible operator keeps such a thing anyway.

**A resolution is not subject to this.** It carries whatever ansible
actually produced, rich values included — flattening it would lose the shape a
configure needs. The rule is only about what Terraform declares.

---

## A read is live, and writes nothing

**Every source Terrapod implements is static** — declared rows the workspace's
own Terraform owns — so resolving one is a database query with nothing to fetch,
parse or time out. A read of the resolved view is therefore **live**: Terrapod
resolves the rows to answer the request. There is no cached-versus-fresh
distinction, no timestamp beside the result, and no staleness for a reader to
reason about.

That follows from a decision rather than from an optimisation. **Dynamic
inventory was considered and declined** — an external, AWX-style source that
executes a script or a plugin to discover hosts. The reasoning is recorded in
[#1970](https://github.com/mattrobinsonsre/terrapod/issues/1970), and it is the
same reasoning as for deriving hosts from state: Pattern A above already covers
the case a plugin was wanted for, and covers it better. A data source plus
`for_each` plus `terrapod_inventory_item` is what an inventory plugin gives you,
except versioned in git, reviewed in a pull request and visible in a plan. A
dynamic source is also arbitrary code execution, which ansible performs whether
it is invoked through the CLI or through its Python API.

**So there is nothing to refresh, and no endpoint that refreshes it.** An
earlier shape stored each resolution and compared an equality token to decide
whether to serve the stored copy; with every source static there is no second
answer such a token could distinguish, so the whole mechanism is gone rather
than kept as a no-op.

### The target set a configure runs against is a separate thing

A *reader* needs a current answer, which is what the above gives. A *configure*
needs something different: a record of exactly which hosts it targeted, fixed
for the life of the run, so a partial failure can be recovered from.

That is forced by measurement rather than chosen. Under `serial:`, ansible's
`v2_playbook_on_play_start` fires once per batch, and in a `serial` +
`any_errors_fatal` abort the untouched hosts appear in **no callback event and
in no PLAY RECAP at all**. So "hosts targeted minus hosts completed" cannot be
computed from parsed results — a retry built that way fixes the failure, reports
success, and silently leaves the rest unconfigured.

So that record belongs to the **run**, written when the run is created, rather
than to the inventory. Configure operations do not exist yet
([#1971](https://github.com/mattrobinsonsre/terrapod/issues/1971),
[#1972](https://github.com/mattrobinsonsre/terrapod/issues/1972)), and the
recovery that subtracts against it is
[#1973](https://github.com/mattrobinsonsre/terrapod/issues/1973).

### The exhaustive host map, and the trap it removes

The resolved view holds **every** host explicitly, including a host with no
variables at all. `ansible-inventory --list` does not: its `_meta.hostvars`
**omits a var-less host entirely** — measured, a host appearing only in a group
was absent from `_meta.hostvars`. Code that enumerates the host set from that
shape loses hosts silently, which is the shape of bug that empties a target set.

Ansible's own shape is still available: the resolved read carries an
`ansible-inventory` attribute rendered on demand from the normalised one, with
`_meta.hostvars`, an `all` group whose `children` name every other group, and
`ungrouped` for the hosts no declared group claims — and with every host present
in `_meta.hostvars`. One source of truth, rendered two ways, rather than two
representations to keep in step.

---

## The `--limit` preview

```
POST /api/v1/inventories/{id}/actions/preview-limit
```

Answers "which hosts would this `--limit` select". Read-only and read-gated. It
expands the forms an operator writes in a limit: host names, group names, `all`
and `*`, comma- or colon-separated terms, globs, `!` exclusion and `&`
intersection. An inclusion term unions, except that the first term starts from
nothing rather than from the whole inventory.

**It expands against a live resolution**, by the same route as the resolved
view: previewing against a target set that has since changed is the wrong answer
in the one place an operator came to check. There is always something to limit
against, including the empty set of a workspace that has declared no host.

And two caveats, both of which matter more than they sound:

**It is advisory.** The authoritative expansion is always
`ansible-inventory --list --limit`, taken in the runner at the start of a
configure. This is a preview so the question can be asked before a Job exists.

**A `~regex` term is refused, with `422`.** Ansible will apply the regular
expression at run time; quietly matching nothing here would show an empty target
set for a pattern ansible would have expanded — the wrong answer dressed as an
answer. Declining is the honest result.

The response carries `of-host-count` from the resolution it expanded against,
so an operator can see how much of the inventory the pattern selects.

---

## Every source the API resolves itself

The `platform` source needs no ansible: the declared items are already rows
Terrapod owns, so resolving that source is a query with nothing to fetch, parse
or time out. It is also the only source kind there is — **dynamic inventory was
declined** ([#1970](https://github.com/mattrobinsonsre/terrapod/issues/1970)),
so nothing here needs a runner to parse and no read can be served from anywhere
but the rows themselves.

That is what makes the whole surface simple to reason about: there is no
resolvable-versus-not distinction on a source, no refusal for an inventory the
API cannot handle, and no second place a resolution could come from.

Git-supplied inventory files and directories are
[#1929](https://github.com/mattrobinsonsre/terrapod/issues/1929), and are static
too — a file in a repository at a resolved commit, not a script that runs. What
that issue adds is a **stored** source stamp and a short expiry, because a
`platform` stamp is *derived* from the rows it summarises and a derived stamp
cannot be stale, whereas a stamp you can only learn by fetching — git's resolved
commit — is the one case an expiry buys anything.

---

## Permissions

Two capabilities in the workspace axis:

| Capability | Tier | Grants |
|---|---|---|
| `inventory:read` | **read** | List and show declared hosts, inventories, the resolved view, and the limit preview |
| `inventory:write` | **write** | Declare, change and remove hosts; create and delete inventories |

Both are granted by the existing presets, so no role became more or less
powerful when they were introduced.

**`inventory:write` is in the write tier, not admin, and that is deliberate.**
The Terraform that declares a host runs under an apply, and `run:apply` is a
write-tier capability — so an API gate stricter than the path every item actually
arrives by would be incoherent. Declaring a host is ordinary workspace write
work, not an administrative act.

A runner token is handled separately: it may manage the inventory items of its
own run's workspace and nothing else, enforced per request rather than as a
capability floor, because the grant is scoped to one workspace and a capability
resolver has no way to know which.

See [RBAC capabilities](rbac-capabilities.md) for the full gate table.

---

## Removing hosts

`tofu destroy` (or `terraform destroy`) removes inventory items one resource at a
time, because each host is its own resource in state. **The consequence is worth
knowing rather than discovering: destroying a workspace's inventory empties the
target set of every configure definition that reads it.**

Deleting an *inventory* does not delete the hosts. An item belongs to the
workspace, not to any one inventory, and is owned by the Terraform that declares
it — an inventory is a view over them, so removing the view cannot remove the
hosts. Deleting an inventory does delete its sources.

---

## Limits and validation

| Limit | Value | On breach |
|---|---|---|
| Declared hosts per workspace | 5000 | `422` |
| Duplicate host name in a workspace | not allowed | `409` |
| Duplicate inventory name in a workspace | not allowed | `409` |

The host ceiling is generous on purpose: a few hundred hosts is an ordinary fleet
and `for_each` over them is the documented shape. It exists so that a runaway
`for_each` fails with a message rather than filling a table one API call at a
time.

---

## Endpoints

The canonical native prefix is `/api/v1`; `/api/terrapod/v1` is the deprecated
alias and serves all of these too. None of this is on the TFE-compatible prefix
— no `terraform`, `tofu` or `tfci` invocation consumes it.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/v1/workspaces/{id}/inventory-items` | `inventory:read` | The hosts this workspace declares. Paginated |
| `POST /api/v1/workspaces/{id}/inventory-items` | `inventory:write` (apply phase for a runner token) | Declare a host. `201` |
| `GET /api/v1/inventory-items/{id}` | `inventory:read` | One declared host |
| `PATCH /api/v1/inventory-items/{id}` | `inventory:write` | Partial update: an absent attribute is left alone, an empty list clears |
| `DELETE /api/v1/inventory-items/{id}` | `inventory:write` | Remove a declared host. `204` |
| `GET /api/v1/workspaces/{id}/inventories` | `inventory:read` | This workspace's inventories. Empty for a workspace that has never declared a host |
| `POST /api/v1/workspaces/{id}/inventories` | `inventory:write` | Create a named inventory, with its `platform` source at position 0. `201` |
| `GET /api/v1/inventories/{id}` | `inventory:read` | One inventory, with its ordered sources |
| `DELETE /api/v1/inventories/{id}` | `inventory:write` | Delete an inventory and its sources. Declared items are untouched. `204` |
| `GET /api/v1/inventories/{id}/resolved` | `inventory:read` | What the inventory resolves to. **Live** — Terrapod resolves the rows to answer the request and writes nothing |
| `POST /api/v1/inventories/{id}/actions/preview-limit` | `inventory:read` | Which hosts a `--limit` pattern would select, against a live resolution. `422` on a `~regex` term; `409` only when no source is one the API owns and no runner has posted a resolution |

Typed id prefixes: `invitem-` for a declared host, `inv-` for an inventory,
`invsrc-` for a source.

---

## See Also

- [API Reference → Ansible Inventory](api-reference.md#ansible-inventory) — request and response shapes
- [Terraform provider](terraform-provider.md) — managing Terrapod's own objects as code
- [RBAC capabilities](rbac-capabilities.md) — the full gate → capability table
- [Package Proxies](package-cache.md) — the pull-through path that is why ansible lives only in the runner
- [Runners](runners.md) — the Job a resolve and, later, a configure runs in
- Original feature requests: <https://github.com/mattrobinsonsre/terrapod/issues/1967> (the inventory) and <https://github.com/mattrobinsonsre/terrapod/issues/1968> (declared hosts)
