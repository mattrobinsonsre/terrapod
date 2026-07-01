# Capability-based RBAC (#585)

> **Status:** implemented. Enforcement across every workspace/pool/registry/
> catalog gate checks capabilities (not level thresholds); roles can be authored
> with an explicit `capabilities` set via the API, `terrapod` provider, and web
> UI. This document is the source of the gate → capability mapping that
> `services/terrapod/auth/capabilities.py` encodes, and the faithfulness contract
> for the migration that back-filled existing roles — existing (level-authored)
> roles are unaffected.
>
> **Authoring.** A role is either *level-authored* (pick a preset per axis:
> workspace `read`/`plan`/`write`/`admin`, pool/registry `read`/`write`/`admin`,
> catalog `none`/`read`/`use`/`admin`) or *capability-authored* (send an explicit
> `capabilities` list of `resource:verb` tokens; the level fields are then
> returned as a derived summary — a preset name, or `custom` when the set matches
> no preset). Capabilities are the stored, enforced truth; a role's response
> always includes its **effective** `capabilities`. Only the grantable tokens
> below are accepted (the `platform:*` tokens are not yet grantable — see #642).

## Model

A **capability** is a `resource:verb` token — the unit of permission. It is
deliberately *not* an API endpoint: endpoints over-split multi-call capabilities
(`state:write` is three endpoints) and under-split payload-polymorphic ones
(apply vs. apply-destroy is one endpoint with a flag).

A **role** carries an explicit set of capabilities. The legacy hierarchical
levels (`workspace_permission`, `pool_permission`, `registry_permission`,
`catalog_permission`) become **presets** over that set:

- **Stored + enforced truth:** a role's `capabilities` list. There is exactly
  one authoritative grant per role.
- **Input sugar (write):** you may POST a preset level (`workspace-permission:
  "plan"`); the server expands it to capabilities once, at write time, and
  stores only the capabilities. Or you POST `capabilities` directly. Never both
  as competing truth.
- **Derived summary (read):** the API reports the closest preset name for a
  role's stored capabilities (or `"custom"`), purely informational.

Capabilities are strictly more expressive than the four cumulative levels
(arbitrary subset vs. prefix), so making them the stored truth **loses
nothing** — it only drops the guarantee that a role is one of four shapes,
which is the point of granular RBAC.

### What capabilities do and don't change

Capabilities replace **what verbs** a principal may perform. They do **not**
touch **which resources** a role applies to — that stays the existing
label/name allow-deny scoping (`allow_labels`/`allow_names`/`deny_labels`/
`deny_names`), resolved per resource. A role still says "on workspaces matching
these labels"; capabilities decide what you can do once matched.

The resolution **order** is unchanged (platform admin → audit → owner →
label-RBAC → everyone-floor → none) and the kind-aware attenuation is unchanged
(`interactive` = live roles; `service_bound` = `min(user, token)`;
`service_detached` = token only). Capability resolution = the **union** of
capabilities from every role that matches a resource, then (for tokens) the
**intersection** with the token's pinned-role capabilities.

## Scope of #585

Capability enforcement covers the **four label-scoped axes** that custom roles
carry: **workspace, pool, registry, catalog**. The platform-admin gates
(`require_admin` / `require_admin_or_audit`) stay role-name based; decomposing
the monolithic platform admin into the scoped `platform:*` capabilities is a
deliberate follow-up (**#642**). The `platform:*` tokens are listed below for
honesty of the built-in `admin` capability set, but are not independently
grantable or enforced in this feature.

Net-new capabilities that **no preset grants** (so no existing role gains them)
are also out of scope here and tracked separately: a finer state-read tier
(`state:read-outputs` / `state:read-sensitive`) and separable
`run:apply-destroy` *enforcement* (the token exists and sits in the `write`
preset for faithfulness; gating destroy on it independently is the granular
win, landed with enforcement).

---

## Workspace axis — gate → capability

Hierarchy today: `read < plan < write < admin`. Catalog-managed workspaces clamp
every non-platform-admin grant to `read` (unchanged; applies on top of
capabilities).

### `read` preset

| Capability | Gates (method path — what it does) |
|---|---|
| `workspace:read` | GET workspace by id/name; GET org workspaces list (per-row filter); GET tag-bindings / effective-tag-bindings; POST/DELETE relationships/tags (no-op, gated read); GET vcs-refs |
| `run:read` | GET run; GET workspace runs; GET run-events; GET plan/apply by id; GET plan log / json-output / apply log; GET run plan/apply (terrapod); GET+POST plan-summary, regenerate, chat messages (read-tier by design — no infra mutation); GET workspace runs SSE stream |
| `state:read-metadata` | GET workspace state-versions list; GET current-state-version; GET state-version metadata (mints upload URL) |
| `var:read` | GET workspace vars (sensitive values masked) |
| `config:read` | GET configuration-versions list; GET cv download; POST cv download-ticket; GET cv diff |
| `run-task:read` | GET workspace run-tasks; GET run-task; GET task-stages; GET task-stage |
| `notification:read` | GET workspace notification-configurations; GET notification-configuration |
| `run-trigger:read` | GET workspace run-triggers; GET run-trigger |

### `plan` preset (adds)

| Capability | Gates |
|---|---|
| `run:plan` | POST runs (plan-only branch: `plan_only=true`) |
| `run:cancel` | POST runs/{id}/actions/discard; .../cancel; .../retry |
| `workspace:lock` | POST workspaces/{id}/actions/lock; .../unlock (own lock) |
| `state:read` | GET state-versions/{id}/download (**raw** state JSON — contains secrets) |
| `drift:dismiss` | POST workspaces/{id}/actions/dismiss-drift |

### `write` preset (adds)

| Capability | Gates |
|---|---|
| `run:apply` | POST runs (apply branch: not plan-only); POST runs/{id}/actions/apply (confirm) |
| `run:apply-destroy` | the same two gates **when `is_destroy=true`** (separable enforcement lands with the enforcement slice; in the `write` preset so migrated write roles keep destroy) |
| `var:write` | POST/PATCH/DELETE workspace vars |
| `state:write` | POST workspaces/{id}/state-versions; POST state-versions actions/upload; POST state-versions/{id}/actions/rollback |
| `config:upload` | POST workspaces/{id}/configuration-versions |

### `admin` preset (adds)

| Capability | Gates |
|---|---|
| `workspace:settings` | PATCH workspace (settings, VCS, labels, execution mode, pool — pool reassign additionally needs `pool:assign` on the target pool) |
| `workspace:force-unlock` | POST workspaces/{id}/actions/unlock when the lock is held by another user |
| `workspace:delete` | DELETE workspace |
| `state:delete` | DELETE state-versions/{id}/manage (delete a non-current state version) |
| `notification:manage` | POST/PATCH/DELETE notification-configurations; POST .../actions/verify |
| `run-task:manage` | POST/PATCH/DELETE run-tasks; POST task-stages/{id}/actions/override |
| `run-trigger:manage` | POST workspaces/{id}/run-triggers; DELETE run-triggers/{id} |

> **Correction vs. first draft:** variable-**set** management is *not* here — varset
> CRUD is `require_admin` (platform), so it is a `platform:varset-admin`
> capability, not a workspace-admin one. Putting it in the workspace-admin preset
> would have falsely granted it to workspace-admin roles.

---

## Pool axis — gate → capability

Hierarchy `read < write < admin`.

| Preset | Capability | Gates |
|---|---|---|
| read | `pool:read` | GET agent-pools (filter); GET pool; GET pool listeners; GET pool events SSE |
| write | `pool:assign` | assign a pool to a workspace — checked in POST workspace create + PATCH workspace + catalog provision (in addition to workspace admin / catalog use) |
| admin | `pool:manage` | PATCH pool; DELETE pool; GET/POST/DELETE pool tokens; DELETE listener |

Pool **creation** (POST agent-pools) is `require_admin` → `platform:pool-admin`,
not a pool-RBAC level. Orphan-listener cleanup also falls back to platform admin.

---

## Registry axis — gate → capability

Hierarchy `read < write < admin`. (Runner tokens get an implicit `read` floor —
unchanged, lives in `registry_rbac_service`.)

| Preset | Capability | Gates |
|---|---|---|
| read | `registry:read` | CLI list/download (modules + providers); GET module/provider show, interface, versions, platforms, workspace-links |
| write | `registry:write` | POST module versions; PUT module upload; PUT provider shasums / shasums.sig / platform binary |
| admin | `registry:admin` | DELETE module/provider/version/platform; PATCH module/provider (labels) — **owner reassignment additionally requires platform admin**; PATCH module vcs; POST/DELETE workspace-links |

`platform:registry-admin` (not registry-RBAC) covers: binary-cache + provider-cache
admin endpoints, and registry resource **owner** reassignment. **Gap flagged:**
`gpg_keys.py` CRUD is currently authenticated-only (no registry or admin gate) —
a pre-existing surface noted for follow-up, not changed by the faithful migration.

---

## Catalog axis — gate → capability

Hierarchy `read < use < admin`. No `everyone` floor; `none` grants nothing
(opt-in). Unchanged here.

| Preset | Capability | Gates |
|---|---|---|
| read | `catalog:read` | GET catalog-items (filter); GET item; GET item form; GET item instances; GET instance |
| use | `catalog:use` | POST item provision (also needs `pool:assign`); PATCH instance reconfigure; POST instance destroy/confirm/discard |
| admin | `catalog:admin` | DELETE catalog-instance (orphan-delete) |

Catalog **item** authoring (POST/PATCH/DELETE catalog-items) and
**provider-templates** are `require_admin` → `platform:catalog-admin`, not
catalog-RBAC.

---

## Platform capabilities (informational; enforced via role-name until #642)

`platform:role-admin`, `platform:vcs-admin`, `platform:pool-admin`,
`platform:registry-admin`, `platform:varset-admin`, `platform:user-admin`,
`platform:audit-admin`, `platform:policy-admin`,
`platform:autodiscovery-admin`, `platform:catalog-admin`,
`platform:bulk-admin`, `platform:settings-admin`.

These enumerate every `require_admin` / `require_admin_or_audit` surface in the
API (roles, role-assignments, vcs-connections, pool create, binary/provider
cache, GPG/owner reassign, variable sets, users, audit log, policy sets,
autodiscovery rules, catalog items + provider templates, workspace bulk ops,
encryption settings). They are the target vocabulary for #642.

---

## Built-in roles (code, not DB rows)

| Role | Capabilities |
|---|---|
| `admin` | every grantable capability ∪ every `platform:*` (admin bypasses RBAC; this is the honest description) |
| `audit` | every `*:read` / `*:read-metadata` capability across the four axes + `platform:audit-admin` (read the audit log) |
| `everyone` | the `read` workspace preset, granted only on resources labelled `access: everyone` |

---

## Enforcement contract (obligations for the resolution-switch slice)

The data layer (this slice) records capabilities and exposes them read-only;
levels are still enforced, so behaviour is byte-identical. When the enforcement
slice flips resolution from a scalar level to a capability set, it MUST honour
the following — these are the holes an independent review flagged that a naive
"level → preset set" translation would silently re-introduce:

1. **Per-gate capability, never "set ⊇ preset".** Each route gate maps to
   exactly ONE capability (the verb it performs) and checks
   `has_capability(caps, THAT_CAP)`. Translating `has_permission(perm, "write")`
   on `POST /runs` to "holds the whole write preset" would deny a future
   granular role that legitimately holds only the relevant verb, and re-couple
   the levels the feature exists to decouple. The faithfulness test asserts
   **per gate**, not per level.
2. **`has_permission(effective, required)` → `has_capability(caps, CAP)`.** Keep
   a single membership primitive (`capabilities.has_capability`); the ~40 gate
   sites become a mechanical swap of the level string for the gate's capability
   constant.
3. **Resolution = union, attenuation = intersection.** A principal's caps on a
   resource = the union of capabilities from every role that matches it. Token
   attenuation: `service_bound` = `user_caps ∩ token_caps`; `service_detached` =
   `token_caps` only; the everyone-floor contributes exactly the workspace
   `read` preset to the **user** side only (mirroring `apply_everyone_floor`).
   For migrated (preset-only) roles this is provably identical to today's
   scalar-max / `min(level)` because the per-axis presets are cumulative.
4. **Catalog-managed clamp = intersect with the read preset.** Replace
   `if PERMISSION_HIERARCHY[best] > 0: best = "read"` with
   `resolved_caps &= _WORKSPACE_LEVELS["read"]`, applied at the same point
   (after union + everyone-floor, before return). Do **not** reimplement it as a
   string-suffix heuristic (`endswith(":read")`).
5. **Self-lockout guard = set difference, not total-order `<`.** Capability sets
   are a partial order. Replace "would my level drop?"
   (`PERMISSION_HIERARCHY[new] < PERMISSION_HIERARCHY[old]`) with "would the edit
   remove any capability I hold?" (`if old_caps - new_caps: 409`). The four 409
   sites (tfe_v2 workspace label edit, agent_pools, registry module/provider
   owner-change) also interpolate the old/new level strings into the message —
   switch those to the derived summary name or the removed-capability list.
6. **Serialized `can-*` flags become capability-membership.** The 12 workspace
   `can-*` flags (`tfe_v2.py`), and registry `can-*`, map each to its capability.
   Note `can-force-unlock` shifts meaning from "is admin" to "has
   `workspace:force-unlock`" — permissive, acceptable in beta, but documented.
   Org/account entitlements stay platform-role based (unchanged).
7. **Levels become a derived summary on read.** Capabilities are the stored
   truth; the level columns are recomputed from `summarize_capabilities(caps)`
   on every write (a denormalised cache, not a second source of truth) so
   consumers that still read the level fields keep working until they migrate.
8. **Forward-compat.** Load and write-validate stored sets through
   `normalize_capabilities` (alias-upgrade legacy tokens) so a capability
   rename/split never silently drops a grant or traps a role uneditable.

## Faithfulness invariant (the test that proves the migration)

For **every** combination of legacy levels a role can hold,
`expand_preset(...)` must equal *exactly* the set of capabilities for the gates
that level combination grants today — no capability lost, none gained. The
enforcement slice ships an **effective-permission before/after** test anchored
to the real route-gate sites: for each existing role shape, the set of gates it
passes under level-based resolution must be identical to the set it passes under
capability resolution. That, plus the no-retroactive-tightening rule (we are in
beta), is the guarantee that turning capabilities on changes nothing for
existing roles while making finer grants newly possible.
