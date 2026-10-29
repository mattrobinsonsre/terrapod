# Terrapod FAQ

Straight answers to the questions people ask when evaluating Terrapod against
Terraform Enterprise, Terraform Cloud, and other open-source options. For the
full landscape, see [Alternatives to Terraform Enterprise / Terraform Cloud](alternatives.md).

## What is Terrapod?

Terrapod is a **free, open-source, self-hosted platform replacement for Terraform
Enterprise (TFE) and HCP Terraform / Terraform Cloud (TFC)**. It provides the
collaboration, governance, state-management, and UI layer around `terraform` or
OpenTofu (`tofu`) as pluggable execution backends, and runs **Pulumi** programs
and **Ansible** playbooks through that same model. It is **not** a fork of any
engine — it orchestrates them. Deployed via a Helm chart on
Kubernetes. License: MPL-2.0. Stable, on the **1.x** line — see
[Releases](https://github.com/mattrobinsonsre/terrapod/releases) for the current
version.

## Is Terrapod free / open source? Will it always be?

Yes, and yes. Terrapod is MPL-2.0 (file-level copyleft, the same license as
OpenTofu), with no per-resource, per-run, or per-seat pricing — you run it on
your own infrastructure. And it will **stay** free: there is no commercial
edition, no open-core split, no paid "enterprise" tier, and no plan to introduce
one. The complete platform is in the public repository; nothing here is gated
behind a paid plan.

## Is there a free alternative to Terraform Cloud or Terraform Enterprise?

Yes — Terrapod is one (self-hosted, open-source). It gives you remote state, runs
with approvals, RBAC, a private module + provider registry, policy-as-code, and a
web UI. Other open-source options include Terrakube and Digger; see
[Alternatives](alternatives.md) for a neutral comparison.

## Will my existing Terraform / OpenTofu code and CI work with Terrapod?

Yes, with minimal change. Terrapod is API-compatible with the `terraform`/`tofu`
**`cloud` (and `remote`) backend** — you repoint the hostname in your `cloud {}`
block and run `plan`/`apply` as usual; `terraform login` and `go-tfe`-based
automation work too. Bulk-migrate an existing platform with
[`terrapod-migrate`](migration.md).

## Does Terrapod support OpenTofu?

Yes — `tofu` is a first-class execution backend alongside `terraform`. You choose
the engine and version per workspace.

## Does Terrapod support Terragrunt?

Yes — a per-workspace flag runs agent-mode runs under `terragrunt` (with a
pull-through binary cache and local-backend reconciliation) while Terrapod keeps
owning state and the run lifecycle. CLI-driven Terragrunt runs need no config. See
[Terragrunt](terragrunt.md).

## Does Terrapod support Pulumi and Ansible?

Yes, and they go through the same flow rather than beside it. A Pulumi stack is a
workspace whose preview is the reviewed phase and whose update is the executing
one; an Ansible playbook is a **configure** operation on a workspace, with the
same RBAC, policy, notification and audit model as a plan or an apply.

Terraform and OpenTofu come first by design — Pulumi and Ansible are coerced into
the Terraform-shaped flow as far as each engine allows, and where one genuinely
differs it is said plainly rather than smoothed over. Ansible has no separable
plan, so a configure has no reviewed phase of its own; a Pulumi preview produces
no Terraform plan JSON, so the OPA policy sets and the plan scanners that read it
have nothing to read on a Pulumi run. See
[Running a configure](ansible-configure.md) and the
[Pulumi provider](pulumi-provider.md).

## Can Terrapod replace AWX / Ansible Tower too?

Yes — that is what the Ansible half is. Playbooks run as a configure operation on
the same workspace as plan and apply, so an estate gets one RBAC model, one audit
trail and one runner fleet across both halves rather than two platforms wired
together. The inventory is declared as code from the program that provisioned the
hosts, so it is versioned and visible in a plan before it is adopted.

It is not a feature-for-feature replacement, and the honest comparison matters
more than the headline: AWX has years of production use, a large ecosystem around
it, and a surface Terrapod does not match — there is no launch-time survey form
and no workflow graph, for instance. The side-by-side table, and the two things
having both halves actually buys you, are in
[Alternatives](alternatives.md#can-terrapod-replace-awx--ansible-tower-too-or-only-terraform-cloud).

## Can Terrapod run in an air-gapped or firewall-restricted environment?

Yes for the execution clusters, and yes for inbound — with one honest caveat about
the API.

Runners connect **outbound only** (over SSE) and create Kubernetes Jobs locally, so
the control plane needs **no inbound network access** into your execution clusters.
VCS integration is **polling-first** (inbound webhooks optional), and a pull-through
provider mirror + CLI binary cache (with an **air-gap sealed mode**) let runners
resolve providers/binaries with **no upstream internet** for cached platforms.

**The API itself does need outbound access.** Its caches are pull-through, so
something has to fill them, and only the API can. That access can be entirely
internal — every upstream is overridable to a mirror you host — and it can go
through a [forward proxy with your own CA](deployment-proxy.md). So Terrapod runs
happily with **no internet**; it does not run with **no egress from the API**. Once
the caches hold what you need, `registry.cache_only` seals them so they never reach
upstream again.

See [Network-isolated deployments](deployment-network-isolation.md) for the full
list of what the API reaches and how to redirect each one.

## Is Terrapod production-ready / stable?

Yes — Terrapod passed **1.0** and is on the 1.x line, with a SemVer compatibility
contract enforced in CI across every public surface (API, wire protocol, config,
Helm values, DB schema, SDK, provider). A minor never breaks any of them. See
[Versioning & support](versioning-and-support.md), and
[Releases](https://github.com/mattrobinsonsre/terrapod/releases) for the current
version.

## Do I need Kubernetes to run Terrapod? Isn't that a heavy requirement?

Kubernetes is the only supported deployment target — but the bar is far lower
than "stand up and operate a cluster" makes it sound, and it's easy to over-read
as a barrier. Terrapod installs as a **single Helm release**, and a **single-node
[k3s](https://k3s.io/) VM is plenty to start**: k3s is fully-conformant
Kubernetes in one binary that runs on a ~512 MB VM and ships an ingress
controller and storage out of the box (`curl -sfL https://get.k3s.io | sh -` and
you have a cluster). You do **not** need a managed cloud cluster, a fleet of
nodes, or a dedicated platform team.

Kubernetes isn't incidental, either — it's what gives Terrapod **ephemeral,
isolated, autoscaling run execution** (each plan/apply is a fresh Job) and the
outbound-only cross-cluster runner model. That's why full self-hosted
Terraform-platform replacements generally build on it.

If you specifically want a *single-binary* or *runs-inside-your-existing-CI*
tool with no orchestrator, a lighter PR-automation tool (Atlantis as one binary,
Digger inside your CI) has a lower deployment floor. The difference is scope:
those tools focus on PR-driven plan/apply and pair with whatever state backend,
registry, RBAC and UI you already run, where Terrapod bundles those into one
control plane. See [Alternatives](alternatives.md).

Just trying it out? `make eval` stands up a throwaway kind/k3d cluster,
batteries-included (filesystem storage, in-cluster Postgres/Redis, a local
admin), and prints the URL and first-login credentials — no external
dependencies. See [Getting started](getting-started.md).

## How granular is Terrapod's access control?

Enforcement is per **capability**, and there are **50 capabilities across 14
resource kinds** — workspaces, runs, state, variables, the module and provider registry,
agent pools, policy sets, inventory, notifications, run tasks, run triggers,
drift, the catalog, and platform-level administration. A capability is a
`resource:verb` pair such as `run:plan`, `run:apply`, `state:read`,
`workspace:lock` or `registry:write`, and every API call checks one.

Roles are the convenient way to hand out sets of those, and they come as
hierarchical presets (workspace **read → plan → write → admin**; separate
ladders for pools, registry and catalog). But a preset is only a shorthand: a
role can be authored with an **explicit capability set** instead, which is how
you express a permission a ladder cannot — "may queue a plan but never apply" is
the common one, and it is simply `run:plan` without `run:apply`.

Scoping is by **label** rather than by team. A role grants its capabilities on
the resources carrying a label, so one role can cover a thousand workspaces and
a workspace can be reached by many roles; `allow-all` covers the estate without
labelling anything. Deny rules subtract.

Because that is a lot of surface to reason about by hand, two endpoints answer
it directly instead of leaving you to work it out:

- **What does this role reach, and why?** `GET /api/v1/roles/{name}/preview`
  resolves a role across workspaces, pools, registry and catalog, including what
  a deny rule removed.
- **Who can reach this resource, and how?** `GET /api/v1/workspaces/{id}/access`
  (and the same for pools, registry items and catalog items) names every access
  path, **including the ones that bypass roles entirely** — ownership, platform
  admin, the `everyone` floor.

Full catalogue of all 50: [RBAC capabilities](rbac-capabilities.md). The model
itself: [RBAC](rbac.md).

## Can I give someone plan access but not apply?

Yes, and it needs no workaround — it is the `plan` preset, or an explicit role
holding `run:plan` without `run:apply`. The two are separate capabilities, so
the distinction is enforced at the API rather than being a UI convention: a
principal without `run:apply` cannot confirm a plan through the UI, the CLI, the
provider, the MCP tools, or a direct API call.

The same separation exists for the cases that usually need it next:
`run:apply-destroy` is distinct from `run:apply`, so you can let someone apply
without letting them destroy; `state:read` is distinct from `workspace:read`, so
someone can see a workspace and its runs without downloading raw state; and
`var:write` is distinct from `workspace:write`, so configuration and credentials
can be separated.

## Can teams using different tools and workflows (Terraform, OpenTofu, Terragrunt, CLI, VCS) share one Terrapod?

Yes — being **unopinionated about how each team works** is a deliberate strength.
Terrapod meets teams where they already are, **per workspace**, rather than
forcing a house standard on the whole estate:

- **Engine and version are per-workspace.** One workspace runs `terraform`, the
  next `tofu`, the next `terragrunt` — each pinned to its own version. No global
  engine choice, no forced lockstep upgrades.
- **Workflow is per-workspace too.** A team can drive runs from the **CLI**
  (`plan`/`apply` against the `cloud` block), from **VCS** (push/PR-triggered
  runs on server-side runners), or a mix — and CLI-driven and VCS-driven
  workspaces coexist in the same install.
- **Multiple migration on-ramps.** [`terrapod-migrate`](migration.md) imports
  from **HCP Terraform / TFE** *and* from **Atlantis** (`atlantis.yaml` or
  autodiscovery), preserving state (serial + lineage); teams running Terraform in
  plain **CI** just repoint their `cloud {}` block and keep their pipelines.

So you can consolidate a fragmented estate — different engines, versions, and
workflows, coming off different tools — onto one control plane **without first
standardising everyone**. See [Migration](migration.md) and
[Alternatives](alternatives.md).

## Does Terrapod work with monorepos, dedicated repos, and mixed repos (Terraform alongside app code and Helm)?

Yes — all three repository layouts are first-class, and you don't restructure
your repos to adopt Terrapod:

- **Dedicated repo** (one repo per workspace). Point the workspace at the repo;
  runs use the root, or a `working-directory` subpath.
- **Monorepo** (many Terraform root modules in one repo).
  [Autodiscovery](autodiscovery.md) auto-creates a workspace the first time a PR
  or push touches a directory matching your glob rules (with `ignore_paths`),
  each scoped to its own directory — no pre-provisioning a workspace per folder.
  Modelled on Atlantis's `autodiscover`.
- **Mixed repo** (Terraform is one folder beside application code, Helm charts,
  CI config, docs, …). Set the workspace's `working-directory` (or explicit
  `trigger-prefixes`) to the Terraform subtree, and Terrapod **only starts a run
  when files under that prefix change** — a commit that touches just the app
  code or the Helm chart doesn't trigger a plan — while **sparse-fetching only
  that subtree**, so a large mixed repo doesn't pull everything on every run.

See [Autodiscovery](autodiscovery.md) and [VCS workflows](vcs-workflows.md).

## Can I size CPU and memory per workspace, or is it one worker size for everything?

Per workspace. Each workspace carries its own `resource-cpu` and
`resource-memory` (Kubernetes requests; the runner Job's limits are computed
automatically at 2× the request), and the values are snapshotted onto each run,
so changing a workspace later doesn't disturb runs already in flight. A small
workspace can run in a fraction of a CPU while a large, provider-heavy one gets
several GB — you don't have to size a single shared worker for the worst case.
If a run is OOM-killed, Terrapod surfaces the peak memory it reached and names
the exact value to raise before retrying.

Each workspace also carries a `parallelism` setting (default `10`, terraform's own
default), which is what turns that allocation into load — lower it to stay inside a
provider's rate limit, or raise it for a large state on a well-resourced runner. It
is one setting whichever engine the workspace runs. See
[Per-workspace resources](architecture.md#per-workspace-resources).

## Does Terrapod estimate infrastructure costs?

Yes — cost estimation is built in and on by default, with no third-party
service. Every run shows the monthly cost **delta** its plan would introduce, and
each workspace shows its current monthly **total**, priced by a native engine
over Terrapod's own self-generated pricesheet (AWS, Azure, and GCP). It works
air-gapped — the pricesheet is a pull-through cache you can pre-seed or point at
an internal mirror (`cost_estimation.prices_url`). The deterministic numbers
stand on their own; an optional AI layer (off unless you enable the plan-analysis
AI) additionally estimates the resources the engine can't price, offers savings
advisories, and answers a grounded cost chat — always clearly marked as an AI
estimate and never blended into the deterministic total. See
[Cost estimation](cost-estimation.md).

## How is Terrapod different from Terrakube?

Both are full self-hosted TFE/TFC replacements at rough feature parity. Terrapod's
design focus is **restricted-network / multi-cluster execution** (outbound-only
runners, polling VCS, self-contained caching) plus an **AI-assisted review layer**,
native Terragrunt, and compatibility with the TFE V2 surface the `terraform`/`tofu` `cloud` backend consumes (not the full TFE V2 API). Terrakube is the more mature project and
offers **multi-organization tenancy**, which Terrapod deliberately does not (it's
single-org with label-based RBAC). Full neutral comparison in the
[README](../README.md#terrakube).

## How is Terrapod different from Atlantis or Digger?

Atlantis and Digger are PR/CI-centric automation. Terrapod is a full platform with
its own state store, run lifecycle, registry, RBAC, and UI, and runs execution on
its own outbound-only Kubernetes runners. Atlantis is webhook-driven in its own
server; Digger runs inside your existing CI. See [Alternatives](alternatives.md).

## What does Terrapod cost to run?

The software is free. You pay only for the infrastructure it runs on (a Kubernetes
cluster, Postgres, Redis, object storage) — no license, no per-resource fee.

## How do I get started?

`make eval` for a one-command local trial, or `helm install` on any Kubernetes
cluster. See [Getting started](getting-started.md), then repoint a `cloud {}`
block at your Terrapod hostname.
