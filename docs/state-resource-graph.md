# State Resource Graph

The **State Graph** tab on a workspace (`/workspaces/{id}?tab=state-graph`) draws
the resource-level dependency graph parsed from the workspace's Terraform
**state** — one interactive graph of how the resources *inside* a single
workspace relate. It's the complement to the [Estate Topology](estate-topology.md)
view: estate shows how workspaces relate to each other; this shows how the
resources within one workspace relate. It answers *what hangs off this VPC? what
would ripple if I touched this security group? what's the shape of this state?*

## What it shows

- **Nodes** — every resource in the state, one per resource address (a resource
  with `count`/`for_each` collapses to a single node). Managed resources render
  as spheres, **data sources** as boxes. Node size reflects in-degree: the more
  resources depend on a node, the bigger it is, so foundational resources stand
  out.
- **Edges** — `depends-on` relationships Terraform records in state (both
  explicit `depends_on` and the implicit references it tracks per resource).
- **Group by, your choice** — colour the graph by **Resource type** (the
  default — every `aws_subnet` the same colour), **Module**, **Provider**, or
  **Managed / data**.
- **State version picker** — defaults to the workspace's **current** state
  version; drop the picker back to **any older version** to graph a previous
  state (Terrapod versions every state upload, so history is available). Great
  for seeing how the resource graph changed across applies.

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
