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

## Active deprecations

**None.** No public Terrapod surface is currently deprecated.

When the first deprecation lands, it will be listed here in this shape:

<!--
| Surface | Deprecated in | Sunset (removed no earlier than) | Replacement | Notes |
|---|---|---|---|---|
| `GET /api/…/old-thing` | v1.3.0 | v2.0.0 / 2027-06-30 | `GET /api/…/new-thing` | Response shape is identical; only the path changed. |
-->

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
