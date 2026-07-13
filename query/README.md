# terrapod-query

**tofu-native discovery for onboarding existing cloud resources into OpenTofu.**

`terrapod-query` finds existing, unmanaged cloud resources and emits candidate
`import {}` blocks. It is the discovery *engine* — the mechanical primitive that
Terrapod's AI onboarding workflow ([#824](https://github.com/mattrobinsonsre/terrapod/issues/824))
drives — and it decides nothing about strategy and performs no imports itself.
The actual import is always a normal, gated Terrapod run.

It is modelled after Terraform's `terraform query`, but built entirely on native
OpenTofu functionality — **no BUSL-licensed Terraform binary and no bespoke
provider-plugin client**. Everything rides the `tofu` CLI:

| Step | Native tofu mechanism |
|---|---|
| Discover the surface | `tofu providers schema -json` — which data sources accept `filter`/`tags`/name args |
| Run a discovery query | a `data` block + `tofu output -json` — tofu executes the data source natively |
| Generate config | `tofu plan -generate-config-out` (in #824) |

Discovery is **read-only throughout**: data sources only issue provider `Describe`-style
reads; they never create, change, or destroy infrastructure. The `import {}`
block it emits is a *proposal* — a mis-derived id surfaces as a create/replace in
the plan (not an import), which the operator sees and doesn't merge. Wrong
guesses fail safe.

## Where it runs

- **Primary home:** baked into the Terrapod **runner image**, invoked as the
  runner's discovery mode — that's where tofu, the provider mirror, the
  workspace's cloud (CSP) identity, and cloud egress live. Discovery is
  API-orchestrated but runner-executed, the same split as every plan/apply.
- **Standalone:** published as a cross-platform binary (like `terrapod-publish`
  / `terrapod-migrate`) for local use against your own credentials.

## Commands

```
terrapod-query schema [--dir DIR] [--tofu PATH] [--from FILE] [--all]
```

`schema` introspects the provider schema and prints the discovery surface as
JSON: the data sources usable for filter-based discovery, and the arguments each
accepts to narrow the search. By default it lists the strong-signal subset
(sources with a `filter` block, a `tags` argument, or a plural/list return);
`--all` lists every data source with its settable inputs. `--from` reads an
already-captured `providers schema -json` document instead of running tofu.

_This is deliverable D1 of #823. Query execution (D2) and import-block emission
(D3) follow._
