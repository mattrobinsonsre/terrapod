# State Resource Graph

The **State Graph** tab on a workspace (`/workspaces/{id}?tab=state-graph`) draws
the resource-level dependency graph parsed from the workspace's Terraform
**state** — one interactive graph of how the resources *inside* a single
workspace relate. It's the complement to the [Estate Topology](estate-topology.md)
view: estate shows how workspaces relate to each other; this shows how the
resources within one workspace relate. It answers *what hangs off this VPC? what
would ripple if I touched this security group? what's the shape of this state?*

It is the **same interactive graph as the plan/impact graph** ([impact-graph.md](impact-graph.md)) — same module clustering, same click-to-highlight blast radius — sharing one renderer. The only difference is what **colour** encodes: the plan graph spends colour on the change action (create/update/delete); the state graph has no "what's happening" axis, so colour is free to encode a pivot you choose.

## What it shows

- **Nodes** — every resource in the state, one node per resource *block*. Node
  size reflects in-degree: the more resources depend on a node, the bigger it is,
  so foundational resources (a VPC, a shared security group) stand out.
- **Count / for_each "nucleus"** — a resource with `count`/`for_each` is one node
  drawn as a **clump of small spheres, one pearl per instance** (the label shows
  the exact count, e.g. `aws_subnet.this ×8`), so a resource with many instances
  reads as a big cluster at a glance instead of a single sphere. (Modules are
  *not* collapsed — three modules from a `for_each` stay three boxes.)
- **Module clustering** — resources are grouped spatially by their module and
  drawn inside a translucent `module.<name>` box; root-module resources float
  free. So a multi-module state reads as distinct regions at a glance.
- **Edges** — `depends-on` relationships Terraform records in state (both
  explicit `depends_on` and the implicit references it tracks per resource).
- **Blast radius** — click a resource (or pick it from the **Resources** panel)
  and everything that transitively **depends on it** lights up, so you can see
  what a change would ripple into.
- **Color by, your choice** — colour the nodes by **Resource type** (the
  default — every `aws_subnet` the same colour), **Module**, **Provider**, or
  **Managed / data**. (Colouring is independent of the module clustering above.)
- **State version picker** — defaults to the workspace's **current** state
  version; drop the picker back to **any older version** to graph a previous
  state (Terrapod versions every state upload, so history is available). Great
  for seeing how the resource graph changed across applies.

A compact toolbar over the graph keeps `Reset view` and `Clear` always to hand,
with `Key` (the colour legend) and `Resources` (the searchable resource list)
as dismissable overlays so the graph keeps the canvas.

## Graph and table

The 3D graph is an **augmentation, never the only path**. A **Table** view
(toggle in the toolbar) gives the same information as a keyboard- and
screen-reader-navigable table: each resource with its type, mode (managed/data),
module, and how many resources depend on it, sorted by in-degree. On a **phone**
the tab defaults to the table — heavy WebGL is a poor fit for small/low-power
devices, and the table carries the full picture.

Very large states are capped for legibility (the first 2,000 resources); the
toolbar says so explicitly when a state is truncated.

## RBAC

The State Graph requires the same **`state:read`** permission as downloading the
raw state, because the graph is derived from the (secret-bearing) state blob.
Seeing the resource graph therefore requires the same trust as reading the state
it comes from.

## API

The tab is backed by a Terrapod-native endpoint:

```
GET /api/terrapod/v1/workspaces/{workspace_id}/state-graph[?state_version=sv-...]
```

See [api-reference.md → State Graph](api-reference.md#state-graph) for the
response shape. The typed Go client is `go-terrapod`'s `Client.GetStateGraph`.
It's always available — no configuration to enable.
