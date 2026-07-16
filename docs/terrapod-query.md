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

## Status

The `terrapod-query` engine (schema → query → import) is complete and shipped as
a binary. The platform integration that drives it end-to-end — an AI onboarding
workflow that chooses what to query, iterates, runs
`tofu plan -generate-config-out`, cleans the generated config, and raises a
reviewed VCS pull request behind the import-only plan gate — is tracked
separately in [#824](https://github.com/mattrobinsonsre/terrapod/issues/824).
