# Upgrading to 2.0

This page lists every change in 2.0 that can require action on your side, with the
exact edit for each. It is the "read the migration notes before upgrading" that
[versioning-and-support.md](versioning-and-support.md) points at for a MAJOR
release.

2.0 is the first release permitted to break a stable surface since the
[1.0 stability promise](versioning-and-support.md), and the bar for using that
permission is high: a break earns its place only where carrying the old shape
forward would mean shipping a design we know to be wrong for the rest of the
major. Everything else stays additive, exactly as in a minor.

If a surface is not listed here, it did not change.

## Breaking changes

### The deprecated path aliases are removed

**Affects:** anything still calling `/api/terrapod/v1/…` — your own scripts,
dashboards, and any Terrapod runner/listener image, SDK or provider you have not
upgraded.

Three aliases go at 2.0:

| removed | use |
|---|---|
| `/api/terrapod/v1/…` | `/api/v1/…` |
| `/api/v2/…` | `/api/tfe/v2/…` |
| `/v1/providers/…` | `/api/v1/provider-mirror/…` |

`terraform` and `tofu` need nothing — they take the path from service
discovery, which has advertised the new one since v1.7.0.

**The Prometheus `path_template` label changes at the same time.** Until 2.0 it
reports the OLD path for both prefixes, so dashboards keep working through the
window; at 2.0 it reports the canonical one. Update any panel or alert that
matches `/api/v2/` or `/api/terrapod/v1/` literally.

`/api/v1` has been canonical since v1.7.0, with `/api/terrapod/v1` served
alongside it and advertising `Deprecation` / `Sunset` headers. 2.0 removes the
alias.

**Order matters, and Terrapod moves first.** The runner and listener images, the
HA peer-replication calls, `go-terrapod`, the provider and the MCP server all
still call the alias as of v1.7.0 — deliberately, because runners are expected to
lag the API. They are moved onto `/api/v1` in a 1.x minor *before* 2.0, so that
by the time the alias goes, nothing Terrapod ships still depends on it.

**What you must do before upgrading:**

1. Upgrade runner/listener images to a version that calls `/api/v1` — check
   `docs/deprecations.md` for which minor that is.
2. Upgrade `go-terrapod`, `terraform-provider-terrapod` and `terrapod-mcp` if you
   use them directly.
3. Update your own scripts, `curl` calls, dashboards and Prometheus queries.
4. If you pinned `webhookIngress.paths` or `internalIngress.paths`, drop the
   `/api/terrapod/v1` entries and keep the `/api/v1` ones.

**How to find what still uses it:** the alias returns `Deprecation: true` and a
`Sunset` date on every response, so a client hitting it is visible in your own
logs and proxies before you upgrade.

### SSO callback URLs move to `/api/v1` — re-register them with your IdP FIRST

**Affects:** every deployment using OIDC or SAML. **This one will lock you out of
the UI if you skip it**, so it is listed first deliberately.

In 2.0.0 the default for `api.config.auth.legacy_callback_url` flips from `true`
to `false`, and Terrapod starts building its IdP-facing URLs on the canonical
`/api/v1` prefix:

```
before:  {callback_base_url}/api/terrapod/v1/auth/callback
after:   {callback_base_url}/api/v1/auth/callback

before:  {callback_base_url}/api/terrapod/v1/auth/saml/acs    (SAML only)
after:   {callback_base_url}/api/v1/auth/saml/acs
```

**Why serving both prefixes does not save you.** Terrapod has served
`/api/terrapod/v1` and `/api/v1` side by side since v1.7.0, and for every other
path that is the whole story. These two are different: they are **registered**
with your identity provider, which validates the `redirect_uri` Terrapod sends
against its own allow-list. An unrecognised value is refused **at the IdP**,
before any request reaches Terrapod — so there is nothing Terrapod can serve,
alias or not, that makes the switch safe on its own. Every SSO login fails with
an IdP-side error, and local admin login is your only way back in.

**What to do — in this order:**

1. **Before upgrading**, add the new URLs to your IdP's allowed callback list
   (Auth0 "Allowed Callback URLs", Okta "Sign-in redirect URIs", Entra
   "Redirect URIs", and the ACS URL in your SAML SP config). Keep the old ones
   registered: with both listed, either value is accepted and the upgrade is a
   no-op at the IdP.
2. Upgrade to 2.0.0.
3. Confirm an SSO login works.
4. Optionally remove the old URLs from your IdP once you are satisfied.

**If you cannot do step 1 yet**, set `legacy_callback_url: true` explicitly in
your `values.yaml` before upgrading. That pins the old behaviour and 2.0.0 changes
nothing for you — the switch is not removed in 2.0.0, only its default changes.

**If you are already locked out:** log in with a local admin account, or set
`legacy_callback_url: true` and roll the API pods.

