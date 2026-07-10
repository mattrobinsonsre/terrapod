# Estate Topology

The **Estate topology** page (`/estate`) is a whole-estate view of how your
workspaces and modules depend on each other — the cross-workspace structure
Terrapod already holds centrally, drawn as one interactive graph. It answers
questions a flat workspace list can't: *what depends on this foundational
workspace? what does bumping this module touch? what's orphaned?*

## What it shows

- **Nodes** — every workspace you can see (spheres) plus the registry **modules**
  they use (a distinct ◆ shape). Node size reflects in-degree: the more things
  depend on a node, the bigger it is, so foundational workspaces and
  widely-used modules stand out.
- **Edges** — three kinds of real dependency:
  - **remote-state** — a workspace reads another's state (`terraform_remote_state`).
  - **run-trigger** — a workspace's apply triggers a run in another.
  - **uses-module** — a registry module is used by a workspace (from module ↔
    workspace links). Click a module to see *how many workspaces re-plan if you
    bump it*.
- **Group by, your choice** — Terrapod enforces **no labelling convention**, so
  the page never assumes one. The **Group by** control is built from *your*
  estate: **None**, any **label key** actually in use (`team`, `region`,
  `env`, whatever you chose), **Agent pool**, or **Name prefix**. Pick the lens
  that fits how you organise; the graph re-colours instantly.

## Graph and table

The 3D graph is an **augmentation, never the only path**. A **Table** view
(toggle top-left) gives the same information as a keyboard- and
screen-reader-navigable table: each workspace with its group value, agent pool,
how many things depend on it, and the modules it uses, plus a modules table
(each module and how many workspaces use it). On a **phone** the page defaults
to the table — heavy WebGL is a poor fit for small/low-power devices, and the
table carries the full picture.

## RBAC

The graph is **filtered to the workspaces you can read**. A dependency edge or a
module only appears when it touches a workspace you can see, so the estate view
never leaks a workspace (or a dependency on one) your role wouldn't otherwise
show. A platform admin sees the whole estate; a narrowly-scoped role sees a
correspondingly smaller graph.

## API

The page is backed by a Terrapod-native endpoint:

```
GET /api/terrapod/v1/estate-graph
```

See [api-reference.md → Estate Graph](api-reference.md#estate-graph) for the
response shape. The typed Go client is `go-terrapod`'s `Client.GetEstateGraph`.
It's always available — no configuration to enable.
