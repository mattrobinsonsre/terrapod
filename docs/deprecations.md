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