### Label rules take a list of values per key

**Affects:** `terraform-provider-terrapod` configurations, and Go programs that
import `go-terrapod` directly. It does **not** affect the HTTP API, the web UI, or
any existing role — see [What does not break](#what-does-not-break) below.

A role's `allow_labels` / `deny_labels` — and a policy set's, which deliberately
reuse the same matcher — bind each label key to the values that satisfy it. The
server has always stored and enforced a **set** of values per key, so
`{"env": ["dev", "stg"]}` means "env is dev or stg". Every client typed it as a
single string, so no client could express or even read back a rule with more than
one value per key.

That mismatch was not cosmetic. A rule authored through the API was invisible to
the provider and the SDK, and the roles form in the web UI silently kept whichever
value came last — so `env=prod, env=stg` produced a role granting only staging,
with nothing to say so. 1.6.0 stopped that from passing unseen by rejecting the
input outright; 2.0 fixes it properly by giving the clients the shape the server
always had.

**Provider** — a scalar value becomes a one-element list:

```diff
 resource "terrapod_role" "dev_writer" {
   name                 = "dev-writer"
   workspace_permission = "write"
-  allow_labels = { env = "dev" }
+  allow_labels = { env = ["dev"] }
 }
```

The same edit applies to `deny_labels`. There is no state migration to run: the
values are unchanged, only their type in HCL. Once you are on the new type, the
thing you could not say before becomes available — one role covering several
environments rather than one role per value:

```hcl
allow_labels = { env = ["dev", "stg"] }
```

**go-terrapod** — the field types widen, so callers that build a rule need the same
one-element-list edit:

```diff
-AllowLabels: map[string]string{"env": "dev"},
+AllowLabels: map[string][]string{"env": {"dev"}},
```

Reading is more forgiving than writing: the SDK normalises a scalar it receives into
a one-element list, so a rule stored by an older client decodes without error.

Policy sets carry the same two fields and the same widening, because policy-set
scoping deliberately reuses the label-RBAC matcher rather than resembling it. They
have no provider resource, so the only affected callers are Go programs.
`PolicySet` additionally *gains* `AllowLabels` / `DenyLabels` on the read side: they
were settable through create and update and never returned, so a caller could scope
a policy set and then be unable to read back the scoping it had just applied. That
is an addition, not a break.

### What does not break

- **The HTTP API accepts both shapes.** A scalar value is read as a one-element
  list, so existing automation posting `{"env": "dev"}` keeps working and needs no
  change.
- **Existing roles and policy sets are untouched.** Nothing is rewritten in the
  database; both shapes have always been valid there.
- **The web UI needs nothing.** The roles form now accumulates a repeated key
  instead of refusing it, and the policy-set form already supported several values
  per key.

### A run held after its plan reports Terraform Enterprise's statuses

**Affects:** anything that reads a run's `status` and treats `planning` as "not
finished yet", and anything that expects a failed mandatory run task to error a
run. The `tofu`/`terraform` CLI is not affected, except that it now does the
right thing.

In 2.0.0 `api.config.runs.tfe_post_plan_decisions` defaults to `true`. A run that
a post-plan gate holds reports:

| before | after |
|---|---|
| `planning` + `blocked-by: run-task` (tasks running) | `post_plan_running` |
| `errored` (a mandatory task failed) | `post_plan_awaiting_decision`, held for an override or a discard |
| `planning` + `blocked-by: policy` | `policy_override` |
| `planning` + `blocked-by: security-scan` | `policy_override` |

`blocked-by` is reported exactly as before, so code that keys on it needs
nothing. The run also lists its policy checks and task stages, which is how
`tofu apply` now shows a failed policy and asks whether to override it, and how
`-auto-approve` overrides one for a caller allowed to. A speculative `tofu plan`
that a policy fails now exits non-zero, as it does on Terraform Enterprise.

Nothing in the database changes: the new statuses are only reported, and the
run is still stored as `planning`.

**What you must do before upgrading:**

1. Find automation that branches on `status == "planning"` or waits for
   `errored` after a run-task failure, and key it on `blocked-by` (unchanged)
   or accept the new statuses.
2. Try it ahead of time: send `X-Terrapod-Post-Plan-Decisions: tfe` from a
   script to see the new answer, or set `runs.tfe_post_plan_decisions: true`
   on a 1.x release to move the whole deployment, CLI included.
3. To keep the old answer for a while after upgrading, set
   `runs.tfe_post_plan_decisions: false`, or send
   `X-Terrapod-Post-Plan-Decisions: legacy` from the client that needs it.

See [post-plan-decisions.md](post-plan-decisions.md).

### A column is renamed, so the API rollout has a brief window of errors

**Affects:** every deployment, but only for the length of one rolling upgrade, and
only if you run more than one API replica. Nothing you configure changes, and no
API, wire, config or Helm surface is removed.

The `terraform_version` column on `workspaces`, `runs` and `autodiscovery_rules`
becomes `engine_version` — it pins the version of whichever engine the workspace
runs, and two of the three engines Terrapod now supports are not Terraform. The
migration renames it in place.

**What that costs you.** Migrations run as a pre-upgrade hook, and the API rolls
with `maxSurge: 1, maxUnavailable: 0`, so between the hook finishing and the last
old pod being replaced there are replicas running code that selects a column no
longer there. Requests those pods serve in that window fail. It is seconds to a
couple of minutes, and it self-heals — no data is at risk and nothing needs
re-running.

**If you cannot take even that**, scale the API to one replica for the upgrade and
back up afterwards; a single replica is replaced rather than overlapped, so there
is no window at all.

Terrapod's normal rule is expand/contract — add the new column, dual-write, drop
the old one a release later — precisely so this window does not exist. It is
deliberately not followed here: three tables would each carry two columns, every
write path would have to set both, and the dual-write would have to stay correct
across a release boundary for a column nothing reads. Nothing outside the API
touches the database, and the API and the schema ship together, so the exposure is
that one rollout and nothing else.

**The API is not renamed.** `terraform-version` keeps being accepted and returned
alongside the canonical `engine-version`, permanently — go-tfe reads it by that
name. See [api-reference.md](api-reference.md#engine-version-and-its-older-name-terraform-version).

### `registry.platform_tools.pulumi_version` is removed — the version is per workspace

**Affects:** deployments running Pulumi workspaces. Terraform and OpenTofu are
untouched.

The Pulumi CLI version used to be one value for the whole deployment, pinned in
Helm. It is the workspace's now, in the same `engine-version` attribute a
Terraform workspace uses — so two Pulumi workspaces can sit on different CLI
versions, which is what every other per-workspace setting already allowed.

**What to do before upgrading:** delete
`api.config.registry.platform_tools.pulumi_version` and
`api.config.registry.platform_tools.pulumi_mirror_url` from your values. The
chart's schema rejects unknown keys, so leaving them in place fails the upgrade
rather than being ignored. If you were pinning a version, set
`api.config.default_pulumi_version` to it instead — that is what a workspace
gets when it pins none. The mirror moved rather than vanished: it is
`api.config.registry.binary_cache.pulumi_mirror_url` now, beside the other CLI
tools, with `pulumi_version_index_url` alongside it for partial-version
resolution.

**What happens to your existing Pulumi workspaces.** They carry a Terraform
version in that column — typically `1.12` — because the workspace-creation path
filled it in from `default_terraform_version` and nothing read it. Read as a
Pulumi version it is nonsense, and the first run would ask for a Pulumi 1.12
that has never existed. A migration clears it, so those workspaces take the
deployment default: the same behaviour they have had all along. Pin a version on
any workspace that wants a specific one.

**Air-gapped deployments:** the default version is still warmed for you, but a
workspace pinned to anything else now needs an explicit entry in the warm
manifest — the same rule that has always applied to a Terraform workspace on a
non-default version.

### The Python floor moves to 3.14

**Affects:** anyone who builds Terrapod's images themselves, overrides `BASE_IMAGE`,
or derives an image from one of ours. It does **not** affect operators deploying the
published images or Helm chart — those carry their own interpreter, and nothing about
the API, wire protocol, config, Helm values, or database schema changes.

Every image moves from `python:3.13-slim` to `python:3.14-slim`. If you build with a
`BASE_IMAGE` override, move it to a 3.14 base; if you derive an image and install
Python packages into it, note that site-packages is now
`/usr/local/lib/python3.14/site-packages`.

The 3.13 floor was never a preference — it was one dependency. `litellm` declared
`requires_python = <3.14` from 1.83.11, which pinned the whole project. That cap is
gone as of 1.99.0, so the floor moved with it, and 3.14 brings
[PEP 649](https://peps.python.org/pep-0649/) deferred annotation evaluation to a
codebase that leans heavily on typed models.

## Before you upgrade

1. Read the sections above and make the edits they name.
2. Run `terraform plan` (or `tofu plan`) against your Terrapod-managing
   configuration and confirm it is empty. A non-empty plan after a type-only edit
   means a value changed as well as its shape — check it before applying.
3. Upgrade the chart. Runner and listener images within the
   [supported skew window](versioning-and-support.md#component-version-skew)
   continue to work — **provided they are new enough to call `/api/v1`**. The
   alias they used before is removed in 2.0, so an image predating the minor
   that moved them cannot talk to a 2.0 API however recent it otherwise is. See
   "The `/api/terrapod/v1` alias is removed" above.
