# Deprecations

This page is the authoritative list of **deprecated** Terrapod surfaces — parts of
the public API, wire protocol, configuration, or Helm chart that are scheduled for
removal in a future major release. It is the human-readable companion to the
machine-readable `Deprecation` / `Sunset` headers the API emits (see below), and it
is governed by the compatibility guarantees in
[versioning-and-support.md](versioning-and-support.md).

## The promise

Terrapod does **not** remove or rename anything on a public surface without notice.
Every removal goes through a deprecation window:

1. The thing is **marked deprecated** — it keeps working exactly as before, but the
   API advertises a sunset date (and, for endpoints, emits deprecation headers).
2. The deprecation is **announced** here and in the release notes, with a
   replacement and a migration note.
3. It stays working for **at least two minor releases** (and never disappears in a
   MINOR or PATCH).
4. It is **removed only in the next MAJOR**, on or after the published sunset date.

If you keep your consumers (runner/listener images, `go-terrapod`,
`terraform-provider-terrapod`, your `values.yaml`) reasonably current — within the
[supported skew window](versioning-and-support.md) — a deprecation will always reach
you as a warning before it can reach you as a break.

## How to read the API's deprecation signal

A deprecated HTTP endpoint returns its normal body and status, plus these response
headers (per the IETF Deprecation draft and [RFC 8594](https://www.rfc-editor.org/rfc/rfc8594)):

| Header | Example | Meaning |
|---|---|---|
| `Deprecation` | `true` | This endpoint is deprecated. |
| `Sunset` | `Wed, 30 Jun 2027 00:00:00 GMT` | The date on/after which it may stop working (removed in a MAJOR). |
| `Link` | `<https://…/docs/deprecations.md>; rel="deprecation"; type="text/html"` | Where to read what to use instead. |

Automated clients should surface a `Deprecation: true` response as a warning in
their logs and plan a migration before the `Sunset` date. Nothing breaks at the
moment the header appears — it is advance notice.

## Removed without a deprecation window (v1.9.0)

One surface was removed outright rather than deprecated, under the security
exception in [Versioning & Support](versioning-and-support.md). It is listed here
because the policy otherwise says a surface never disappears in a MINOR, and a
reader checking whether that held needs to find the answer rather than infer it.

| Surface | Removed in | Why there was no window | What to use instead |
|---|---|---|---|
| The `terrapod merge` pull-request comment command | v1.9.0 | Commenting was not authorization: on earlier releases anyone who could comment on a pull request could merge it through Terrapod. A deprecation window means shipping that for two more minors. ([`GHSA-x4jp-5g4j-f8rr`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-x4jp-5g4j-f8rr), critical) | Merge on the provider. Terrapod's only self-initiated merge is workspace-configured auto-merge after a successful apply. The other comment commands (`plan`, `apply`, `unlock`, `help`) still exist and now require push access to the repository. |

This is a wider use of the exception than its own wording allows — it covers
tightening a permission, "never removing a route, an attribute, or a config key",
and a comment verb is a command surface. Named rather than quietly stretched.

## Active deprecations

**None.** No public Terrapod surface is currently deprecated.

When the first deprecation lands, it will be listed here in this shape:

<!--
| Surface | Deprecated in | Sunset (removed no earlier than) | Replacement | Notes |
|---|---|---|---|---|
| `GET /api/…/old-thing` | v1.3.0 | v2.0.0 / 2027-06-30 | `GET /api/…/new-thing` | Response shape is identical; only the path changed. |
-->

## Behaviour changes in v1.9.0

Not deprecations — nothing was removed and nothing is scheduled to be — but each
changes what an existing deployment does, which is the other thing a reader comes
to this file to check.

### Fork pull requests do not plan by default

`allow_fork_pr_plans` defaults `false` for workspaces and autodiscovery rules
created from v1.9.0. **Existing rows are not rewritten**, so a workspace created on
1.8 keeps whatever it had — the upgrade closes the default and leaves the audit to
you: `SELECT name FROM workspaces WHERE allow_fork_pr_plans = true;`
([`GHSA-gp5w-76rw-c452`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-gp5w-76rw-c452),
critical.)

### A non-admin cannot make a workspace join a secret-bearing variable set

Creating or editing a workspace so that it newly matches the assignment rule of a
variable set **carrying a sensitive or broker-resolved variable** is refused for
anyone who is not a platform admin. Sets of plain configuration still join
automatically, and shrinking the set that reaches a workspace is always allowed.
`drift_status` and `locked` are no longer usable as rule selectors at all, because
a workspace's own owner can move both; a rule naming one is rejected with `422` and
a stored rule naming one matches nothing.
([`GHSA-49q6-pm68-3xgw`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-49q6-pm68-3xgw).)

### Using a VCS connection requires a claim to it, and may be scoped to repositories

A workspace, a registry module or a minted git credential may only name a VCS
connection its caller has a claim to — platform admin, the connection's owner, a
role reaching its labels, or an existing workspace on it. A connection may also
carry `allowed_repositories`, which is **empty by default and means any
repository**, so nothing changes until you narrow one.
([`GHSA-v8g7-pqrj-8mcm`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-v8g7-pqrj-8mcm).)

### An explicitly configured Helm `false` now takes effect

`| default true` discarded it, so an opt-out has been silently inert. Two existing
values are affected — `notifications.smtp.use_tls` and `database.pool_pre_ping` —
and in both cases honouring the setting changes behaviour on upgrade. Check both
before upgrading; the release notes have the table.

### A mandatory AI policy gate holds a run whose plan it saw only in part

A plan over `ai_summary.plan_json_max_bytes` is reduced before the model sees it,
and a **mandatory** gate now records an un-ruled evaluation and holds the run
rather than ruling on the part it was shown. If you run a mandatory gate and large
plans, runs that previously passed will hold and need an admin override; raise the
cap, or set the gate to `advisory`. Advisory gates are unchanged.
([`GHSA-v677-9r29-3xq8`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-v677-9r29-3xq8).)

### A listener cannot re-join into a different pool

Listener names live in one global namespace, so a join under a name already
registered to a **different** pool returns `409` instead of moving the listener.
Re-joining the same pool and renaming are unaffected. What this stops is moving a
listener between pools by swapping the join token while keeping `listener.name`:
delete the old registration first, or give the listener a new name. A listener
image older than v1.9.0 has never seen a `409` on join and will most likely retry
rather than surface it.
([`GHSA-vr88-c3hx-xr4h`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-vr88-c3hx-xr4h).)

### Resource onboarding runs the engine with an allowlisted environment

`tofu init` and schema introspection no longer inherit the API's own environment —
which matters because the second of those makes the engine launch a provider plugin
as a child. Process basics, the engine's `TF_*` settings, both cases of the proxy
variables, the CA-bundle variables and `TF_TOKEN_*` / `TF_CLI_ARGS*` survive;
anything else you set through `api.extraEnv` does not. **If discovery stops working
after upgrading, look here first** —
[`terrapod-query.md`](terrapod-query.md#the-schema-subprocesses-get-an-allowlisted-environment)
names what survives.
([`GHSA-658f-j48w-w8m9`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-658f-j48w-w8m9).)

### `terrapod-migrate` refuses an `http://` TFE address

The TFE API token it carries is as privileged as the Terrapod token beside it, and
one was protected while the other was not. Loopback is exempt; set
`TERRAPOD_ALLOW_INSECURE_TRANSPORT=1` if plaintext is deliberate — the same
variable the Terrapod side already honours.
([`GHSA-r98p-vq35-mcg2`](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-r98p-vq35-mcg2).)

### A PR-comment command requires push access to the repository

`vcs.require_push_permission_for_commands` defaults **true**, so a `terrapod plan`,
`apply` or `unlock` comment from someone without push access is now refused with a
reply saying so (`help` is exempt — it only prints the usage table). Previously
commenting was enough.

Who this stops that was previously succeeding: outside contributors on a public
module repository, organisation members with triage or read, bots and CI tokens that
are not collaborators, and GitLab Reporters (level 20) or anyone whose access
`members/all/` cannot resolve. The check **fails closed**, so a provider rate limit
or outage refuses commands rather than allowing them — which means a VCS rate-limit
incident now also silences the comment surface. Set
`api.config.vcs.require_push_permission_for_commands: false` to restore the previous
behaviour. (`GHSA-x4jp-5g4j-f8rr`, critical.)

### The pinned platform-tool versions moved

`registry.platform_tools` now defaults to OPA **1.21.1**, Trivy **0.75.0** and
Checkov **3.3.21** (from 1.21.0 / 0.74.0 / 3.3.19). These binaries are not in any
image — they are fetched at run time, and a fetch failure is **fatal to the run**
rather than a silently skipped gate.

So on an air-gapped or egress-restricted deployment whose mirror holds only the old
versions, every run with a policy set applied and every run with scanning on will
error after `helm upgrade`, without the operator having changed anything. **Seed
your mirror with the new versions first**, or pin the old ones in your values. A
routine bump on a connected deployment; not routine behind a mirror.

### A variable-set assignment rule naming `drift_status` or `locked` stops matching

Both are self-assignable, so a rule selecting on either could be satisfied by the
workspace's own owner — which is the escalation `GHSA-49q6-pm68-3xgw` is about. A
new rule naming one is refused with `422`; a **rule stored before this release
silently matches nothing**, so the set stops reaching its workspaces. The only
signal is a warning in the API log, and if that set carried a required `TF_VAR` the
next plan fails with an opaque Terraform error.

Find them before upgrading:

```sql
SELECT id, name, assignment_rule
FROM   variable_sets
WHERE  assignment_rule ?| array['drift_status', 'locked'];
```

Re-express each rule on something an admin controls — a label is the usual answer —
or assign the set explicitly.

## Announced behaviour changes for 2.0

These are not deprecated surfaces. They are changes to what an existing field
reports, so there is no `Deprecation` header to watch. Each keeps its current
behaviour through every 1.x release.

### A run held at a post-plan gate stops reporting `planning` (v1.7.2)

**Today:** when a mandatory policy set, an enforced security scan, or a
mandatory post-plan run task stops a run, the run stays at `status: planning`
after its plan has finished. Since v1.7.2 it also reports what holds it, in the
read-only `blocked-by` attribute (`policy`, `security-scan` or `run-task`; null
otherwise).

**In 2.0:** the run reports the status Terraform Enterprise uses for the same
situation: `policy_override` when a mandatory policy set or an enforced security
scan holds it, and `post_plan_awaiting_decision` when a mandatory run task does.
The `tofu`/`terraform` CLI understands those statuses: it shows the failing
checks and asks whether to override, and `-auto-approve` overrides them for a
caller allowed to.

**What to do now:** if you have automation that treats "`planning` for a long
time" as a stuck run, key it on `blocked-by` instead. A run with a non-null
`blocked-by` is waiting for a decision, not stuck. Code that reads `blocked-by`
keeps working unchanged in 2.0.

## For maintainers

Mark an endpoint deprecated by injecting the FastAPI `Response` into the handler and
calling the helper from `terrapod.api.deprecation`:

```python
from datetime import date
from fastapi import Response
from terrapod.api.deprecation import mark_deprecated

@router.get("/old-thing")
async def old_thing(response: Response, ...):
    mark_deprecated(response, sunset=date(2027, 6, 30))
    ...  # keep serving the normal response
```

Then add a row to the **Active deprecations** table above and a note to the release
notes. The `sunset` date must be at least two minor releases out. Removal is a
separate change in a future MAJOR — and per the pre-release backward-compatibility
gate, dropping the route/attribute/key before its window completes will fail the
contract tests in CI.
