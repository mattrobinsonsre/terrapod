# Role-Based Access Control (RBAC)

Terrapod uses a label-based RBAC system instead of Terraform Enterprise's team model. Labels replace teams entirely -- a "team" is just a label. This document covers the permission model, role configuration, and common patterns.

---

## Permission Model

### Workspace Permission Levels

Workspace permissions are strictly hierarchical -- each level includes all permissions from the levels below it:

| Level | Grants |
|---|---|
| **read** | View workspace, view runs and plan output, view state metadata, view non-sensitive variables |
| **plan** | read + queue plan-only runs, lock/unlock (own locks), download raw state |
| **write** | plan + confirm/discard applies, create apply runs, CRUD variables, upload state/config |
| **admin** | write + update/delete workspace, change VCS/execution settings, change labels. Cannot change owner (platform admin only) |

### Registry Permission Levels

Modules and providers use a similar three-level hierarchy (no "plan" concept):

| Level | Grants |
|---|---|
| **read** | View module/provider, download artifacts |
| **write** | read + create versions, upload artifacts |
| **admin** | write + update/delete module/provider, change labels |

A role's `workspace_permission` maps to registry permissions: `plan` maps to `read`.

### Platform Permissions

| Operation | Required Role |
|---|---|
| Manage roles and assignments | `admin` |
| Manage VCS connections | `admin` |
| Manage agent pools and tokens | `admin` |
| Binary/module/provider cache admin | `admin` |
| View roles, VCS connections, agent pools | `admin` or `audit` |
| Create workspaces | Any authenticated user (creator becomes owner) |
| Create registry modules/providers | Any authenticated user (creator becomes owner) |
| Variable sets (create/update/delete) | `admin` |

---

## Built-in Roles

Terrapod has three built-in roles that cannot be modified or deleted:

| Role | Behavior |
|---|---|
| **admin** | Bypasses all RBAC checks. Full access to every workspace, registry item, and platform operation. |
| **audit** | Read-only access to all workspaces and registry items. Can view (but not modify) roles, VCS connections, and agent pools. |
| **everyone** | Implicit role assigned to all authenticated users. Grants `read` access to workspaces that have the label `access: everyone`. |

---

## Permission Resolution Order

When a user accesses a workspace, permissions are resolved in this order. The first match wins (highest to lowest priority):

```
1. Platform admin?
   YES --> admin permission on ALL workspaces
   NO  --> continue

2. Platform audit?
   YES --> read permission on ALL workspaces
   NO  --> continue

3. Workspace owner? (ws.owner_email == user.email)
   YES --> admin permission on this workspace
   NO  --> continue

4. Label-based RBAC (custom roles):
   For each custom role the user holds:
     a. Check allow rules (labels + names)
     b. Check deny rules (labels + names)
     c. If workspace matches allow AND does NOT match deny:
        collect that role's workspace_permission
   Take the HIGHEST collected permission
   |
   v (if any permission found, use it)

5. "everyone" role:
   If workspace has label "access: everyone" --> read permission
   |
   v (otherwise)

6. Default: no access (403)
```

---

## Custom Roles

Custom roles define access using allow/deny rules on labels and workspace names.

### Creating a Custom Role

```zsh
curl -X POST https://terrapod.example.com/api/v2/roles \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "roles",
      "attributes": {
        "name": "developer",
        "description": "Can plan and write to development workspaces",
        "workspace-permission": "write",
        "allow-labels": {"env": "dev"},
        "allow-names": [],
        "deny-labels": {},
        "deny-names": []
      }
    }
  }'
```

### Role Attributes

| Attribute | Type | Description |
|---|---|---|
| `name` | string | Unique role name (lowercase, alphanumeric + hyphens) |
| `description` | string | Human-readable description |
| `workspace-permission` | string | One of: `read`, `plan`, `write`, `admin` |
| `allow-labels` | object | Label key-value pairs that grant access |
| `allow-names` | array | Explicit workspace names that grant access |
| `deny-labels` | object | Label key-value pairs that deny access (overrides allow) |
| `deny-names` | array | Explicit workspace names that deny access (overrides allow) |

### Label Matching Rules

- **Allow labels**: A workspace must have ALL specified label key-value pairs to match
- **Allow names**: A workspace name must appear in the list to match
- **A workspace matches if it matches allow-labels OR allow-names**
- **Deny labels**: If a workspace has ANY specified deny label, access is denied regardless of allow rules
- **Deny names**: If a workspace name appears in the deny list, access is denied
- **Deny always wins over allow**

### Listing Roles

