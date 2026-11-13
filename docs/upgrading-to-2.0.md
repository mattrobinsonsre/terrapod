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

## Policy sets now evaluate on a configure

**If you use OPA policy sets and you have configure-enabled workspaces, read
this before upgrading.** Policy sets are selected by label, and there is no
configure opt-in: a set already enabled and in scope for a workspace now
evaluates on that workspace's **configures** as well as its plans. Before 2.0 a
configure was never gated, because it has no plan JSON for OPA to read.

What a configure hands a policy is the run's **resolved inventory document** --
`input.hosts`, `input.groups`, `input.children` -- not a Terraform plan. So:

| your policy | what happens on a configure |
|---|---|
| keyed on plan shapes (`input.resource_changes`, `input.planned_values`) | its rules do not match, `deny` is empty, the configure **passes**. No action needed |
| keyed on the ABSENCE of something (`deny[msg] { not input.terraform_version }`) | the rule **fires**, and on a `mandatory` set it **blocks the configure** |
| errors on an unexpected input shape | the set reports `errored`, which a `mandatory` set treats as a failure |

So the risk is confined to **mandatory** sets whose rules fire on absence or
error on shape. An advisory set can only produce a warning.

To check before upgrading, list your enabled sets and the configure-enabled
workspaces their labels reach, and read each policy for the two rows above. To
keep a set away from configures entirely, narrow its labels so it does not match
a configure-enabled workspace.

