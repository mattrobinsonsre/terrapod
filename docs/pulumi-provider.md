# Pulumi provider (`pulumi-resource-terrapod`)

Manage Terrapod itself from a Pulumi program — the same objects the
[Terraform provider](terraform-provider.md) manages, reached from TypeScript,
Python, Go or C#.

It exists because of one concrete gap. A workspace's Ansible inventory is
[declared from the IaC program that provisions the hosts](ansible-inventory.md),
as `terrapod_inventory_*` resources. Those are provider resources, so without a
Pulumi equivalent a Pulumi workspace could provision an estate and then have no
way to declare the inventory for it — git, the UI or the API, but nothing
derived from what it had just created. A Terraform workspace had that and a
Pulumi one did not, which is the kind of second-class outcome the
[multi-engine stance](../AGENTS.md) exists to avoid.

## It is bridged, and what that means for you

The provider is generated from `terraform-provider-terrapod` with
[`pulumi-terraform-bridge`](https://github.com/pulumi/pulumi-terraform-bridge),
not written separately. Two consequences, and the second is a real trade:

- **The surface is the same and stays the same.** A resource added to the
  Terraform provider appears here with no separate work, so the two cannot drift
  apart. All 35 resources and 14 data sources are available, not just the
  inventory ones.
- **The ergonomics are Terraform-shaped.** Naming, types and the general feel
  follow the Terraform schema rather than idiomatic Pulumi. That is a deliberate
  trade for not maintaining a second provider surface, and it is the thing worth
  revisiting if Pulumi use here grows.

## Getting started

### 1. Add the package

Pulumi generates the SDK **on your machine** from the plugin, rather than you
installing a published package from npm, PyPI or NuGet:

```sh
pulumi package add pulumi-resource-terrapod
```

That downloads the plugin and generates an SDK in the language your project
uses. Needs Pulumi 3.200.0 or later. The generated `sdk/` directory can be
committed — useful for a component others consume — or regenerated later with
`pulumi install`.

This is Pulumi's own recommended path for a provider that is not in its public
registry, and it is why nothing is published to npm, PyPI or NuGet: the plugin
is the only artifact, which is also what keeps the restricted-network story
below simple.

### 2. Configure it — or don't, inside a run

The provider reads `TERRAPOD_HOSTNAME` and `TERRAPOD_TOKEN` from the
environment, exactly as the Terraform provider does.

**Inside an agent run you set neither.** Terrapod exports both from the run's
own credentials, so an empty provider configuration works:

```typescript
import * as terrapod from "@terrapod/pulumi";

// No hostname, no token: the run supplies them, scoped to this run.
const provider = new terrapod.Provider("terrapod", {});
```

A workspace variable that sets either one takes over, and then **both** are left
to you — supplying one and inheriting the other would send the run's own token
to a host the workspace chose. See
[cloud identity](cloud-identity.md) for the same pattern on the Terraform side.

**Outside a run** — a practitioner on a laptop — set them yourself, and point
Pulumi's plugin resolution at your instance:

```sh
export TERRAPOD_HOSTNAME=terrapod.example.com
export TERRAPOD_TOKEN=…
export PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES='pulumi-resource-terrapod=https://terrapod.example.com/api/v1/package-cache/pulumi/'
```

That is the same step you already take to install the Terraform provider from
your own instance rather than from the public registry.

### 3. Declare the inventory for what you just provisioned

The reason the provider exists. Each host your program creates becomes an
inventory host, and group membership and variables follow from the program
rather than from a file somebody edits:

```typescript
const web = new aws.ec2.Instance("web", { /* … */ });

const host = new terrapod.InventoryHost("web", {
    workspaceId: workspace.id,
    name: web.privateDns,
}, { provider });

new terrapod.InventoryHostVar("web-addr", {
    hostId: host.id,
    key: "ansible_host",
    value: web.privateIp,
}, { provider });

new terrapod.InventoryHostGroup("web-in-webservers", {
    hostId: host.id,
    groupId: webservers.id,
}, { provider });
```

A [configure operation](ansible-configure.md) on that workspace then targets
exactly those hosts. `terrapod.getInventoryResolved` reads the merged result
back, including anything contributed from git.

## Restricted networks

The plugin is fetched through Terrapod's own
[Pulumi plugin cache](registry.md), like every other plugin a run needs:
`PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES` points at the loopback proxy with a
pattern matching everything, so a runner needs no upstream reach at all.

Terrapod's own plugin is published on Terrapod's releases rather than
`get.pulumi.com`, so the cache resolves it against a second base,
`api.config.registry.package_cache.pulumi.first_party_url`. Point that at an
internal mirror for an air-gapped install, the same way the other mirror URLs
work — and note it is **separate** from `upstream` on purpose, because that one
is where Pulumi's plugins come from and redirecting it would send every other
plugin to a host that does not have them.

## Where this is not coerced into the Terraform shape

Stated rather than smoothed over:

- **The SDK is generated locally**, so there is no published package version to
  pin in `package.json` or `requirements.txt` the way a registry package would
  be. Pin the provider by plugin version and commit the generated `sdk/` if you
  need that reproducibility.
- **Names and types are Terraform's.** `inventoryHostVar` rather than anything a
  Pulumi author would have chosen, and attribute types follow the Terraform
  schema.
- **No Pulumi-native component resources.** The bridge maps resources
  one-for-one; there is no higher-level `InventoryForFleet` convenience type.

## See also

- [Terraform / OpenTofu provider](terraform-provider.md) — the same surface, and
  the schema this one is generated from
- [Pulumi workspaces and runs](pulumi.md) — the engine side
- [Ansible inventory](ansible-inventory.md) — what the inventory resources build
- [Registry and caching](registry.md) — how the plugin reaches a runner
