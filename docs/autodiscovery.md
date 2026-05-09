# Workspace Autodiscovery

Modelled on [Atlantis's `autodiscover`](https://www.runatlantis.io/docs/server-side-repo-config.html#autodiscover) feature, autodiscovery auto-creates a Terrapod workspace the first time a PR (or default-branch push) touches a path matching one of your rules. Designed for monorepos where pre-provisioning a workspace per directory is impractical.

## When you'd want this

- A monorepo with hundreds of nested terraform root modules (one per AWS account, one per environment, etc.).
- New roots are added regularly via PRs, and you don't want every PR-author to also have to create a Terrapod workspace.
- You're happy giving every discovered directory the *same* execution defaults — agent pool, terraform version, resource requests, default labels, owner.

## When you wouldn't

- You only have a handful of long-lived workspaces. Just create them.
- Different directories need different workspace configuration that can't be expressed by a single rule.

## How it works

```
┌─────────────────┐       ┌──────────────────────┐       ┌─────────────────────┐
│ PR opened       │  PR's │ Poller scans changed │ Match │ Workspace created   │
│ on branch X     ├──────►│ files vs rules       ├──────►│ with rule's         │
│                 │ files │ (pattern + ignore)   │       │ template defaults   │
└─────────────────┘       └──────────────────────┘       └─────────────────────┘
                                                                    │
                                                                    ▼
                                                         ┌─────────────────────┐
                                                         │ Next poll cycle     │
                                                         │ runs the speculative│
                                                         │ plan as normal      │
                                                         └─────────────────────┘
```

Autodiscovery runs on every poll cycle (default 60s) and on every webhook-triggered immediate poll for the matching repo. It only creates workspaces — the existing PR/branch poll logic queues the speculative plan on the next pass.

## GitHub App permissions

**No new permissions required.** The same `Contents: read`, `Pull requests: read & write`, and `Metadata: read` you've already granted for VCS integration cover autodiscovery (file listing on PRs uses `Pull requests: read`). Existing GitLab access tokens with `read_api` + `read_repository` (or `api`) work without changes.

## Rule schema

Rules are scoped to a single VCS connection + repo. A rule has:

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Display name for the rule. Unique per VCS connection. |
| `vcs-connection-id` | UUID | yes | Reference to an existing VCS connection. |
| `repo-url` | string | yes | Full repo URL, e.g. `https://github.com/myorg/monorepo`. |
| `branch` | string | no | Branch the rule scopes to. Empty = default branch. |
| `pattern` | string | yes | Glob matched against changed file paths (gitignore-style with `**` support). |
| `ignore-patterns` | string[] | no | Globs filtered out before pattern matching. |
| `name-template` | string | no | Template for derived workspace names. Default: directory path with `/` replaced by `-`. |
| `enabled` | bool | no | Default `true`. |
| `execution-mode` | enum | no | Must be `agent` (default). Autodiscovery is VCS-driven; `local` mode would create workspaces with queued runs and no executor. |
| `agent-pool-id` | UUID | no | Inherited by created workspaces in `agent` mode. |
| `execution-backend` | enum | no | `tofu` or `terraform`. Default `tofu`. |
| `terraform-version` | string | no | Default `1.11`. |
| `resource-cpu` / `resource-memory` | string | no | Defaults `1` / `2Gi`. |
| `auto-apply` | bool | no | Default `false`. |
| `labels` | map | no | Inherited by created workspaces — feeds Terrapod's label-based RBAC and filtering. |
| `owner-email` | string | no | Inherited by created workspaces; if unset, created workspaces have no owner and label-RBAC alone determines access. |

## Pattern syntax

Rules use gitignore-style globs. Patterns are matched against the **full file path** (e.g. `accounts/alpha/network/main.tf`).

| Token | Meaning |
|---|---|
| `*` | match anything within a single path segment (no `/`) |
| `**` | match zero or more path segments |
| `?` | match a single non-`/` character |
| `[abc]` | match one of `a`, `b`, `c`; `[!abc]` = NOT one of those |

Only terraform configuration files (`*.tf`, `*.tfvars`, `*.tf.json`, `*.tfvars.json`, `*.hcl`) trigger autodiscovery. README/CI/script changes are filtered out before pattern matching.

## How the workspace is named

The created workspace's `working-directory` is the directory containing the matched terraform file. The default name is that directory with `/` replaced by `-`:

```
accounts/alpha/network/main.tf  →  workspace `accounts-alpha-network` (working_directory = `accounts/alpha/network`)
```

If the default would collide with an existing unrelated workspace, Terrapod logs a warning and skips creation. Tighten `name-template` to disambiguate:

```
name-template: "monorepo-{path}"   →  monorepo-accounts-alpha-network
name-template: "ws-{root}"         →  ws-accounts-alpha-network  ({root} preserves /, sanitiser maps to -)
```

`{path}` is the dashed directory; `{root}` is the directory with `/` preserved. Names are sanitised to `[A-Za-z0-9_-]` and capped at 90 chars (the workspaces.name column limit).

## Created workspace properties

A workspace created by a rule:
- Inherits all template fields above.
- Has `vcs-connection-id`, `vcs-repo-url`, `vcs-branch` set from the rule.
- Has `working-directory` set to the matched file's parent.
- Has `trigger-prefixes` set to `[working_directory]` so subsequent PRs that touch the same dir route to the same workspace via the regular PR-scan path (not via re-running autodiscovery).
- Tracks `autodiscovery-rule-id` so you can audit which rule created it.

If you delete the rule later, existing workspaces keep working — the foreign key sets to NULL on cascade.

## Example

A monorepo for ~2000 AWS accounts in this shape:

```
accounts/
  alpha/network/main.tf
  alpha/compute/main.tf
  beta/network/main.tf
  ...
modules/
  vpc/main.tf      # reusable module — NOT a discoverable root
```

Rule:

```yaml
name: monorepo
vcs-connection-id: vcs-019e0e7b-a6de-7ea6-8b27-3a983c0a098e
repo-url: https://github.com/myorg/monorepo
branch: main
pattern: accounts/*/**/*.tf
ignore-patterns:
  - modules/**
execution-mode: agent
agent-pool-id: apool-019e01db-a2a3-7494-afe0-1a8ecf70b3eb
labels:
  managed-by: monorepo-autodiscover
owner-email: platform@example.com
```

Outcome:

| PR change | Result |
|---|---|
| `accounts/alpha/network/main.tf` | Workspace `accounts-alpha-network` auto-created (if it didn't exist) |
| `accounts/gamma/dns/main.tf` | New workspace `accounts-gamma-dns` auto-created |
| `modules/vpc/main.tf` | No workspace created (matches ignore pattern) |
| `README.md` | No workspace created (not a terraform file) |

## API

Admin-only CRUD at `/api/terrapod/v1/autodiscovery-rules`:

```
GET    /api/terrapod/v1/autodiscovery-rules
POST   /api/terrapod/v1/autodiscovery-rules
GET    /api/terrapod/v1/autodiscovery-rules/{id}
PATCH  /api/terrapod/v1/autodiscovery-rules/{id}
DELETE /api/terrapod/v1/autodiscovery-rules/{id}
```

JSON:API request body example:

```json
{
  "data": {
    "type": "autodiscovery-rules",
    "attributes": {
      "name": "monorepo",
      "vcs-connection-id": "vcs-019e0e7b-...",
      "repo-url": "https://github.com/myorg/monorepo",
      "pattern": "accounts/*/**/*.tf",
      "ignore-patterns": ["modules/**"],
      "execution-mode": "agent",
      "agent-pool-id": "apool-019e01db-...",
      "labels": {"managed-by": "monorepo-autodiscover"},
      "owner-email": "platform@example.com"
    }
  }
}
```

## Operational notes

- **Lifecycle of discovered workspaces**: workspaces persist after the source directory is deleted. Operators can archive/delete via the normal workspace API.
- **Race-safety**: idempotent. Concurrent poll cycles trying to create the same workspace fall through to a "found existing" branch.
- **Failure isolation**: a misconfigured rule (bad repo URL, GitHub auth failure, rate limit) is logged and skipped; other rules in the same cycle continue.
- **Observability**: every autodiscovery action emits a structlog entry — grep API logs for `Autodiscovery created workspace` and `Autodiscovery name collision`.

## Related

- Atlantis autodiscover docs: <https://www.runatlantis.io/docs/server-side-repo-config.html#autodiscover>
- Original feature request: <https://github.com/mattrobinsonsre/terrapod/issues/283>
