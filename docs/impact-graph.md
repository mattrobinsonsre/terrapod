# Impact Graph

The **Impact graph** is an interactive dependency + blast-radius view of a plan,
rendered on the run-detail page. It turns the flat list of resource changes into
a graph you can explore: click any resource and its **transitive impact** —
everything downstream that depends on it — lights up, with a running count of how
many resources are affected.

![Impact graph: a multi-module plan clustered by module, with a resource selected and its 14 downstream dependents highlighted](images/impact-graph.png)

It's always available — no configuration to enable. The **Impact** tab appears on
any run whose plan produced structured JSON output (every `terraform`/`tofu`
plan run through an agent-mode workspace).

## What it shows

- **Nodes** are the resources in the plan, coloured by their planned action:
  create, update, replace, delete, or no-op. Each node is labelled with its
  resource address (the module prefix is stripped — module membership is shown
  by the cluster instead).
- **Edges** are dependencies: an arrow points from a resource to what it depends
  on. Selecting a node highlights its whole downstream cone (its blast radius)
  and reports the count of affected resources.
- **Module clusters** group same-module resources together, each boxed and
  labelled `module.<name>`, so a large monorepo/multi-module plan reads as
  distinct regions rather than one hairball.
- A filterable resource list on the right jumps the camera to any resource; the
  legend counts changes by action.

## How the graph is derived

Terraform/OpenTofu plan JSON has no explicit instance-level edge list, so
Terrapod reconstructs the dependency DAG from the plan's `configuration` block.
The derivation walks the **entire module tree** (`root_module` →
`module_calls[].module`), resolving each reference in its own module scope:

- a sibling `type.name` reference becomes an edge qualified with the module
  prefix (`module.vpc.aws_subnet.this`);
- a `var.x` reference binds to the parent module call's input expression
  (resolved in the *parent* scope);
- a `module.child.out` reference binds to the child module's output expression
  (resolved in the *child* scope).

That cross-module binding is what makes edges span module boundaries — without
it a modular plan (i.e. essentially all real infrastructure) would show no
dependencies at all. `for_each`/`count` references are included too, so
per-instance cross-module fan-out (e.g. one DNS record per compute instance) is
captured, with a same-key heuristic pairing `svc["api"] → instance["api"]`.

The graph is derived **server-side** and returned as a small JSON payload —
rather than shipping the raw, possibly multi-MB plan JSON to the browser. This
keeps it fast and works uniformly through the BFF in every storage backend.

## API

The tab is backed by a Terrapod-native endpoint:

```
GET /api/terrapod/v1/runs/{run_id}/impact-graph
```

See [api-reference.md → Impact Graph](api-reference.md#impact-graph) for the
response shape and permissions. The typed Go client is
`go-terrapod`'s `Client.GetRunImpactGraph`.
