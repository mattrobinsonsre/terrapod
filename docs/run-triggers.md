# Run Triggers

Run triggers create cross-workspace dependency chains. When a source workspace completes an apply, all downstream workspaces with triggers configured on that source automatically get new runs queued.

This is useful for multi-layer infrastructure where changes in a base layer (e.g. networking) should cascade to dependent layers (e.g. compute, applications).

---

## How It Works

1. An admin creates a trigger linking a **source** workspace to a **destination** workspace
2. When the source workspace completes a successful apply (non-speculative), Terrapod fires all triggers where that workspace is the source
3. For each trigger, a new run is created and queued in the destination workspace
4. The triggered run respects the destination workspace's `auto_apply` setting

No data is passed between workspaces via the trigger mechanism. Downstream workspaces read outputs from upstream workspaces using the `terraform_remote_state` data source independently.

---

## Limits

| Constraint | Value |
|---|---|
| Max source workspaces per destination | 20 |
| Duplicate triggers | One trigger per (source, destination) pair |
| Self-triggers | Not allowed — a workspace cannot trigger itself |

---

## API

All endpoints use JSON:API format.

### Create Trigger

```
POST /api/v2/workspaces/{workspace_id}/run-triggers
```

Requires `admin` permission on the destination workspace.

```bash
curl -X POST https://terrapod.local/api/v2/workspaces/ws-dest-id/run-triggers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "relationships": {
        "sourceable": {
          "data": {
            "id": "ws-source-id",
            "type": "workspaces"
          }
        }
      }
    }
  }'
```

Response (201):

```json
{
  "data": {
    "id": "rt-uuid",
    "type": "run-triggers",
    "attributes": {
      "workspace-name": "destination-ws",
      "sourceable-name": "source-ws",
      "created-at": "2025-01-15T10:30:00Z"
    },
    "relationships": {
      "workspace": {
        "data": { "id": "ws-dest-id", "type": "workspaces" }
      },
      "sourceable": {
        "data": { "id": "ws-source-id", "type": "workspaces" }
      }
    }
  }
}
```

### List Triggers

```
GET /api/v2/workspaces/{workspace_id}/run-triggers?filter[run-trigger][type]={inbound|outbound}
```

Requires `read` permission. The `filter[run-trigger][type]` parameter is required:

- `inbound` — triggers where this workspace is the destination (will receive triggered runs)
- `outbound` — triggers where this workspace is the source (will trigger other workspaces on apply)

### Show Trigger

```
GET /api/v2/run-triggers/{run_trigger_id}
```

Requires `read` permission on the destination workspace.

### Delete Trigger

```
DELETE /api/v2/run-triggers/{run_trigger_id}
```

Requires `admin` permission on the destination workspace. Returns 204 No Content.

---

## Trigger Firing

Triggers fire when a run transitions to `applied` status and is not plan-only. The flow:

1. Run in source workspace reaches `applied` state
2. `fire_run_triggers()` queries all `RunTrigger` rows where this workspace is the source
3. For each trigger, a new `Run` is created in the destination workspace with:
    - Message: `"Triggered by successful apply in workspace '{source_name}'"`
    - `auto_apply` from the destination workspace's setting
    - `plan_only=false` (full plan + apply)
4. Each triggered run is immediately queued for execution

Triggers do **not** fire for:

- Plan-only runs (speculative plans, drift detection runs)
- Errored, canceled, or discarded runs

---

## Cascade Behaviour

Triggers cascade. If workspace A triggers workspace B, and workspace B triggers workspace C, then an apply in A will trigger B, and when B's triggered run applies, it will trigger C.

There is no cycle detection — avoid creating circular trigger chains (A → B → A).

---

## Example

```
networking (source)
  ├── compute (destination)
  └── database (destination)

Apply in "networking" → queues runs in "compute" and "database"
```

---

## See Also

- [Architecture](architecture.md) — run state machine and scheduler
- [API Reference](api-reference.md) — full endpoint documentation
