# terrapod-query — tofu-native discovery for onboarding existing resources

`terrapod-query` finds existing, unmanaged cloud resources and emits candidate
OpenTofu `import {}` blocks. It is the **discovery engine** for bringing already-
provisioned infrastructure under Terrapod management, and it is modelled after
Terraform's `terraform query` — but built **entirely on native OpenTofu
functionality**, with **no BUSL-licensed Terraform binary and no bespoke
provider-plugin client**. It is MPL-2.0, like the rest of Terrapod.

It performs **no import itself**. It emits import blocks a human (or, later, the
AI onboarding workflow) reviews; the actual import is always a normal, gated
Terrapod run.

## Why it exists

Adopting an existing estate into IaC means writing an `import` block (and
matching config) for every resource — tedious and error-prone by hand.
`terrapod-query` automates the *discovery* half: given a provider and a filter,
it asks the provider (through tofu) what exists and turns the answer into import
blocks.

Everything rides the `tofu` CLI — Terrapod does not reimplement the provider
plugin protocol, because tofu already exposes what discovery needs:

| Step | Native tofu mechanism |
|---|---|
| Find the discovery surface | `tofu providers schema -json` — which data sources accept `filter`/`tags`/name args |
| Run a discovery query | a `data` block + `tofu output -json` — tofu executes the data source natively |
| Generate config for the imports | `tofu plan -generate-config-out` (used by the AI workflow) |

Discovery is **read-only throughout**: the configuration it runs contains only a
`data` block and an `output`, so it issues provider read/`Describe` calls and
never creates, changes, or destroys anything.

## Useful on its own — not just inside Terrapod

`terrapod-query` is a **standalone tool** first and a Terrapod feature second. You
do not need a Terrapod deployment to use it: point it at a provider and your own
credentials and it prints `import {}` blocks you can drop into any OpenTofu (or
Terraform) project. That makes it useful to **OpenTofu users independently**, and
useful to **Terraform users too**.

The reason it works so broadly is that discovery rides **data sources**, not a
provider's dedicated resource-*listing* capability:

- Terraform 1.14 added `terraform query` with `list {}` blocks, but that path
  depends on each provider shipping **provider-defined list resources** — a new
  capability providers are only beginning to adopt. Where a provider hasn't added
  list support for a resource type, `terraform query` has no `list` resource to
  enumerate it.
- `terrapod-query` instead reads the provider **schema** to find data sources
  that accept a `filter`/`tags`/name argument and return a plural/id list, then
  runs an ordinary `data` block. Data sources are long-established and widely
  available across mature providers, so discovery can reach resource types that
  have a data source but no `list` resource.

This is a property of the mechanism, not a benchmark: `terrapod-query` never calls
a provider's `list` resource or `terraform query` at all — it only introspects the
schema, runs `data` blocks, and (for config) `plan -generate-config-out`. So it
doesn't rely on the provider's `list` functionality the way `terraform query`
does; it uses whatever data sources a provider already ships. It complements the
native path rather than replacing it — where a provider *does* offer first-class
list resources, that route is great; where it doesn't, data-source discovery can
still get you import blocks.

## How the effective id is chosen

There is **no declared link** in a provider schema between a data source and the
managed resource it imports into, and no field that says "this attribute is the
import id" — that logic lives in the provider's Go importer, not the schema. So
`terrapod-query` derives both by convention, then confirms what it can and lets
the plan catch the rest:

1. **The id list** — prefer the canonical `ids` list; otherwise the single
   computed string-list attribute whose name ends `_ids`/`_identifiers` (so
   `aws_eips` → `allocation_ids`, `aws_db_instances` → `instance_identifiers`
   are handled with **no per-resource map**). A source with no such list, or an
   ambiguous choice of several, has no derivable id list.
2. **The resource type** — singularise the data-source name (`aws_eips` →
   `aws_eip`), then **confirm it exists** in the provider's `resource_schemas`.
   The name is a convention guess; the existence check is authoritative and
   rejects list sources with no matching managed resource
   (`aws_availability_zones`, `aws_ip_ranges`).

A data source is **importable** only when both hold. `schema --importable`
returns exactly that subset — the onboarding surface — so an operator never
selects a source that would silently find nothing.

## The fail-safe

Even for an importable source the *import identifier* is not guaranteed (some
resources import by a composite key, an ARN, or a `name:region` tuple). Id
derivation is therefore **best-effort**. It fails safe: a mis-derived id shows up
in the plan as a **create/replace instead of an import**, which the operator sees
and does not apply. A wrong guess never mutates infrastructure — the import-only
plan (0 to add, 0 to destroy) is the gate. Emerging provider *resource identity*
schemas will make step 2 above authoritative rather than convention-based once
providers populate them.

