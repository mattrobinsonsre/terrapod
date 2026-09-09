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
3. It stays working until **both** two minor releases **and** eight weeks have
   passed, whichever is later (and it never disappears in a MINOR or PATCH). The
   wall-clock floor is there because minors can land quickly — a window measured
   only in releases shrinks whenever we ship faster.
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

## Active deprecations

| Surface | Deprecated in | Sunset (removed no earlier than) | Replacement | Notes |
|---|---|---|---|---|
| `/api/terrapod/v1/…` (the whole Terrapod-native surface) | v1.7.0 | v2.0.0 / 2026-11-03 (8-week floor; if two minors have not shipped by then, the later date governs) | `/api/v1/…` | Same routes, same responses — only the prefix changed. Both are served; nothing to do until you upgrade your consumers. |

| `/api/v2/…` (TFE compatibility surface) | v1.7.0 | v2.0.0 / 2026-11-03 | `/api/tfe/v2/…` | Same routes, same responses. Service discovery advertises the new path and the CLI honours it, so `terraform`/`tofu` move by themselves. |
| `/v1/providers/…` (provider network mirror) | v1.7.0 | v2.0.0 / 2026-11-03 | `/api/v1/provider-mirror/…` | The mirror joins the other pull-through caches. Runner images write the old URL into the Job's CLI config themselves and move when they are upgraded. |

### `/api/v2/…` → `/api/tfe/v2/…` and `/v1/providers/…` → `/api/v1/provider-mirror/…`

`/api/v2` was both the TFE compatibility layer *and* a path inside Terrapod's own
version namespace, which would have made `/api/v2` unusable for a future Terrapod
v2. `/api/tfe/v2` says what it is. The mirror is a pull-through cache, so it sits
with `binary-cache` and `package-cache` rather than on a bare `/v1/`.

**Nothing to do for `terraform` or `tofu`.** They read the path from
`/.well-known/terraform.json`, and both honour it — verified with a real
`init` / `plan` / `apply` against a relocated surface. The old paths keep serving
for anything that caches discovery or bypasses it.

**What to update, in your own time:** your own scripts and `curl` calls; any
`go-terrapod`, provider or `terrapod-migrate` you pin directly; and Prometheus
queries **only when the label changes** — it does not yet. The metric
`path_template` deliberately keeps reporting the old path for both prefixes, so
existing dashboards keep working; it flips at the major, called out in the
upgrade notes.

**Runner images** still write the old paths into each Job's CLI config, on
purpose: those literals are compiled into the image and a runner lags the API by
design. They move in a later 1.x minor, before the sunset.

### `/api/terrapod/v1/…` → `/api/v1/…`

`/api/v1` is now the canonical Terrapod-native API. `/api/terrapod/v1` continues
to serve every route identically for the support window.

**Nothing breaks on upgrade** for a default install. Both prefixes are mounted
from the same routers, so they cannot diverge and the alias is not a partial copy.

Two exceptions, both only if you have overridden the relevant setting:
SSO (below), and a **pinned `webhookIngress.paths`** — that list is an allow-list,
and run-task callback URLs are now generated on `/api/v1`.

**What to update, in your own time:**

| Consumer | Action |
|---|---|
| Runner + listener images | Nothing **yet**. The shipped images still call the alias by design — they are expected to lag the API — so they move to `/api/v1` in a later 1.x minor, before the sunset. |
| `go-terrapod`, `terraform-provider-terrapod`, `terrapod-mcp` | Take a newer version; the prefix is internal to them |
| Your own scripts / `curl` / dashboards | Change `/api/terrapod/v1` to `/api/v1` |
| `values.yaml` | Nothing |

**If you use the optional split webhook Ingress** (`webhookIngress.enabled`),
its `paths` is an allow-list. The chart default now covers both prefixes, so a
default install needs nothing. If you have **pinned** that list in your own
`values.yaml`, add `/api/v1/vcs-events` and `/api/v1/task-stage-results` —
run-task callback URLs are generated on the canonical prefix, and an unlisted
path is not routed, which appears as a 404 at the sender rather than in
Terrapod's logs.

**One thing the alias does NOT cover — SSO.** The OIDC callback and SAML ACS URLs
are *registered with your identity provider*, which validates what Terrapod sends
against its own allow-list. Terrapod serving both prefixes does not help: an
unregistered `redirect_uri` is refused **at the IdP**, before the request reaches
us. So the callback URL is governed by an explicit switch,
`api.config.auth.legacy_callback_url`, which defaults to `true` (the old prefix).

To move it: add `{callback_base_url}/api/v1/auth/callback` — and
`/api/v1/auth/saml/acs` if you use SAML — to your IdP's allowed URLs **first**,
then set `legacy_callback_url: false`. The default flips in 2.0.0; see
[upgrading-to-2.0.md](upgrading-to-2.0.md).

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
notes. The `sunset` date must be at least two minor releases AND eight weeks out, whichever is later. Removal is a
separate change in a future MAJOR — and per the pre-release backward-compatibility
gate, dropping the route/attribute/key before its window completes will fail the
contract tests in CI.