Writing a policy that is *meant* for a configure is covered in
[Policies -> The configure input](policies.md#the-configure-input).

## Pre-flight: the entries with an order

**The rest of this page is a catalogue, not a procedure.** Most of it can be read
and acted on whenever suits you. A handful of entries cannot, because the window
in which they are doable closes at the upgrade — and the sequencing for each is
stated inside its own section, where you reach it after the point at which it
mattered. Read front to back and acting as you go, the IdP re-registration below
arrives *after* you have locked yourself out of the UI.

So this is the ordered subset and nothing else. It is not a summary: an entry
absent from here needs no particular timing, and every row sends you to the full
section rather than restating it. Work through the condition in the middle column
first — most rows do not apply to most deployments.

### Before you upgrade, or you lose access

| Do this | If | Why it cannot wait |
|---|---|---|
| [Register the `/api/v1` SSO callback and SAML ACS URLs with your IdP](#sso-callback-urls-move-to-apiv1--re-register-them-with-your-idp-first), keeping the old ones | you use OIDC or SAML | the IdP validates the `redirect_uri` against its own allow-list, so there is nothing Terrapod can serve that makes the switch safe. Local admin is your only way back in |
| [Set the five SAML assertion checks to `true` on your current release and confirm a login](#the-five-saml-assertion-checks-are-strict-by-default) | you use SAML | they default to `true` from 2.0; a provider that cannot satisfy one stops being able to log in, and doing it beforehand means the defaults and your config already agree |
| [Check your provider sends `email_verified`](#an-oidc-login-needs-an-email-the-provider-vouches-for), and set `require_email_verified: false` if it does not | you use OIDC | a login whose claims omit the field is refused from 2.0, and some providers verify the address without sending it |
| [Grant `admin` / `audit` deliberately](#an-idp-group-can-no-longer-grant-admin-or-audit) — the entry's `SELECT` runs on your current release | your administrators get their role from an IdP group name | a bare group name stops granting a built-in role at the upgrade, and the grant is what you would need in order to put it back |
| [Supply a CA bundle, or pin `ssl_mode: require`](#cloud-iam-database-auth-verifies-the-server-by-default) | `database.auth_mode` is one of the cloud-IAM modes **and** you have not set `database.ssl_mode` | the mode becomes `verify-full`, and verification needs CA material the system trust store may not carry — the API then cannot reach its database at all. **This is the one row whose own section does not frame it as a sequencing step**; it reads as a configuration note and has the blast radius of one |
| [Upgrade every listener in every pool first, or set `require_listener_proof_of_possession: false`](#listeners-must-prove-they-hold-their-certificates-private-key) | you run server-side execution and a pool may still hold a pre-2.0 listener image | a listener that does not sign gets `401` on every call including the event stream, so **the pool goes quiet rather than noisy**: nothing connects, nothing claims a run, and no error says why |

### Before you upgrade, or something breaks quietly

| Do this | If | Why it cannot wait |
|---|---|---|
| Remove the pre-release-only Helm keys — [`engines:`](#the-engine-onoff-switch-is-removed), [`default_ansible_version`](#apiconfigdefault_ansible_version-is-removed), [`registry.platform_tools.pulumi_version`](#registryplatform_toolspulumi_version-is-removed--the-version-is-per-workspace) | your `values.yaml` was written against a 2.0 pre-release build. **An upgrade from 1.x has nothing to do here** — none of the three ever shipped in a 1.x chart | `values.schema.json` refuses keys it does not declare, so `helm lint` and the `helm upgrade` itself fail rather than ignoring them. Loud, but it fails the upgrade |
| Move automation off the shapes that change: [a `status == "planning"` branch](#a-run-held-after-its-plan-reports-terraform-enterprises-statuses), [`ListRunPolicyEvaluations` and `IsBlocking()`](#policyevaluation-becomes-policycheck-in-the-go-sdk), and [a literal `queued` or `canceling` read from `/api/tfe/v2`](#a-runs-status-on-apitfev2-is-always-one-go-tfe-defines) | you have scripts or Go programs that read a run's status or its policy results | each is silent: a branch that no longer fires, a `switch` that falls through every case. The first two can be tried ahead of time on your current release |
| [Rename any label key `flag` or `locked`](#the-workspace-filter-has-flag-and-locked-and-status-is-only-the-runs-status), and move bookmarked `status:locked` / `status:unhealthy` filters | a workspace, agent pool, registry module or provider, or autodiscovery rule carries a label named `flag` or `locked`, or you share filter URLs | the next create or update of that resource that includes the key is refused with a `422`; existing rows keep working until then |
| [Check your own EE images can function as uid 1000](#an-execution-environment-must-run-as-uid-1000) | you set `api.config.ansible.execution_environments`. Terrapod's own published images need nothing | the sidecar's startup probe asserts it, so a non-conforming image fails at pod start — which is better than mid-playbook, but it is still every configure on that workspace |
| [Grant module `registry:read`](#downloading-a-protected-configuration-version-archive-needs-module-registryread-too) | anyone downloads configuration-version archives from workspaces provisioned by a **self-contained** catalog item | the grant exists on your current release, so doing it first means nobody meets the new `403` |
| [Point consumers at the catalog surface](#a-human-read-of-a-catalog-instances-variables-no-longer-returns-their-values) | a script or an agent reads `/variables` on a catalog-provisioned workspace and uses the values | the route, the attribute names and the status code are all unchanged — only the content becomes `null`, so nothing reports it |
| [Stop reading a catalog item's authored `default` and `options`](#reading-a-catalog-item-no-longer-returns-its-authored-defaults-and-choices) | a script, an agent or a pre-2.0 provider binary reads `/catalog-items` and uses `variable-options[].default`, its `options` list, or a provider template's `body` | the same shape as the row above and for the same reason — an ordinary read is projected to six keys, so a consumer that copied a default starts copying nothing. Provisioning and reconfiguring are unaffected, which is what makes it quiet |
| [Assign the set, make it global, or grant `audit`](#listing-variable-sets-is-filtered-by-workspace-reach) | a **non-admin** needs to see a variable set that is assigned to no workspace | a set that reaches no workspace becomes visible to `admin` and `audit` only, and a read by id answers `404` rather than `403` |
| [Move a configuration that branches on the run phase](#tf_var_terrapod_run_phase-is-no-longer-exported) onto the token's `phase` claim or an apply-only variable | a configuration declares `variable "terrapod_run_phase"` **and** the workspace uses per-workspace cloud identity | the export is gone the moment you upgrade, so from then on the variable takes its `default` — the branch still evaluates, and picks the same side in both phases. The trust-policy route is already the recommended one and works on your current release |
| [Look for a key held in more than one category](#a-variable-is-identified-by-its-category-and-key-together) across a workspace and the variable sets reaching it | you have variable sets | nothing breaks, but a variable set entry that a workspace variable of the same key used to make *absent from the run* will now be delivered. Knowing which runs change is only cheap beforehand |

### Decide before you start

| Decide | Why it is a decision rather than a step |
|---|---|
| [How many API replicas to run through the rollout](#two-columns-are-renamed-so-the-api-rollout-has-a-brief-window-of-errors) | two columns are renamed in place, so between the pre-upgrade migration hook and the last old pod being replaced there are replicas selecting a column that is gone. It is seconds to a couple of minutes and it self-heals — but the mitigation is to scale the API to **one** replica, and a single replica is replaced rather than overlapped, so there is no window at all. That has to be chosen before you begin, not after you notice |
| [Whether `require_strong_secrets` stays on](#terrapod-owns-the-token-signing-key-and-a-used-deployment-adopts-its-old-one) | a deployment that has executed runs **adopts** its old derived signing key, and that switch now fires on an adopted key — which is exactly the state the upgrade lands in. So either supply `api.tokenSigningKey` at the same time as the upgrade, or leave the switch off until you have |
| [Whether you may need to roll back](#a-variable-is-identified-by-its-category-and-key-together) | the downgrade removes variables a 1.x schema cannot represent — the surplus when one key is held in two categories — keeping the oldest row per key and printing each removal. It is a rollback that costs something rather than one that refuses, which is the right trade and worth knowing in advance |

### Only possible after the upgrade

| Do this | Why not before |
|---|---|
| [Find the API tokens with no recorded provider and re-mint them](#an-api-tokens-roles-are-resolved-against-the-provider-it-was-minted-under) | that entry's `SELECT` reads `api_tokens.identity_provider`, and the column is **added by a 2.0 migration** — so the query cannot run until the migration has. Until they are re-minted, automation using one resolves to no roles and gets `403` |
| Confirm an SSO login works, then [remove the old callback URLs from your IdP](#sso-callback-urls-move-to-apiv1--re-register-them-with-your-idp-first) | with both values registered the upgrade is a no-op at the IdP, and the old ones are what a fallback to `legacy_callback_url: true` would need. Removing them is the last step, and it is optional |
| [Remove `require_listener_proof_of_possession: false`](#listeners-must-prove-they-hold-their-certificates-private-key) once every pool is on a 2.0 listener | it is a relaxation held open for the rollout, and it is the kind that stays set for a year if nobody writes it down |
| [Delete the old `<release>-token-signing` Secret](#terrapod-owns-the-token-signing-key-and-a-used-deployment-adopts-its-old-one) — **only after confirming you do not reference it** from `api.tokenSigningKey.existingSecret` | Helm leaves it behind on purpose (`helm.sh/resource-policy: keep`). It is inert once you are on 2.0 **unless it is the key you supply**, in which case it is live |

Two things to expect rather than do: [sessions open across the upgrade are capped
from their own sign-in
time](#a-web-session-ends-when-the-roles-behind-it-change-and-has-a-hard-ceiling),
so some will end shortly after you upgrade rather than at the 24-hour mark; and
[the UI opens in the light
theme](#the-uis-default-theme-is-light-and-dark-is-a-per-user-toggle), which no gate,
snapshot or test reports because nothing fails.

### Two orderings that are **not** tied to the upgrade

Worth saying, because both look like upgrade steps and are not:

- **The deprecated path aliases are not removed in 2.0**, so there is
  [nothing to do first](#the-deprecated-path-aliases-are-still-served--they-are-not-removed-in-20)
  and no deadline here. The one ordering that does apply is within your own
  migration: re-register a webhook against the canonical path **before** dropping
  the alias entries from `webhookIngress.paths`, or every webhook registered
  against the old path breaks.
- **The client-side type changes move when you move the client**, not when you
  move the server: [label rules becoming a list per
  key](#label-rules-take-a-list-of-values-per-key), [an explicit `= ""` clearing
  an optional string](#an-explicit---clears-an-optional-string-and-removing-the-line-no-longer-does),
  and [`GetVariableByKey` taking a
  category](#a-variable-is-identified-by-its-category-and-key-together). The
  first is a type in HCL over a shape the server always stored and still accepts
  either way; the second is settled inside the provider; the third is a filter
  the SDK applies to a list it already fetches. So none of the three obliges you
  to move the server and your clients together — see
  [versioning-and-support.md](versioning-and-support.md) for the skew Terrapod
  does promise.

## Breaking changes

### `TF_VAR_terrapod_run_phase` is no longer exported

**Affects:** a configuration that declares `variable "terrapod_run_phase"`, on a
workspace using per-workspace cloud identity. If you do not use that feature, or
do not declare that variable, nothing changes.

Until 2.0 the runner exported `TF_VAR_terrapod_run_phase` alongside
`TERRAPOD_RUN_PHASE` on any run that was delivered a token. It was the only
place Terrapod reached into an operator's own Terraform variable namespace —
every other variable it supplies arrives as a tfvars file, and every other
environment variable it sets is either `TERRAPOD_`-prefixed or belongs to the
tool it configures.

It was also a trap rather than a convenience. The runner applies the *saved*
plan, so an ordinary variable's recorded `plan` value conflicted with the
`apply` supplied at apply time and the engine refused the apply:

```
Error: Mismatch between input and plan variable value
```

That happened **whether or not the configuration used the variable**, so merely
declaring it broke every apply on a federated run unless it was also marked
`ephemeral = true` — and ephemeral variables may be used in provider
configuration only from OpenTofu 1.11 / Terraform 1.10, so on an older engine
the pattern could not be used at all.

**What to do instead**, in order of preference:

| you want | do this |
|---|---|
| one role, different permissions per phase | condition the role's trust policy on the **`phase` claim** the token already carries. The cloud does the switching; the configuration carries one role ARN and no conditional. This was already the recommended route |
| two genuinely distinct roles (two accounts, say) | hold the role ARN twice under one key as an **apply-only variable** — a standard row with the plan role, an `apply-only` row with the apply role. The apply-only row shadows the standard one within its tier, so the configuration is still just `var.role_arn` |
| the phase in a shell hook or an `external` program | `TERRAPOD_RUN_PHASE`, unchanged |

If you were already using the `ephemeral = true` pattern, delete the `variable`
block and the conditional and move the choice into the trust policy or into an
apply-only variable. Leaving the declaration in place is harmless once the
export is gone — nothing supplies it, so it takes its `default`.

### The deprecated path aliases are still served — they are not removed in 2.0

**Affects:** nobody at the upgrade. It is listed because "are the aliases gone in
2.0?" is the most consequential compatibility question about this release, and the
answer is no.

All three keep serving, exactly as they do on 1.x:

| still served | canonical path to move to |
|---|---|
| `/api/terrapod/v1/…` | `/api/v1/…` |
| `/api/v2/…` | `/api/tfe/v2/…` |
| `/v1/providers/…` | `/api/v1/provider-mirror/…` |

Each is, in the API's own words, "a deprecated alias, kept for the support window
because our own runner and listener images construct it directly and are expected
to lag the API by minors". Both prefixes are mounted from one helper and there is
no switch that turns the alias off, so a route cannot exist on one and not the
other.

**Nothing breaks when you upgrade, and there is no step to take first.** Move your
own scripts, `curl` calls and dashboards onto `/api/v1` and `/api/tfe/v2` at
whatever pace suits you; until you do, the old paths answer as before. `terraform`
and `tofu` need nothing either way — they take the path from service discovery,
and 2.0.0 is the release that starts advertising the canonical one: 1.x advertised
`/api/v2/`, so the CLI follows the move by itself the first time it talks to an
upgraded deployment.

**The sunset waits on Terrapod, not on the major version.** Terrapod's own
consumers still call the alias, by design: the runner and listener images, the HA
peer-replication calls, `go-terrapod`, the Terraform provider, `terrapod-migrate`
and the MCP server. Removing the alias while they do would break them, so they move
onto `/api/v1` in a later 1.x minor first, and the sunset follows that migration
rather than the version number.

**[`deprecations.md`](deprecations.md) is the schedule, and the only place it is
written down.** It records the alias as removed *no earlier than* the published
sunset — a floor rather than a date, precisely because the prerequisite above has
to land first. Read the schedule there rather than from a second copy here: a date
restated in two places is how this page came to describe a removal as done.

**Do not drop the alias entries from `webhookIngress.paths`.** The chart ships both
prefixes for `vcs-events` and `task-stage-results`, and the alias entries are what
keep already-registered webhooks working. Removing them breaks every webhook
registered before you re-register it against the canonical path.

**The Prometheus `path_template` label still reports the alias** for both prefixes,
so existing dashboards and alerts keep matching. It folds onto the legacy name on
purpose — an alert holding a path literal is the same kind of consumer as an IdP
allow-list, so renaming it mid-window would blank a panel nobody asked to change —
and it is scheduled to flip with the aliases rather than ahead of them. Panels
matching `/api/terrapod/v1/` or `/api/v2/` literally are the ones to revisit then.

**How to find what still uses it:** the alias returns `Deprecation: true` and a
`Sunset` date on every response, so a client hitting it is visible in your own
logs and proxies.

### SSO callback URLs move to `/api/v1` — re-register them with your IdP FIRST

**Affects:** every deployment using OIDC or SAML. **This one will lock you out of
the UI if you skip it**, so it is listed first deliberately.

`api.config.auth.legacy_callback_url` selects which prefix Terrapod builds its
IdP-facing URLs on, and **its default is now `false`** — it was `true` through
1.x, and 2.0.0 flipped it, so those URLs move to the canonical `/api/v1` the
moment you upgrade. **This is not a change to prepare for at your leisure: it
takes effect on the upgrade itself**, so do step 1 below before you roll the
new version, or SSO stops working until you do.

What moves:

```
before:  {callback_base_url}/api/terrapod/v1/auth/callback
after:   {callback_base_url}/api/v1/auth/callback

before:  {callback_base_url}/api/terrapod/v1/auth/saml/acs    (SAML only)
after:   {callback_base_url}/api/v1/auth/saml/acs
```

**Why serving both prefixes does not save you.** 2.0.0 serves `/api/v1` and
`/api/terrapod/v1` side by side, and for every other path that is the whole
story — a consumer still calling the alias is unaffected. These two are
different: they are **registered**
with your identity provider, which validates the `redirect_uri` Terrapod sends
against its own allow-list. An unrecognised value is refused **at the IdP**,
before any request reaches Terrapod — so there is nothing Terrapod can serve,
alias or not, that makes the switch safe on its own. Every SSO login fails with
an IdP-side error, and local admin login is your only way back in.

Note that `/api/v1` is **new in 2.0.0** — no 1.x release served it — so you cannot
verify the new callback URL against your current deployment before upgrading, and a
deployment with `auth.local_enabled: false` has no local account to fall back on.
Register the new URL anyway: an unused entry in an IdP allow-list is harmless, and
with both listed the upgrade is a no-op at the IdP.

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

The same edit applies to `deny_labels`. **The provider upgrades your stored
state for you** — it declares schema version 1 and rewrites each `"dev"` as
`["dev"]` when it first reads a state written by a 1.x provider. There is
nothing to run and nothing to import.

> An earlier revision of this page said "there is no state migration to run:
> the values are unchanged, only their type in HCL". The values are indeed
> unchanged, but their type in **state** changed too, and until the upgrader
> existed a 1.x state could not be read at all: every plan failed with
> `Unable to Read Previously Saved State for UpgradeResourceState`, and it
> failed even once the configuration had been corrected. That was #2452, fixed
> for both `terrapod_role` and `terrapod_policy_set`.

Once you are on the new type, the
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

### A pull request gets one Terrapod comment, not one per workspace per push

**Affects:** every VCS-connected deployment. Nothing to change before
upgrading; this is a behaviour change to read about rather than act on.

A pull request used to accumulate two kinds of Terrapod comment with opposite
update semantics. The status table was edited in place for ever. The
per-workspace comment — the one carrying the AI plan summary, the gate verdicts
and the run link — had the commit SHA in its hidden identity marker, so a push
could never match the previous comment and always posted another. The
per-workspace comments therefore multiplied as *workspaces x pushes*: four
workspaces and four pushes measured seventeen Terrapod comments on one pull
request.

There is one comment now, and everything is in it. Each affected workspace is a
row of the table, and that row's AI summary and gate verdicts share the
workspace's own collapsed block beneath it, with the risk level and the first
failing gate in the summary line so the whole pull request triages without
expanding anything. A push edits that comment.

**Module pull requests change most.** Their comment was keyed on the workspace
id, so a module with ten linked workspaces put ten comments on one pull
request, each reposted on every push for the same reason. Every consuming
workspace is a row of one comment now.

**What happens to comments already posted.** They carry the old marker and will
never be matched again, so an open pull request keeps whatever per-workspace
comments it has collected and they stop updating. No cleanup is attempted:
Terrapod posts its one comment and edits that from then on. The stale ones are
harmless and finite — delete them by hand if they bother you, or let the pull
request merge. New pull requests after the upgrade only ever see one comment.

**Per-push visibility is unchanged**, which is what the per-commit identity was
introduced for. The commit status is still posted per commit, and a `terrapod
...` command is still acknowledged with a reaction on receipt and a reply when
it is dropped — so "did Terrapod see my push" and "did Terrapod see my command"
are both still answered next to the thing you did, without a comment per push.

### What does not break

- **The HTTP API accepts both shapes.** A scalar value is read as a one-element
  list, so existing automation posting `{"env": "dev"}` keeps working and needs no
  change.
- **Existing roles and policy sets are untouched.** Nothing is rewritten in the
  database; both shapes have always been valid there.
- **The web UI needs nothing.** The roles form now accumulates a repeated key
  instead of refusing it, and the policy-set form already supported several values
  per key.

### The workspace filter has `flag:` and `locked:`, and `status:` is only the run's status

**Affects:** anyone who bookmarks or shares a Workspaces filter containing
`status:locked` or `status:unhealthy`, and any deployment that uses `flag` or
`locked` as a **label key**. It does not change the HTTP API, and it changes no
stored data.

`status:` used to carry two values that were not statuses at all. `status:locked`
matched a workspace's manual lock, and `status:unhealthy` matched "has at least
one health condition". They rode `status:` because reserving a new label key takes
that word away from every operator already using it as a label. The cost was that
`status:` stopped being one set of mutually exclusive values, which is what made it
impossible to let two `status:` terms mean "either": `status:locked status:errored`
was "locked **and** errored".

They are gone from `status:`, which now means only the run's status (thirteen
values, exactly one per workspace):

| Before | Now |
|---|---|
| `status:locked` | `locked:true` (and `locked:false` comes with it) |
| `status:unhealthy` | `flag:state-diverged flag:no-agent-pool flag:no-live-agent-pool flag:vcs-error flag:run-errored flag:drifted flag:drift-errored` |

The **Health Issues** and **Locked** indicators on the workspace list insert
exactly these, so clicking one shows what it means and lets you delete a condition
from it. The `flag:` terms union, so the result is the set `status:unhealthy`
matched.

**Two label keys become reserved.** `flag` and `locked` can no longer be used as
label keys, for the same reason `status` and `owner` cannot. `locked` was released
in 1.6.0 on the grounds that `status:locked` answered it; that spelling is gone, so
the reason is gone with it. A resource that already carries one of these keys
keeps working, but the next create or update that includes the key is refused with
a `422`. Rename the label first:

```sh
# workspaces, agent pools, registry modules and providers, autodiscovery rules
# and VCS connections all take labels; list the ones carrying either key
curl -s -H "Authorization: Bearer $TOKEN" "$TERRAPOD_URL/api/v1/workspaces" \
  | jq -r '.data[] | select(.attributes.labels | has("flag") or has("locked")) | .attributes.name'
```

That example finds workspaces only; check the other resource types the same way.

**What else changes for the better.** The Status dropdown now lists the run
statuses only. `state-diverged`, `vcs-error` and `drifted` moved to the new
**Flags** dropdown, where they are matched against the server's `health-conditions`
array instead of a derived status. That array is authoritative, and it fixes two
disagreements: a workspace awaiting confirmation shows `needs-confirm` and used to
hide an underlying `state-diverged` from `status:state-diverged`, and a workspace
with drift detection turned off but a stale `drift-status` showed the Drifted pill
with no drifted condition. The status **pill** is unchanged and still shows all
sixteen with its existing precedence. Three conditions (`no-agent-pool`,
`no-live-agent-pool`, `drift-errored`) had no filter at all before; the Flags
dropdown is the first way to ask for them individually.

**What this cannot do.** Because `flag:a flag:b` means "either", asking for two
conditions *together* is not possible. It was not before either: the roll-up was
all-or-nothing.

### A run held after its plan reports Terraform Enterprise's statuses

**Affects:** anything that reads a run's `status` and treats `planning` as "not
finished yet", and anything that expects a failed mandatory run task to error a
run. The `tofu`/`terraform` CLI is not affected, except that it now does the
right thing.

`api.config.runs.tfe_post_plan_decisions` selects this reporting, and **its
default is now `true`** — it was `false` through 1.x, and 2.0.0 flipped it, so
the new statuses arrive on the upgrade rather than when you opt in. Nothing is
locked out by it; what changes is the `status` a script or dashboard reads back
for a held run. Set it to `false` explicitly to keep the 1.x vocabulary. A run
that a post-plan gate holds now reports:

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

### `PolicyEvaluation` becomes `PolicyCheck` in the Go SDK

**Affects:** Go code importing `go-terrapod` that reads a run's policy results.
Nothing else — no API route is removed, the HTTP surface gains rather than
loses, and neither the `tofu`/`terraform` CLI nor the Terraform provider is
affected.

The SDK reported one `PolicyEvaluation` per policy **set** evaluated. It now
reports one `PolicyCheck` per **gate** per run, in the Terraform Enterprise
shape the CLI reads — one for the OPA policy sets (`Scope` `organization`) and
one for the security scan (`Scope` `workspace`), each present only when that
gate recorded something. The per-set rows did not disappear; they moved to where
a caller wanting that level of detail now asks.

| removed | use instead |
|---|---|
| `ListRunPolicyEvaluations(ctx, runID)` | `ListRunPolicyChecks(ctx, runID)` |
| `type PolicyEvaluation` | `type PolicyCheck` — `Status`, `Scope`, `IsOverridable`, `CanOverride`, and the `Passed` / `SoftFailed` / `AdvisoryFailed` counts |
| `(*PolicyEvaluation) IsBlocking()` | `check.Status == terrapod.PolicyCheckSoftFailed` |
| `PolicySetID`, `PolicySetName`, `EnforcementLevel`, `Outcome`, `OverriddenBy`, `OverriddenAt` | the same six fields on `PolicyEvaluationDetail`, via `RunComplianceReport.PolicyEvaluations` |
| `Result map[string]any` (the raw OPA result) | **no typed equivalent.** `GetPolicyCheckOutput(ctx, checkID)` returns the same information as plain text, per policy set and per policy |

**The `IsBlocking` replacement is exact, not approximate.** The old method
returned true when an evaluation was `mandatory`, had outcome `failed` or
`errored`, and had not been overridden. The server now applies that identical
predicate across a run's evaluations and reports `soft_failed` when any of them
matches, so one status read replaces a loop. Once such a check is overridden the
status becomes `overridden`, which the old field combination expressed as
`OverriddenBy` being set.

Three methods are **new** rather than renamed: `GetPolicyCheck`,
`GetPolicyCheckOutput` and `OverridePolicyCheck` — the last being how a caller
clears a held run, which previously had no SDK route at all.

**What you must do before upgrading:**

1. Replace `ListRunPolicyEvaluations` with `ListRunPolicyChecks`, and any
   `IsBlocking()` loop with the single `Status` comparison above.
2. If you read `PolicySetName` or `EnforcementLevel` per set, move that read to
   `RunComplianceReport.PolicyEvaluations`; it is a batched call, so fetching a
   report is cheaper than the per-run listing it replaces.
3. If you parsed `Result` to find which policy denied, read
   `GetPolicyCheckOutput` instead and expect text rather than a map. There is no
   typed form of the raw OPA result, deliberately — its shape is OPA's and not
   ours to freeze in an SDK contract.

See [policies.md](policies.md).

### The `auto_apply` boolean is removed — one setting, not two

**Affects:** HCL or Pulumi programs that set `auto_apply` on `terrapod_workspace`
or `terrapod_autodiscovery_rule`, or read it off the `terrapod_workspace` data
source; anything reading `auto-apply` from a workspace or autodiscovery-rule
document on **`/api/v1`**; and Go code using go-terrapod's
`Workspace.AutoApply`, `CreateWorkspaceRequest.AutoApply` or
`UpdateWorkspaceRequest.AutoApply`, or the MCP `auto_apply` field on
`terrapod_workspace_create` / `terrapod_workspace_update`.

**Not affected:** the `tofu`/`terraform` CLI and anything else speaking
`/api/tfe/v2`, where `auto-apply` is still returned (see below); the run
document's own `auto-apply`, which is what a particular run decided rather than
a workspace setting; and `terrapod_catalog_instance.auto_apply`, which is a
different setting again (see below).

One setting was described by two attributes: `auto_apply_mode`
(`never` / `always` / `create` / `create_update`) and an `auto_apply` boolean
that was its projection. Two attributes for one setting let a configuration
state a contradiction:

```hcl
resource "terrapod_workspace" "app" {
  name            = "app-prod"
  auto_apply      = false
  auto_apply_mode = "always"   # accepted, then resolved to "always"
}
```

which was accepted without complaint, resolved towards **more** automation than
the configuration asked for, and then failed the apply with "Provider produced
inconsistent result after apply" — by which point the workspace had already been
changed to auto-apply. With one attribute the contradiction cannot be written
down.

**What you must do before upgrading the provider:**

1. Delete `auto_apply` from every `terrapod_workspace` and
   `terrapod_autodiscovery_rule` block and state the mode instead —
   `auto_apply = true` becomes `auto_apply_mode = "always"`, and
   `auto_apply = false` becomes `auto_apply_mode = "never"`.
2. Repoint anything reading `data.terrapod_workspace.<name>.auto_apply` at
   `.auto_apply_mode`, comparing against `"never"` rather than against `false`.
   Note that attribute previously reported a **blank** string — it was declared
   and never populated — so a program that read it and found nothing was reading
   the defect, not an unset workspace.
3. Repoint anything reading `auto-apply` off a **`/api/v1`** workspace or
   autodiscovery-rule document at `auto-apply-mode`, again comparing against
   `"never"`. The key is gone from that surface; it is still on `/api/tfe/v2`.
4. In Go, replace go-terrapod's `AutoApply` with `AutoApplyMode` — a
   `*string` on both request types, and a `string` on `Workspace`. In an MCP
   client, replace `auto_apply` with `auto_apply_mode`.

A configuration that still names the boolean fails at **plan** time, before any
request is made, with Terraform's own *An argument named "auto_apply" is not
expected here*. Existing state needs no migration: the plugin framework ignores
a prior-state field the current schema no longer defines, so the stale value is
dropped on the next read rather than erroring.

**Writing `auto-apply` to the API still works, on every write path.** Create,
PATCH, bulk update and the autodiscovery-rule routes all accept the boolean and
map it onto a mode (`true` → `always`, `false` → `never`), because a client
written against the compatibility surface should not be refused for a change
that is ours. Sending **both** spellings in one request is still a `422` rather
than a guess — which is the whole point of the removal, so do not send the pair
to be safe.

**`/api/tfe/v2` still returns `auto-apply`.** It is derived as
`auto-apply-mode != "never"` instead of being stored, which is what the boolean
has always meant. That surface is frozen for `go-tfe` and the cloud backend,
both of which read the key by name.

#### The database column goes with it, and the rollout is briefly unavailable

The migration drops `workspaces.auto_apply` and `autodiscovery_rules.auto_apply`
(`runs.auto_apply` stays). It is a **contraction**, so unlike every other
migration in this release it cannot be rolled through transparently:

* **Stop 1.x API replicas before migrating, or accept a window of errors.** A
  1.11 replica reads `workspaces.auto_apply` on every workspace query, and
  1.11's readiness check has no handling for a schema that is *ahead* of its
  code — so it stays in the load balancer and fails those queries until it is
  replaced. This is stated as an expectation rather than worked around: it is
  the one shape of change that cannot be made invisible to a pod that predates
  it.
* **After the upgrade, 2.0 pods protect themselves.** The migration records
  itself in `schema_contractions`, and a 2.0 pod that finds a contraction
  revision outside its own revision graph refuses readiness rather than serving
  against a schema it cannot read. That covers every later contraction
  automatically.
* **Rolling back is supported and lossless.** `auto_apply_mode` is not
  touched, and `downgrade()` recreates the boolean and derives it from the mode
  exactly as every write path did before the drop — so a workspace on `create`
  or `create_update` comes back on that mode, not flattened to `always`. 1.x
  reads the pair and resolves it as it always did.

**`terrapod_catalog_instance.auto_apply` is unchanged.** It is not this setting:
it says whether the provision or reconfigure run that resource queues applies
without waiting for a human, which is a property of that operation rather than a
standing workspace setting.

See [terraform-provider.md](terraform-provider.md#auto_apply_mode).

### Two columns are renamed, so the API rollout has a brief window of errors

**Affects:** every deployment, but only for the length of one rolling upgrade, and
only if you run more than one API replica. Nothing you configure changes, and no
API, wire, config or Helm surface is removed.

Two migrations rename a column in place rather than adding one and dropping the
old a release later:

| renamed | on | why |
|---|---|---|
| `terraform_version` → `engine_version` | `workspaces`, `runs`, `autodiscovery_rules` | it pins the version of whichever engine the workspace runs, and two of the three engines Terrapod now supports are not Terraform |
| `hcl` → `structured` | `variables`, `variable_set_variables` | the flag answers whether a value is a raw expression rather than a string, which is the same question for Pulumi's stack config and Ansible's extra vars as it is for Terraform |

**What that costs you.** Migrations run as a pre-upgrade hook, so the schema is
renamed before any 2.0 pod exists. A 1.x pod then finds a revision its own
migrations have never heard of and reports **not ready**, so it is pulled from
the load balancer rather than left serving requests against a column that is no
longer there. The API is therefore *unavailable* from the moment the hook
completes until the first 2.0 pod passes its readiness probe — seconds to a
couple of minutes. It self-heals, no data is at risk, and nothing needs
re-running.

Unavailable is the better of the two outcomes: no request is served a wrong
answer and no write lands half-applied against a renamed column. But it is a
gap, not a trickle of errors, and it is worth planning for if you front Terrapod
with something that alarms on a hard outage.

**Scaling the API down does not remove the gap**, which an earlier version of
this page said it would. The hook runs before any new pod is created, so a single
replica is just as unready as three; what decides the length of the gap is how
fast one 2.0 pod reaches readiness. If you need a genuinely silent upgrade, take
a deliberate window: stop traffic, upgrade, resume.

**This is the last upgrade that behaves this way.** From 2.0 onward an API pod
stays ready when the schema is merely *ahead* of its code, which is what makes a
rolling upgrade zero-downtime for an additive migration (#2500). The fix lives in
2.0's image, so it cannot help the upgrade *into* 2.0 — the pods being replaced
are 1.x pods running 1.x code. A later release that genuinely removes a column
records itself in `schema_contractions`, and pods that predate it go not-ready
deliberately, as these ones do.

Terrapod's normal rule is expand/contract — add the new column, dual-write, drop
the old one a release later — precisely so this window does not exist. It is
deliberately not followed here: each of the five tables would carry two columns,
every write path would have to set both, and the dual-write would have to stay
correct across a release boundary for a column nothing reads. Nothing outside the
API touches the database, and the API and the schema ship together, so the
exposure is that one rollout and nothing else.

**The API is not renamed.** `terraform-version` keeps being accepted and returned
alongside the canonical `engine-version`, permanently — go-tfe reads it by that
name. See [api-reference.md](api-reference.md#engine-version-and-its-older-name-terraform-version).

### `registry.platform_tools.pulumi_version` is removed — the version is per workspace

**Affects:** a `values.yaml` written against a pre-release build of 2.0. **Neither
key ever shipped in a 1.x release** — Pulumi arrived during 2.0 development, and no
1.x chart carried either key — so an upgrade from 1.x has nothing to remove here.
The rest of this section is recorded for anyone who set them on a pre-release
build. Terraform and OpenTofu are untouched either way.

The Pulumi CLI version used to be one value for the whole deployment, pinned in
Helm. It is the workspace's now, in the same `engine-version` attribute a
Terraform workspace uses — so two Pulumi workspaces can sit on different CLI
versions, which is what every other per-workspace setting already allowed.

**If you set either key on a pre-release build:** delete
`api.config.registry.platform_tools.pulumi_version` and
`api.config.registry.platform_tools.pulumi_mirror_url` from your values. The
chart's schema rejects unknown keys, so leaving them in place fails `helm lint`
and the upgrade rather than being ignored. If you were pinning a version, set
`api.config.default_pulumi_version` to it instead — that is what a workspace
gets when it pins none. The mirror moved rather than vanished: it is
`api.config.registry.binary_cache.pulumi_mirror_url` now, beside the other CLI
tools, with `pulumi_version_index_url` alongside it for partial-version
resolution.

**What happens to a pre-release deployment's Pulumi workspaces.** They carry a
Terraform version in that column — typically `1.12` — because the
workspace-creation path filled it in from `default_terraform_version` and nothing
read it. Read as a Pulumi version it is nonsense, and the first run would ask for
a Pulumi 1.12 that has never existed. A migration clears it, so those workspaces
take the deployment default: the same behaviour they have had all along. Pin a
version on any workspace that wants a specific one.

**Air-gapped deployments:** the default version is still warmed for you, but a
workspace pinned to anything else now needs an explicit entry in the warm
manifest — the same rule that has always applied to a Terraform workspace on a
non-default version.

### Cloud-IAM database auth verifies the server by default

If `database.auth_mode` is one of the cloud-IAM modes and you have not set
`database.ssl_mode`, the TLS mode changes from `require` (encrypt, do not
verify the server) to `verify-full` (verify the certificate chain and the
hostname).

**Why it changed.** `require` is a faithful implementation of libpq's mode of
that name, and as a Postgres connection mode it is correct. What made it the
wrong default here is specific to IAM auth: the short-lived cloud token is sent
**as the database password**, so the connection that was not verifying its peer
was the one carrying a cloud credential. Anyone able to answer for the database
host could harvest it, and nothing about that was visible to you. AWS's own
guidance for RDS IAM authentication is `verify-full`, for the same reason.

**What you may need to do.** Verification needs CA material. Some managed
databases chain to a root the system trust store already carries and will keep
working untouched; others do not — the AWS RDS bundle is the common case. If a
connection now fails to verify, point `database.ssl_root_cert` at your
provider's CA bundle:

```yaml
api:
  config:
    database:
      ssl_root_cert: /etc/ssl/rds/global-bundle.pem   # mounted via api.extraVolumes
```

The API logs a warning at startup whenever a verifying mode is in effect with no
`ssl_root_cert`, naming both keys, so you do not have to work backwards from a
TLS error.

**To keep the old behaviour**, set it explicitly:

```yaml
api:
  config:
    database:
      ssl_mode: require
```

That is a deliberate choice to encrypt without verifying, and it is available on
purpose — but on a connection that carries a cloud credential, prefer supplying
the CA bundle.

Deployments using `auth_mode: password` or a Kubernetes secret are unaffected;
this module is inert unless an IAM mode is selected.

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

### A variable is identified by its category and key together

A variable used to be identified by its **key alone**, enforced by a unique
constraint on `(workspace_id, key)` and again when variables were resolved for a
run. Category was an attribute hanging off the variable rather than part of its
name. Identity is now `(category, key)`.

**Nothing you have breaks, and you need change nothing to upgrade.** The database
constraint is widened, not narrowed, so every existing variable already satisfies
it; no values move and nothing is deleted. What changes is what becomes possible,
and one behaviour you may have been relying on without knowing.

**You can now hold the same key in two categories.** Previously a workspace could
have only one variable named `region` whatever its category, so an input variable
and an environment variable could not share a name — an ordinary thing to want,
and refused. You had to delete one first, on a live workspace, with no way to
stage the change. Both can now exist.

**A variable set variable is no longer silently dropped by a workspace variable
of the same key in a different category.** This is the part worth checking. When
a variable set supplied `env:region` and the workspace also had `terraform:region`,
the workspace one replaced the set's entry entirely — the environment variable was
*absent from the run*, not merely lower precedence. After the upgrade both are
delivered. If a run of yours depended on that disappearance, it will now see an
environment variable it did not see before.

To find out whether this affects you, look for a key that exists in more than one
category across a workspace and the variable sets reaching it. Precedence itself
is unchanged — priority sets beat workspace variables beat non-priority sets — it
simply now applies within a category rather than across all of them.

**If you roll back, variables that only the new identity could hold are
deleted.** A 1.x schema cannot represent two categories sharing a key, so the
downgrade removes the surplus rather than refusing — a rollback you cannot rely
on is worse than one that costs something. The rule is that the **oldest row per
key survives**: a collision can only exist because the second variable was added
after upgrading, so what remains is exactly what you had before. Each removal is
printed by the migration. If you have staged an engine migration this way and
then roll back, you will need to add those variables again.

**If you use `go-terrapod` directly**, `GetVariableByKey` takes a category:

```go
// before
v, err := client.GetVariableByKey(ctx, workspaceID, "region")
// after
v, err := client.GetVariableByKey(ctx, workspaceID, "terraform", "region")
```

A key-only lookup could not stay: under the new identity it would have to choose
between two real variables, and it chose by list order. The provider, the
migration tool and the MCP server are updated; only your own code needs this.

**`terrapod_variable_delete` in the MCP server** now refuses a key that names more
than one variable, listing the categories, rather than deleting whichever it found
first. Pass `category` to choose. `terrapod_variable_set` upserts on
`(category, key)`, which also fixes a quieter bug: setting a key with
`category=env` on a workspace that already had that key as `terraform` used to
re-categorise the existing variable instead of creating a new one.

### One variable category for every engine's parameters

**Nothing you have breaks, and you need change nothing to upgrade.** If you set
`category = "terraform"` — in the provider, through `tfci`, in an `/api/v2`
request, or in the UI — it keeps working and keeps reading back as `terraform`.
That is the overwhelming majority of configurations and it is unaffected.

**What changed.** Each engine has exactly one channel for "the parameters the
platform supplies to this run": Terraform's input variables, Pulumi's stack
config, Ansible's extra vars. They are the same role and only the delivery
differs, so they are now one category rather than one per engine. It is stored as
`native`, and the runner decides the delivery from the workspace's engine.

**The name you see depends on the surface you ask.** The TFE-compatible surface
(`/api/tfe/v2` and its `/api/v2` alias) returns `terraform` and always will —
`tofu`, `terraform` and `tfci` hold that as a constant, so changing it would be a
compatibility break. Terrapod's own `/api/v1` returns `native`. On input,
`terraform`, `native` and `pulumi_config` are all accepted on either surface.
This is exactly the arrangement `structured` already has with `hcl`.

**The variable routes are now served on `/api/v1` as well**, which is new and
purely additive. Nothing moved; `/api/v2` serves them as it always did.

**`pulumi_config` is gone as a stored category.** It existed only in
pre-release builds of 2.0 and never shipped. The migration folds every such
variable into `native`, and the name is still accepted on input, so a script or
`terrapod_variable` resource written against it keeps applying. The provider
treats `terraform`, `native` and `pulumi_config` as one value and keeps whichever
you wrote, so none of them drifts — you need change nothing, though `terraform`
is the clearest thing to settle on.

**If a workspace held both**, the migration keeps the **oldest** row per key and
deletes the rest, printing each removal — two rows that were `terraform:region`
and `pulumi_config:region` become one `native:region`. This can only affect a
deployment that ran a pre-release build.

**Two read-only signals are removed**, and their absence is the point rather than
a loss: a variable's `applies-to-engine` attribute, and the workspace's
`variables_not_consumed` health condition. Both reported that a variable sat in a
category its engine would never read. With one category that state cannot arise.
Neither was ever in a release. If you match on health-condition codes, drop
`variables_not_consumed` from the list.

**If you use `go-terrapod` directly**, `Variable.AppliesToEngine` is removed for
the same reason. Compare categories with the new `SameCategory` rather than `==`:
`GetVariableByKey` already does, so it finds the same row whichever name you pass,
but your own comparisons are right only until something reads from the other
prefix.

### The five SAML assertion checks are strict by default

**Affects:** every deployment using SAML. **A misconfigured provider stops being
able to log in**, so work through the table below before you upgrade.

Five per-provider SAML checks arrived with the 1.7.7 and 1.8.2 security releases
(`GHSA-hgx9-xwfp-5qcr`). On those lines all five default to `false`, because a
patch release must not change what a running deployment does — a destination check
that starts rejecting assertions mid-week is an outage, not a fix. **From 2.0 all
five default to `true`.** The implementation is unchanged; only the default moved,
so a provider that already sets these explicitly is unaffected.

| Setting | What it starts requiring | Check this first | If your IDP cannot do it |
|---|---|---|---|
| `validate_destination` | the assertion's `Destination` and `Recipient` name **this** deployment's ACS URL | the ACS URL Terrapod builds is the one you registered with the IDP — the provider's own `acs_url`, else `auth.callback_base_url`, else `external_url`, plus the SAML ACS path | `validate_destination: false` |
| `validate_in_response_to` | the assertion answers the `AuthnRequest` this login sent, and carries an `InResponseTo` at all | your IDP echoes `InResponseTo` on the `Response` element, and you do not rely on IDP-initiated sign-on | `validate_in_response_to: false` |
| `reject_replayed_assertions` | each assertion id is used once, remembered in Redis for the rest of its validity window | Redis is reachable from every API replica (it already must be — sessions live there) | `reject_replayed_assertions: false` |
| `want_assertions_signed` | the signature is on the assertion itself, not only on the enclosing message | your IDP signs the assertion; Azure AD, Okta and Auth0 all do by default | `want_assertions_signed: false` |
| `reject_deprecated_algorithm` | no SHA-1 signature or digest (`RSA-SHA1`, `DSA-SHA1`, `SHA1`) | your IDP signs with SHA-256 | `reject_deprecated_algorithm: false` |

**The ACS URL is the one to get right**, because `validate_destination` is the
check with a configuration prerequisite rather than an IDP prerequisite. Set
`auth.sso.saml[].acs_url` when a proxy rewrites the path between your IDP and
Terrapod; otherwise `auth.callback_base_url` has to be this deployment's
externally-reachable URL. A SAML provider with no absolute ACS URL is **refused**
rather than waved through, and the refusal names the keys to set.

The quickest way to upgrade without surprises is to set all five to `true` on your
current 1.x release, confirm a login still works, and then upgrade — at which point
the defaults and your config agree. Relax one at a time if a login fails; each
failure message names the check that refused it (see
[`docs/authentication.md`](authentication.md#assertion-validation)).

### A web session ends when the roles behind it change, and has a hard ceiling

**Affects:** anyone signing in to the web UI. **Nothing to configure**, but your
users will notice being signed out in cases where they previously were not.

A session carried the roles resolved at sign-in and nothing pushed a change to it,
so a demoted user kept their old roles — `admin` included — for the rest of the
session (`GHSA-pwrq-j4cv-w7qg`). Two things change.

**A role change now reaches live sessions.** A reduction signs the user out; a
widening adds the role in place and signs nobody out. The full table is in
[`docs/authentication.md`](authentication.md#a-sessions-roles-and-when-they-change-under-it);
the cases that will be new to your users are:

* removing a role assignment, or a PUT that drops one, signs that user out;
* narrowing a custom role's grant — removing a capability, turning `allow-all`
  off, editing its scope rules — signs out **everyone holding that role**;
* deleting a custom role signs out everyone who held it;
* an admin password reset signs that user out everywhere. Their API tokens are
  left alone, because a routine rotation that broke someone's automation would be
  a worse trap than the one being closed; *deactivate* is what revokes everything.

Editing only a role's description, or adding a capability to it, signs nobody out.

**And a session has an absolute ceiling.** `auth.session_absolute_ttl_hours`
defaults to **24**, measured from sign-in, and the 12-hour sliding window is
clamped to it — so a session a polling browser keeps warm now ends after a day
instead of living indefinitely. Raise it if that is too short for you, or set it
to `0` to restore the old unbounded behaviour (not recommended: it is what bounds
how long anything resolved at sign-in can outlive a change to it).

```yaml
api:
  config:
    auth:
      session_absolute_ttl_hours: 24   # or 0 for the pre-2.0 behaviour
```

Sessions open across the upgrade are capped from their own sign-in time, so some
of them will end shortly after you upgrade rather than at the 24-hour mark.

### An IdP group can no longer grant `admin` or `audit`

Role resolution took IdP group names verbatim, so a group called `admin` granted
platform admin (`GHSA-22vg-4g2w-7w34`). It no longer does, from either OIDC or
SAML, however the group is named or prefixed.

**Check this before upgrading if your admins get their role from a group.** Run

```sql
SELECT provider_name, email, role_name FROM platform_role_assignments;
```

and if that returns nothing while your administrators currently sign in through an
IdP group, they will lose `admin` at the upgrade. Grant it deliberately first,
either with a platform role assignment or with a `claims_to_roles` rule on the
provider entry you already have:

```yaml
api:
  config:
    auth:
      sso:
        oidc:
          - name: okta
            issuer_url: https://okta.example.com/
            client_id: "..."
            claims_to_roles:
              - claim: groups
                value: "platform-engineering"   # the group, named as the IdP names it
                roles: ["admin"]
```

A rule is written by whoever administers Terrapod; a group name is written by
whoever administers the directory. That difference is the whole point, and it is
why a rule naming the same group is accepted while the bare group is not.

The local admin account is unaffected, so a deployment is not lockable out of
itself — but recovering that way is worse than spending a minute on the query above.

**`role_prefixes` is also a filter now,** where it used to strip a matching prefix
and pass everything else through. If you set it, a group without one of those
prefixes is ignored rather than taken as a role name. That is what the setting
always read as, and it narrows rather than widens — but if you relied on the
pass-through, those roles stop arriving.

### Listeners must prove they hold their certificate's private key

**Affects:** any pool still running a listener image older than 2.0.

A listener authenticates with `X-Terrapod-Client-Cert`, the certificate the CA
issued it at join. A certificate is **public material** and it travels on every
request, so until 2.0 that header was effectively a bearer token: anyone who
observed one call could replay it until the certificate expired, and every check
the API made — CA signature, expiry, name lookup, fingerprint — was satisfied by
the copy just as well as by the holder.

From 2.0 each request is also signed with the private key the CA returns once at
join, binding it to one method, one path and a single-use nonce inside a 60-second
window. Both listener authentication paths enforce it, including the SSE event
stream.

**A listener image older than 2.0 does not sign, so it gets `401` on every
call — the SSE event stream included.** It therefore never connects, never
claims a run, and the pool dispatches nothing. That is the failure to expect:
the pool goes quiet, not noisy.

It also does not fail cleanly, and the reason is in the 1.x code rather than in
the API: a pre-2.0 listener could not tell a *rejected* certificate from a
*transient* error, because `_renew` returned the same `None` for both. So a
rejection retried for ever instead of surfacing. 2.0 splits them
(`_CertRejected` against `_JoinTransient`) and that is what makes
`on_cert_rejected` possible — but the listener holding the old code gets none of
it.

(This paragraph previously said `renew` "is not retried and falls back to the
join token", and that the listener "re-registers under a fresh name on every
renewal cycle and churns pool membership". Both halves were wrong, and wrong in
the direction that sends an operator hunting churn when the real symptom is
silence. `_CertRejected`'s own docstring records why: *"the documented fallback
never happened and a rejected cert retried for ever."*)

So either upgrade every listener in every pool before the API, or set:

```yaml
api:
  config:
    agent_pools:
      require_listener_proof_of_possession: false
```

and remove it once the fleet is upgraded. The setting honours an explicit
`false` — it is rendered with `hasKey`, not `| default`.

**Clock skew matters now.** The signature carries a timestamp and is rejected
more than 60 seconds either side of the API's clock, so a listener cluster whose
clock has drifted further than that fails to authenticate. That is a real failure
mode on long-running VMs and in nested virtualisation.

### An OIDC login needs an email the provider vouches for

The email claim is the principal everywhere in Terrapod: it selects role
assignments, owns workspaces and names token owners. Before 2.0 the OIDC connector
took it straight from the merged claims and checked nothing, so a provider that
lets a user set their own address could hand an attacker a victim's identity.

From 2.0 a login is refused when the claims carry no `sub`, carry no `email`, or
report `email_verified` as false. **A login whose claims simply omit
`email_verified` is also refused**, because an address nobody vouches for is one
the person logging in may have chosen.

That last case is the one that can stop a working deployment logging in: some
providers verify email without sending the claim. If yours is one of them, set it
per provider:

```yaml
api:
  config:
    auth:
      sso:
        oidc:
          - name: my-idp
            issuer_url: https://idp.example.com/
            client_id: "..."
            require_email_verified: false
```

An explicit `email_verified: false` is still refused with that set — the relaxation
covers a missing claim, not a provider telling us the address is unverified. The
key honours an explicit `false` (it is rendered with `hasKey`, not `| default`).

### An API token's roles are resolved against the provider it was minted under

Role assignments have always been keyed by provider *and* email. Token role
resolution only ever matched on email, so a token inherited every role assigned to
that address under **any** configured provider — up to platform admin. With more
than one login source, an account at the weakest one was enough.

From 2.0 a token records the provider of the identity it is bound to, and
resolution joins on it. The same applies to the container registry, the package
proxies and Slack-initiated actions, which resolved roles the same way.

**Tokens minted before the upgrade have no recorded provider, and resolve to no
roles at all.** They are not silently given every provider's roles, because
guessing is what the original defect did. The migration attributes what it can
prove — a token whose owner has a local account with a password is marked `local` —
and leaves the rest unattributed. Anything unattributed logs a warning naming the
token id when it is used, so:

```sql
SELECT id, bound_to, description FROM api_tokens WHERE identity_provider IS NULL;
```

Re-mint those, or expect 403s from automation that used them. A `terraform login`
token is re-minted by logging in again.

**`require_external_sso_for_roles` now applies to tokens too.** It was enforced
only at login, on the session's roles, so a token minted by a local account carried
the restricted roles regardless. A local-provider token now has them dropped from
its resolved set — the token keeps working for everything else rather than being
refused outright, since automation cannot be prompted to go and log in via SSO.

### A role assignment can be pinned to an IdP subject

Role assignments are keyed by provider and email. Email is the weaker half of an
identity: a provider that lets a user change their address, or an operator recycling
one, moves the grant to a different person.

An assignment may now carry a `subject` — the IdP's `sub` claim. Unpinned is the
default and behaves exactly as before, matching on provider and email; every existing
assignment is unpinned, so **nothing changes on upgrade**. Pinned, it matches only
that subject, and a credential that cannot prove its subject matches unpinned
assignments only.

Pin the grants that matter most, which in practice means `admin` and `audit`:

```hcl
resource "terrapod_role_assignment" "platform_admin" {
  provider_name = "okta"
  email         = "admin@example.com"
  role_name     = "admin"
  subject       = "00u1a2b3c4d5e6f7g8h9"  # the IdP's `sub` for this person
}
```

`subject` is Optional+Computed, and that matters when an identity has several
`terrapod_role_assignment` resources: the pin belongs to the identity, not to one
grant, so an instance that does not name it keeps whatever is already pinned rather
than clearing it. Set it on one of them, or on all of them to the same value.

The UI shows a pin read-only. A `sub` is an opaque provider-issued string, so it is
set as code or through the API rather than typed into a form.

### Terrapod owns the token signing key, and a used deployment adopts its old one

**Affects:** every deployment. Most need to do nothing before upgrading.

One key signs runner tokens, run-task callback tokens, download tickets and
Slack link tokens. It used to come from the Helm chart, and failing that was
derived from `sha256(database_url)` — which hands token forgery to anyone
holding database credentials (GHSA-hc47-q72v-4vcm). Terrapod now generates it
and stores it in the database, read on every startup, the same way it has always
owned the listener certificate authority.

**What happens on upgrade, with nothing configured.** A deployment that has
executed runs **adopts** its existing `sha256(database_url)` key rather than
minting a new one, so tokens already in flight keep verifying — a key change
mid-apply means an apply that succeeds and then cannot upload its state, which
leaves the workspace flagged as diverged. The adopted key is still derivable
from the database URL, so **startup warns until you replace it**. A deployment
that has never executed a run has nothing in flight and generates a strong key
immediately.

**What to do before upgrading:** nothing, unless you set
`api.config.require_strong_secrets`. That switch used to fire whenever no key
was *configured*; it now fires on an adopted key, which is the state an upgrade
of a used deployment lands in. So either supply a key at the same time as the
upgrade, or leave the switch off until you have. An unset `api.tokenSigningKey`
is the normal, strong case from here on and the switch does not object to it.

**If you already supply `api.tokenSigningKey.existingSecret`, nothing changes.**
A supplied key still wins, on every startup, and Terrapod does not read its
stored key at all — so rotating your own Secret still takes effect. Your key is
deliberately not copied into Terrapod's table: that would make the first value
you ever supplied win for ever and silently ignore every later change.

**The generated Secret is gone.** The chart no longer renders
`<release>-token-signing`, because a random value in a rendered manifest is not
a pure function of its inputs — under `helm template`, which is what Argo CD and
Flux run, the generating branch minted a fresh key on every render. If you
deploy through a GitOps controller, the whole class of problem goes with it:
there is nothing to re-mint and nothing for a pruning controller to delete.
The existing Secret carries `helm.sh/resource-policy: keep`, so Helm leaves it
behind rather than deleting it. It is inert once you are on 2.0 — **but delete
it only after confirming you do not reference it** from
`api.tokenSigningKey.existingSecret`, because if you do, it is your key and
still live.

`api.tokenSigningKey.existingSecret` and `existingSecretKey` are unchanged, so
no values edit is required.

### The engine on/off switch is removed

**Affects:** a `values.yaml` written against a pre-release build of 2.0. **The
switch never shipped in a 1.x release** — no 1.x chart had an `engines:` block to
set — so an upgrade from 1.x has nothing to remove here. It is recorded because the
block existed on `main` through 2.0 development, and because the table below is
what changes if you were relying on it.

`api.config.engines.ansible.enabled` and `api.config.engines.pulumi.enabled` are
gone, along with the `engines:` block that held them. Because
`values.schema.json` refuses keys it does not declare, a `values.yaml` that still
sets them **fails `helm lint` and `helm upgrade`** rather than ignoring them —
which is the useful direction: it tells you at upgrade time instead of leaving
dead configuration behind.

```yaml
# remove this block entirely
api:
  config:
    engines:
      pulumi:
        enabled: true
      ansible:
        enabled: true
```

**There is nothing to replace it with, and nothing to decide.** The platform
offers every engine it can run; a deployment that uses only Terraform or OpenTofu
simply never names another one. Switching an engine off never deployed less — it
left routers unmounted and filtered a list — and an engine's CLI is still fetched
only when a run of that engine happens, so a Terraform-only deployment is
unaffected either way.

**What this does change,** if you were relying on the switch:

| was | now |
|---|---|
| `GET /api/v1/engines` omitted a disabled engine | lists every engine the build contains |
| creating a workspace with a disabled engine was refused | accepted; the engine must simply be one Terrapod can run |
| the Pulumi service surface was unmounted | always mounted (nothing there is reachable without a Pulumi workspace to name) |

**Per-capability switches are unaffected and are what to use instead.** If your
reason for switching an engine off was to stop a *surface* serving, that control
survives and is unchanged — `api.config.registry.oci.enabled` for the container
registry, and `api.config.registry.package_cache.enabled` plus its six
per-ecosystem flags (`pypi`, `npm`, `galaxy`, `pulumi`, `go`, `nuget`) for the
package proxies. Each of those is read, and switching one off unmounts its
routes rather than leaving them answering 404.

### `api.config.default_ansible_version` is removed

**Affects:** a `values.yaml` written against a pre-release build of 2.0. **The key
never shipped in a 1.x release** — no 1.x chart carried any ansible setting at
all — so an upgrade from 1.x has nothing to remove. It is recorded for anyone who
set it on a pre-release build, and because the two questions it used to answer now
have separate answers worth knowing.

```yaml
# remove this line entirely
api:
  config:
    default_ansible_version: "2.21.5"
```

Because `values.schema.json` refuses keys it does not declare, a `values.yaml`
that still sets it **fails `helm lint` and `helm upgrade`** rather than ignoring
it — which is the useful direction, since the setting no longer corresponds to
anything.

**Nothing replaces it, because it was answering two different questions at
once.** They now have separate, better answers:

| question | before | now |
|---|---|---|
| which ansible does the **API** use to read an inventory? | the workspace's pinned version, installed into a virtualenv on first use via `pip install ansible-core==…` through the PyPI pull-through cache, with this setting as the fallback | one `ansible-core` pinned in the API image at build time. Not configurable, because parsing a static inventory file is one stable operation done once, server-side |
| which ansible runs a workspace's **playbooks**? | the same value, conflated with the above | the workspace's `ansible-version`, naming an ansible-core **line**, which selects its execution-environment image — where the line genuinely matters, because it decides which target Pythons a playbook can reach |

**Your workspaces keep the versions they carry.** `ansible-version` is still a
per-workspace attribute, still settable on create, PATCH, bulk update and the
autodiscovery rule template, and nothing rewrites existing values. What changed
is what the value selects. New workspaces that name none take the platform's
current line rather than a value from your `values.yaml`.

**If you pinned a patch** (`2.21.5`), that is accepted and still means its line —
there is one execution environment per line, so the patch component names no
image of its own. The default for a new workspace is now written as a line
(`2.21`), which keeps a workspace on that line's newest patch as the image is
rebuilt for upstream fixes.

**The PyPI pull-through cache is unaffected** and still serves Pulumi's language
SDKs and a collection's own Python dependencies. What was removed is the
ansible-specific layer on top of it: the API no longer fetches an ansible at run
time, so an inventory read can no longer fail because an index was unreachable.

### An execution environment must run as uid 1000

**Who this affects: deployments that set `api.config.ansible.execution_environments`** — the
label→URI map naming your own EE images. Terrapod's own published EE images already run as
uid 1000 and need nothing.

1.x asked an EE for "a uid able to read the shared volume", and satisfied it with an
`fsGroup`: the sidecar's `runAsUser` was deliberately left unset so an image running as any
non-root user worked. 2.0 pins `runAsUser`/`runAsGroup` to **1000** on the EE container.

The reason is the per-host capture (#1928). The orchestrator and the EE now exchange two
files on the shared volume — the callback plugin one way, its event stream the other — and
with the uids guaranteed equal those are `0600` rather than group-readable. The shared
volume is a pod-scoped `emptyDir`, so group-readable was never reachable from outside the
pod; this narrows the grant to what the work needs rather than closing an exposure.

**What to check.** An image whose `USER` is a different uid is still *started* as 1000
(Kubernetes applies `runAsUser` regardless of the image's own `USER`), so the question is
whether it can *function* as 1000:

```sh
# As the uid Kubernetes will use, against your own image:
docker run --rm --user 1000:1000 <your-ee-image> sh -c '
  id -u && ansible --version >/dev/null && echo CONFORMS'
```

Two ways it can fail, both measured while building the EE images:

- **no passwd entry for 1000** — a task dies `KeyError: getpwuid(): uid not found`;
- **a HOME that uid cannot write** — `DEFAULT_LOCAL_TMP` resolves under `/` and ansible dies
  inside `import ansible.constants`.

Fix either by building the image with `USER 1000` and a passwd entry for it (what Terrapod's
own EE images do), or by adding one to the image you have.

**You will not meet this mid-run.** The EE agent asserts the uid at the sidecar's startup
probe, ahead of the binary checks, so a non-conforming image fails at pod start naming the
uid — rather than part way through a playbook on a file it cannot read, which is where the
older, looser contract would have sent you.

### A machine credential holds secrets only — four connection fields are gone

**Affects:** an `ansible_machine_auth` credential written against a pre-release
build of 2.0, and any inventory referring to one of the four fields below. **The
category never shipped in a 1.x release**, so an upgrade from 1.x has nothing to
do; this is for alpha and beta adopters, whose inventories may contain
`${terrapod:web-key.user}`.

`user`, `become`, `become_method` and `become_user` are no longer fields of the
category. What remains is `private_key`, `known_hosts`, `password` and
`become_password` — secrets, plus the one file that is not a secret because a
host's public key is public.

**The rule the four failed is that a credential holds what must not reach the
resolved inventory.** None of them is secret and none is a file, so substituting
one put a plain value into the resolved inventory — the opposite of what the
category is for. The placeholder is pull-based, so an inventory author was
already writing `${terrapod:web-key.user}` by hand; for a non-secret that is
strictly worse than writing `ansible_user: deploy` in the same file, which has
identical visibility, one less indirection and one home for the value instead of
two. Become settings also differ legitimately per host and per group, while a
credential is referenced by name from many hosts.

`become_password` stays, because it is a secret and ansible has no
`ansible_become_password_file` — so it can only ever be a variable, which is
exactly why it needs the log redactor.

**Move each value into the inventory**, where it is visible in a diff and
settable per host or per group:

| was, in the credential | now, as an inventory variable |
|---|---|
| `user` | `ansible_user` |
| `become` | `ansible_become` |
| `become_method` | `ansible_become_method` |
| `become_user` | `ansible_become_user` |

```yaml
# before -- the credential carried the connection settings, and the inventory
# pulled them back out one placeholder at a time
all:
  hosts:
    web1:
      ansible_user: "${terrapod:web-key.user}"
      ansible_become: "${terrapod:web-key.become}"
      ansible_ssh_private_key_file: "${terrapod:web-key.private_key}"

# after -- the non-secrets are written where they are read, and the credential
# is referenced only for the secret it holds
all:
  hosts:
    web1:
      ansible_user: deploy
      ansible_become: true
      ansible_ssh_private_key_file: "${terrapod:web-key.private_key}"
```

**Nothing stops working while you do this, and nothing is silently dropped.** The
two halves are deliberately asymmetric:

| | |
|---|---|
| a stored credential carrying a retired field | the retired names are stripped on read and the **secrets are still delivered**, so the credential goes on working. No migration, and no action needed until you edit it |
| editing that credential in the UI | the form writes the narrowed envelope back, so any edit at all cleans the row up. You do not have to find the retired fields — the inputs are already gone |
| writing one through the API with a retired field | `422`, naming the field and listing the ones the category carries |
| an inventory still referring to `${terrapod:web-key.user}` | the configure is **refused before anything runs**, naming every reference it could not resolve |

That last row is the one to plan around: it is a loud, actionable failure rather
than a silent one, but it is still a failure, so an inventory referring to a
retired field needs the edit above before its next configure.

There was a window in which this was not true. On `v2.0.0-alpha.1` a stored
credential carrying a retired field was refused on read and then **dropped
without an error or a log line** — the runner reported that no credentials had
been declared, and every host came back unreachable naming neither the credential
nor the field. If you are upgrading from alpha.1 and saw that, the credential
itself was never damaged: it delivers again on this release with nothing to
repair.

### An explicit `= ""` clears an optional string, and removing the line no longer does

**Affects:** Terraform configurations that clear an optional string attribute by
deleting its line, on five attributes; and any Go program importing
`go-terrapod` that builds an `UpdateWorkspaceRequest`.

Five attributes become `Optional + Computed`:

| resource | attribute |
|---|---|
| `terrapod_workspace` | `vcs_repo_url`, `vcs_branch`, `vcs_connection_id` |
| `terrapod_agent_pool_token` | `description` |
| `terrapod_gpg_key` | `source_url` |

**What changes is what *removing* the line means.** Before, deleting the
attribute from your configuration asked for a clear; now the diff is suppressed
and the stored value is kept, and you clear by setting it explicitly:

```hcl
resource "terrapod_workspace" "example" {
  name = "example"

  # Clears the binding. Deleting these lines no longer does.
  vcs_repo_url      = ""
  vcs_branch        = ""
  vcs_connection_id = ""
}
```

**It replaces a broken removal with a defined one rather than taking away a
working affordance** — which is the whole reason the trade is worth making.
Deleting those lines never cleared the binding: the SDK's update struct carried
them as plain `string` with `,omitempty`, so an empty value was dropped before
serialisation and the server's `PATCH` then merged, leaving the stored value
exactly as it was. The configuration said one thing, the deployment did another,
and the plan showed the difference again on every run. So the behaviour you had
was a perpetual diff, not a clear.

**And the explicit form did not work either, which is the defect this fixes.**
An `Optional + Computed` attribute plans an explicit `""` as `""`, so the
framework then requires `""` back from the apply — and both legs dropped it: the
write leg through `,omitempty`, and the read leg by mapping the server's `""` to
null. Either one alone produced

```
Error: Provider produced inconsistent result after apply
```

on the one configuration that asked for the value to be cleared. The two legs
are fixed together, because a fix to either on its own still fails.

#### The Go SDK change, for anyone importing `go-terrapod`

Four fields on `UpdateWorkspaceRequest` become `*string`:

```go
// before
req := terrapod.UpdateWorkspaceRequest{VCSBranch: "main"}

// now
branch := "main"
req := terrapod.UpdateWorkspaceRequest{VCSBranch: &branch}
```

They are `AnsibleExecutionEnvironment`, `VCSRepoURL`, `VCSBranch` and
`VCSConnectionID`. This is a **compile break**, deliberately: a pointer is the
only way the struct can distinguish "leave this alone" (`nil`) from "set it to
empty" (a pointer to `""`), and that distinction is what a partial update needs.
`CreateWorkspaceRequest` is untouched — a create has no stored value to leave
alone, so an absent field there takes the column default and a plain `string`
says everything it needs to.

`vcs-connection-id` now also travels as an attribute alongside its JSON:API
relationship, because a relationship cannot express a clear; the server reads
either, and an empty attribute is what removes the binding.

**What does not break.** `terrapod_workspace.ansible_execution_environment`
keeps its `Optional`-only schema, so removing *that* line still clears it — the
server has no value of its own to supply for an unset label, and making it
`Computed` would write `""` into state against a configuration that never set it
and diff for ever. Its explicit `= ""` round-trips too, decided from what the
configuration asked for as well as what the server answered. No other attribute
changes shape, no state migration is needed, and a stored value is never
rewritten by the upgrade itself.

### Downloading a protected configuration-version archive needs module `registry:read` too

**Affects:** anyone reading configuration-version archives through the API on a
workspace provisioned from a **self-contained** catalog item. If you have no
self-contained items, nothing changes.

Until 2.0 a configuration-version archive was governed by workspace
`config:read` alone. A self-contained catalog item publishes a registry
module's own archive as the root configuration, so that archive carries
registry bytes — and reading it through the workspace door was a way to obtain
module content the caller might have no registry grant for.

So a **protected** archive now requires both workspace `config:read` **and**
module `registry:read`, and that applies to the download, to a mint of a
download ticket, and to each side of a diff. A caller holding only one of the
two gets 403 where 2.0 previously returned bytes.

Two consequences worth planning for rather than meeting:

- **Historic archives are fail-closed.** A configuration version written before
  this release has no recorded provenance, and the server will not guess. Such
  a version is denied unless it is plainly an ordinary one — no module id, not
  a catalog workspace, and produced by one of the familiar sources (`tfe-api`,
  `cli`, `vcs`, `drift-detection`) — in which case it reads as unprotected and
  behaves as before. Anything else, including an archive whose module has since
  been deleted, is denied.
- **A download ticket authorised at mint stays valid for up to 30 minutes**
  even if the grant behind it is revoked in the meantime, and a ticket minted
  for an ordinary configuration version cannot open a protected archive.

**What to do:** give anyone who downloads archives from catalog-provisioned
workspaces `registry:read` on the modules behind those items. During a mixed
version rollout an older replica keeps serving the old behaviour, so the
tightening appears gradually rather than at one instant.

### Reading a catalog item no longer returns its authored defaults and choices

**Affects:** anything reading `/api/v1/catalog-items` or
`/api/v1/catalog-items/{id}` — a third-party script, an agent, or a provider
binary built before the change. Provisioning and reconfiguring are unaffected.

A catalog item's `variable-options` entries are **projected to six keys** on an
ordinary read — `name`, `hidden`, `sensitive`, `description`, `type`, `required`
— so an entry's authored `default` and its `options` list are no longer in the
document. A provider template's `body` comes back `null` by the same rule.

The reason is the same one that moved a catalog instance's variable values (the
entry above): an item exists to narrow a module, and part of narrowing it is
fixing inputs the provisioner must not choose or must not see. An authored
default and a choice list are that authoring, and returning them to every reader
made the `hidden` and `sensitive` overlays advisory. Unknown metadata is dropped
rather than filtered, because a nested default or choice list inside a key
nobody has enumerated is still the authoring.

**This is the kind of change least likely to be noticed before it bites**, and
the reason is worth stating plainly: the route, the attribute names and the
status code are all unchanged. Only the content moves, so **no contract snapshot
and no schema check reports it**. A consumer that reads `variable-options[].default`
and writes it somewhere will start writing nothing at all.

**To recover them**, ask explicitly and hold platform `admin`:

```
GET /api/v1/catalog-items/{id}?include-sensitive-values=true
```

A caller without platform `admin` asking for them gets **403**, not a quietly
reduced document — the refusal is the answer, because a silent projection is
what this entry is about.

**What to do.** If you are choosing a value for an input, the
**interface** surface is the one to read: `GET /api/v1/catalog-items/{id}/interface`
answers "what may I supply here" for a chosen version, which is the question a
default and a choice list were being used to answer indirectly. The Terraform
provider, the web UI, go-terrapod and the MCP tools were all moved to send or
document the opt-in, so an up-to-date consumer needs no change; a script of your
own that was reading the plain document does.

### A human read of a catalog instance's variables no longer returns their values

**Affects:** anything reading `/variables` on a workspace provisioned from a
catalog item — a person in the UI, a script, an agent. Runs are unaffected.

A catalog item exists to narrow a module, and part of narrowing it is fixing
inputs the provisioner must not choose, or must not see. Until 2.0 those
values were readable straight back off the workspace's variables, which made
the item's `hidden` and `sensitive` overlays advisory rather than real.

A static native (or `terraform`-category) variable on a catalog-managed
workspace is now returned with `value: null`, `sensitive: true` and
`version-id: null` when the item marked it sensitive, when the item hid it, or
when **the item's definition can no longer be tied to this instance** — the
last being deliberately fail-closed, because guessing which inputs were hidden
at provision time is exactly the guess that would leak one.

**This is the change least likely to be noticed before it bites**, and the
reason is worth stating: the route, the attribute names and the status code are
all unchanged. Only the content moves, so no contract snapshot and no schema
check reports it. A consumer that reads a value and writes it somewhere will
start writing `null`.

**What to do:** read catalog inputs from the catalog surface rather than the
workspace's variables — `terrapod_catalog_instance_get` and the catalog API
return the non-sensitive subset explicitly, which is an honest answer rather
than a masked one. A reconfigure that omits `input_values` keeps the stored
set, so a round trip through the catalog surface is lossless even for the
values it does not show you.

### Listing variable sets is filtered by workspace reach

**Affects:** any **non-admin** principal that lists variable sets, reads one by
id, or lists a set's variables -- a person in the UI, a script, an agent, or a
`terraform plan` refreshing a `terrapod_variable_set` resource under a non-admin
token. Platform `admin` and `audit` are unaffected and still see everything, and
every *write* was already platform `admin`, so nothing a non-admin could change
has moved.

Until 2.0 these three reads authenticated and then authorized nothing. Any
principal who could log in could enumerate every variable set in the deployment,
and through `GET varsets/{id}/relationships/vars` every variable `key` and
`sensitive` flag in each. Values were masked; the inventory was not -- and for a
set carrying a credential, the inventory is most of what you would want to know
before going looking for it.

A set is now visible when it **reaches at least one workspace you hold
`workspace:read` on**. Reach covers an explicit assignment and an
`assignment-rule` match, computed by the same function the injector uses, so the
filter cannot disagree with the matcher about who receives a credential. A
**global** set reaches the whole estate and stays visible to everyone.

Two consequences worth knowing before you upgrade:

- **A set assigned to nothing is visible only to `admin` and `audit`.** That is
  the honest reading of the rule rather than an oversight: it reaches no
  workspace, so no workspace grant can reach it.
- **`GET varsets/{id}` and `GET varsets/{id}/relationships/vars` answer `404`,
  not `403`,** on a set you cannot see. A distinguishable `403` would let ids be
  walked to learn which sets exist, which a filter on the collection alone cannot
  close.

`meta.pagination.total-count` on the list is now the count of what the caller can
see, because the filter runs before the page is sliced.

**What to do:** nothing, if your variable sets are assigned to workspaces or are
global — which is the normal arrangement, and those stay visible to everyone who
can read a workspace they feed. If you rely on a non-admin seeing a set that is
assigned to nothing, assign it, make it global, or give that principal `audit`.
The admin variable-sets page in the UI is already admin-gated, so it is
unaffected.

### A run's status on `/api/tfe/v2` is always one `go-tfe` defines

**Affects:** anything branching on `status` from the **TFE-compatible** surface —
`go-tfe`-based automation, `tfci`, a script reading `/api/tfe/v2/runs/{id}`. The
native `/api/v1` surface is **unchanged** and still reports Terrapod's own
vocabulary verbatim.

Two statuses had been going out on that surface since the beginning and are not
`tfe.RunStatus` values at all, so a client's `switch r.Status` fell through every
case:

| Terrapod status | reported on `/api/tfe/v2` before | and now |
|---|---|---|
| `queued` | `queued` (not a TFE status) | `plan_queued`, or `apply_queued` for a standalone configure |
| `canceling` | `canceling` (not a TFE status) | `applying` |

`canceling` folds to `applying` rather than to the terminal `canceled`
deliberately. A client that reads `canceled` concludes the run is finished while
its Job is still winding down and its state upload has not happened;
`VALID_TRANSITIONS` reaches `canceling` only from `confirmed`, `applying` and
`configuring` — never from `planning` — so a canceling run is always apply-side,
and `applying` is both parseable and true until it reaches `canceled` for real.

`queued` folds by phase because Terrapod's `queued` means "waiting for a runner
to claim it", and the dispatcher claims an ordinary run as a plan and a
standalone configure as a configure. That is the same reading that already folds
`configuring` to `applying`.

**It reaches go-terrapod, and through it the provider, MCP and
terrapod-migrate**, because `go-terrapod` reads a run from `/api/v2/runs/{id}` —
the TFE alias — rather than from the native surface. That is pre-existing and
not new with this change: it has always received the folded vocabulary, which is
why a `configuring` run has always arrived there as `applying`. `Run.Status` is
a bare `string` with no constant set, so nothing in the SDK needed changing, and
nothing in the provider or MCP compares the literal. A third party importing
go-terrapod and matching `"queued"` will need to match `"plan_queued"`.

**What to do:** nothing, if you read the native surface or use the `terraform` /
`tofu` CLI — the CLI could not interpret the old values either, so this can only
improve what it does. If you have automation that matches the literal strings
`queued` or `canceling` **against `/api/tfe/v2`**, point it at `/api/v1`, which
reports the stored status and always did, or match the new values. A fold is now
enforced in both directions by `test_run_status_contract.py`: every status the
TFE surface reports must be one `go-tfe` defines, and the native surface must
report the stored status verbatim.

### The UI's default theme is light, and dark is a per-user toggle

The web UI defaulted to dark through 1.x and **defaults to light from 2.0**
(#2337). The dark theme is kept, not removed: each user can switch to it with
**Account → Dark theme** (the account drawer on a phone). The choice is
stored in a `terrapod-theme` cookie in that browser and rendered on the server,
so it loads without a flash of the light theme.

There is still **no Helm value or config key that sets the default** for
everyone, and the UI does not follow the browser's `prefers-color-scheme`. A
deployment that wants dark for every user by default changes `DEFAULT_THEME` to
`'dark'` in `web/src/lib/theme.ts`; a user's own choice still wins over it.

Dark is restored exactly rather than approximately, which is a property of how
the themes are built rather than a promise: the dark values are Tailwind's own
plus the `brand` ramp and are declared nowhere, and light is a single
`:root:not(.dark)` override that wins on specificity. So `class="dark"` takes
the override out of play and leaves the stock values — dark has nothing to
drift from.

Listed here because no gate, snapshot or test will tell you about it: nothing
fails, the UI simply looks different until each user flips the toggle.