```zsh
curl https://terrapod.example.com/api/v2/roles \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

Returns both built-in and custom roles.

### Updating a Role

```zsh
curl -X PATCH https://terrapod.example.com/api/v2/roles/developer \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "roles",
      "attributes": {
        "workspace-permission": "plan",
        "allow-labels": {"env": "dev", "team": "platform"}
      }
    }
  }'
```

### Deleting a Role

```zsh
curl -X DELETE https://terrapod.example.com/api/v2/roles/developer \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

Built-in roles (`admin`, `audit`, `everyone`) cannot be deleted.

---

## Role Assignments

Role assignments bind a user (identified by provider + email) to a role.

### Setting Roles for a User

```zsh
curl -X PUT https://terrapod.example.com/api/v2/role-assignments \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "role-assignments",
      "attributes": {
        "provider-name": "local",
        "email": "alice@example.com",
        "roles": ["developer", "sre-reader"]
      }
    }
  }'
```

For platform roles (admin, audit):

```zsh
curl -X PUT https://terrapod.example.com/api/v2/role-assignments \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "role-assignments",
      "attributes": {
        "provider-name": "local",
        "email": "alice@example.com",
        "roles": ["admin"]
      }
    }
  }'
```

### Listing All Assignments

```zsh
curl https://terrapod.example.com/api/v2/role-assignments \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Removing a Single Assignment

```zsh
curl -X DELETE https://terrapod.example.com/api/v2/role-assignments/local/alice@example.com/developer \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

---

## Workspace Labels and Ownership

### Setting Labels on a Workspace

Labels are key-value pairs set on workspace creation or update:

```zsh
curl -X PATCH https://terrapod.example.com/api/v2/workspaces/ws-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "labels": {
          "env": "production",
          "team": "platform",
          "region": "eu-west-1"
        }
      }
    }
  }'
```

### Workspace Ownership

- The user who creates a workspace automatically becomes its owner
- Owners have `admin` permission on their workspace
- Only a platform admin can change workspace ownership

---

## API Token Role Resolution

When an API token is used for authentication, the user's roles are resolved from:

1. `role_assignments` table (custom roles mapped to provider + email)
2. `platform_role_assignments` table (platform roles: admin, audit)

Results are cached in Redis (`tp:token_roles:{email}`, 60-second TTL) to avoid repeated database queries.

---

## Example Configurations

### Environment-Based Access

Separate read/write access by environment:

```
Role: "dev-writer"
  workspace-permission: write
  allow-labels: { "env": "dev" }

Role: "staging-planner"
  workspace-permission: plan
  allow-labels: { "env": "staging" }

Role: "prod-reader"
  workspace-permission: read
  allow-labels: { "env": "production" }
```

Workspaces:
- `my-app-dev` with labels `{ "env": "dev" }`
- `my-app-staging` with labels `{ "env": "staging" }`
- `my-app-prod` with labels `{ "env": "production" }`

A user with roles `dev-writer` and `staging-planner` can:
- Read, plan, and write to `my-app-dev`
- Read and plan on `my-app-staging`
- No access to `my-app-prod`

### Team-Based Access with Production Exclusion

```
Role: "platform-team"
  workspace-permission: write
  allow-labels: { "team": "platform" }
  deny-labels: { "env": "production" }

Role: "platform-prod"
  workspace-permission: write
  allow-labels: { "team": "platform", "env": "production" }
```

Assign `platform-team` to all platform engineers. Assign `platform-prod` only to senior engineers who need production write access.

### Named Workspace Access

Grant access to specific workspaces by name:

```
Role: "networking-admin"
  workspace-permission: admin
  allow-names: ["vpc-primary", "vpc-secondary", "dns-zones"]
  deny-names: []
```

### Read-Only Audit Access for Everyone

Use the built-in `everyone` role with the `access: everyone` label:

```zsh
# Set label on workspace
curl -X PATCH https://terrapod.example.com/api/v2/workspaces/ws-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "labels": {
          "access": "everyone"
        }
      }
    }
  }'
```

Now all authenticated users can view this workspace.

### Role Assignment via OIDC Groups

Configure your OIDC provider to include group claims, then map them to Terrapod roles:

```yaml
oidc:
  - name: okta
    groups_claim: "groups"
    role_prefixes: ["terrapod:"]
    claims_to_roles:
      - claim: groups
        value: "TerrapodAdmins"
        roles: ["admin"]
      - claim: groups
        value: "PlatformEngineers"
        roles: ["platform-team"]
      - claim: groups
        value: "SRE"
        roles: ["platform-team", "platform-prod"]
```

With `role_prefixes: ["terrapod:"]`, an IDP group named `terrapod:developer` automatically maps to the Terrapod role `developer` without needing an explicit `claims_to_roles` entry.