## Commands

```
terrapod-query schema [--dir DIR] [--tofu PATH] [--from FILE] [--all] [--importable]
terrapod-query query  --type TYPE --provider-config FILE [--filter NAME=V1,V2 ...] [--arg NAME=HCL ...] [--dir DIR]
terrapod-query import [--from FILE] [--resource TYPE] [--out FILE]
terrapod-query clean  --config FILE [--tofu PATH] [--dir DIR] [--out FILE]
```

**`schema`** introspects the provider schema and prints the discovery surface as
JSON — the data sources usable for filter-based discovery and the arguments each
accepts to narrow the search. By default it lists the strong-signal subset
(sources with a `filter` block, a `tags` argument, or a plural/list return);
`--all` lists every data source with its settable inputs; **`--importable`**
narrows to the sources the import path can actually consume (see *How the
effective id is chosen*) — this is the onboarding surface. `--from` reads an
already-captured `providers schema -json` document instead of running tofu.

**`query`** runs one data-source query via tofu in an ephemeral directory and
prints the structured result — the resources it found — from
`tofu output -json`. `--filter` and `--arg` are repeatable; `--provider-config`
points at a `.tf` file with the provider block(s) and `required_providers`. A
version constraint in that `required_providers` block (e.g.
`version = "< 6.0"`) pins the provider the query runs against — the onboarding
flow uses this so discovery matches the provider major you actually run.

**`clean`** is the deterministic, AI-free cleanup of the config that
`tofu plan -generate-config-out` emits: it drops Computed-only and zero-valued
attributes (using only the schema's required/optional/computed flags) so the
generated config plans import-only. The optional AI mode layers on top of this
for a nicer diff — never for correctness.

**`import`** turns a query result into candidate `import {}` blocks, mapping the
returned ids onto the managed resource type (derived from the data-source name,
or set with `--resource`). Composable via a pipe:

```sh
terrapod-query query --type aws_vpcs --provider-config aws.tf \
  --filter 'tag:env=prod' | terrapod-query import --resource aws_vpc
```

## Where it runs

`terrapod-query` is published as a **standalone cross-platform binary** (like
`terrapod-publish` and `terrapod-migrate`) for local use against your own
credentials, and is also **baked into the Terrapod API and runner images**:

- **Schema introspection** is credential-less and provider-version-stable, so it
  can run **in the API** for a responsive answer without spinning up a runner
  Job. (The API image carries a pinned OpenTofu for exactly this.)
- **Query execution** hits the real cloud with the workspace's identity, so it
  runs in a **runner Job**, the same execution split as every plan/apply.

## AI config polish (optional)

Machine-generated config is correct but hard to read: every resource carries an
opaque label derived from its cloud id (`aws_eip.eipalloc_0ccdb1`). When
`api.config.ai_onboarding.enabled` is set (its own model + endpoint + token
budget, independent of the plan-summary AI), Terrapod can **polish** a discovery
session's generated config so it reads like something a human wrote — resources
**renamed from their tags**, **grouped**, and **commented**.

The polish is deliberately narrow and **safe by construction**: the model returns
only structured naming decisions (a per-resource new-name / group / comment), and
Terrapod applies them to the raw text deterministically. It **never changes an
attribute value or an import id** — those are copied verbatim, and a value-
preservation check runs before anything is stored. If the model's suggestion is
inconsistent (an unknown resource, an invalid or colliding name), the polish is
rejected and the raw config is kept.

The result is stored **alongside** the deterministic output, never replacing it:
the onboarding review screen shows a **Raw ↔ Polished** toggle, and the raw,
guaranteed-import-clean config is always available as the fallback. The import is
still a normal, gated Terrapod run either way. Disable the feature and discovery
behaves exactly as before (raw config only).

## Status

The `terrapod-query` engine (schema → query → import) is complete and shipped as
a binary, and the platform integration that drives it — discovery sessions, D1
schema introspection, runner-side query + `tofu plan -generate-config-out` +
config cleanup, and the optional AI config polish above — is implemented. The
remaining end-to-end step (an AI workflow that chooses what to query, iterates,
and raises a reviewed VCS pull request behind the import-only plan gate) is
tracked in [#824](https://github.com/mattrobinsonsre/terrapod/issues/824).
