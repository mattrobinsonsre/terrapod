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
   continue to work.
