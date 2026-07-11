# Versioning & Support Policy

This is Terrapod's compatibility contract: **what a version number means, what
won't break when you upgrade, how long components can lag, and how anything is
ever removed.** It exists so a risk-averse team can pin to Terrapod and know
exactly what an upgrade guarantees.

Terrapod follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`)
for every public surface below.

## What each version bump means

| Bump | Meaning |
|---|---|
| **PATCH** (`0.58.0 → 0.58.1`) | Bug fixes and security fixes only. No new surface, no behaviour change beyond fixing the bug. Always safe to take. |
| **MINOR** (`0.58.0 → 0.59.0`) | New, **backward-compatible** features. Existing routes, response attributes, wire protocol, SDK methods, Helm values, and config keys keep working unchanged. Additive only. Safe to take without config changes. |
| **MAJOR** (`0.x → 1.0`, `1.x → 2.0`) | The only release that may contain a **breaking change** to a stable surface — and only after the deprecation window below. Read the migration notes before upgrading. |

> **Pre-1.0 note.** While Terrapod is `0.x`, the guarantees below are the ones we
> hold ourselves to and enforce in CI, but SemVer formally permits breaking
> changes in a `0.MINOR` bump. We call out any such change loudly in the release
> notes. From **`1.0.0`** onward, the MAJOR/MINOR/PATCH contract is absolute.

## The stable surfaces

Each surface below is a contract. Within a MAJOR version, **nothing on it is
removed, renamed, or retyped** — changes are additive only. Each is guarded by
an automated CI gate (a committed snapshot that fails the build on any removal),
so a breaking change cannot merge by accident.

| Surface | What's frozen | CI gate |
|---|---|---|
| **`/api/v2/` CLI API** | The routes + response attributes the `terraform`/`tofu` `cloud` backend and `go-tfe` consume ([`tfe-cli-surface.md`](tfe-cli-surface.md)) | route + attribute snapshots |
| **`/api/terrapod/v1/` runner + listener wire protocol** | Every route, the `runs/next` attributes, the SSE event names, and the runner artifact/body keys (`has_changes`, `policy-results`, `job-status`, …) | route + attribute snapshots |
| **go-terrapod SDK** | Exported methods + struct JSON tags | SDK ↔ server contract test |
| **terraform-provider-terrapod** | Resource/data-source attribute schema | provider schema |
| **Helm values** | `values.yaml` keys | schema (`additionalProperties: false`) + removal snapshot |
| **Config keys** | Every `Settings` key (`config.yaml` / `TERRAPOD_*`) | config-key snapshot |
| **Database schema** | Reversible + **expand/contract**-safe migrations | migration contract gate |

The **rest** of the `/api/terrapod/v1/` management API (beyond the runner/listener
wire subset) is Terrapod-native and evolves more freely, but still follows the
deprecation window below for any removal.

## Component version skew

Terrapod is several components that upgrade on **different schedules** — the API
and web ship together in one Helm release, but runners and listeners run as
ephemeral Jobs / long-lived Deployments that may lag the control plane during a
rolling upgrade or live in a separate, independently-upgraded cluster.

- **API ↔ web (BFF):** always the same version (one Helm release). No skew.
- **API ↔ runner / listener:** a runner or listener image is **forward-compatible
  with a newer API across at least 2 minor versions** (`N-2`). You can upgrade the
  control plane and let runners/listeners catch up later; the reverse (a newer
  runner against an older API) is also supported within the same window. This is
  what the frozen wire protocol above buys you.
- **API ↔ go-terrapod / provider / `terraform` CLI:** an SDK/provider/CLI built
  against API version `N` keeps working against API `≥ N` within the same MAJOR.

## Deprecation window

Nothing on a stable surface is removed without notice. To remove or rename
anything:

1. **Announce** — mark it deprecated in the release notes and (for API responses)
   emit a `Deprecation` / `Sunset` HTTP header; keep the old behaviour working.
2. **Grace period** — leave the deprecated surface in place for at least **2
   minor releases** (or until the next MAJOR, whichever is longer).
3. **Remove** — only in a MAJOR release, with the removal listed in the migration
   notes.

Database columns follow the stricter **expand → migrate → wait a release →
contract** rule (a rolling upgrade runs old and new code against the same DB, so
a column can only be dropped a release *after* the code stops using it). See the
migration convention in [`AGENTS.md`](../AGENTS.md).

## Supported releases

- The **latest minor** is fully supported (bug + security fixes).
- The **previous minor** receives **security backports** until the next minor
  ships.
- Older minors are best-effort. Because Terrapod ships as a single Helm chart and
  every migration is reversible, upgrading forward one minor at a time is the
  supported upgrade path.

Releases and their notes are published on the
[GitHub Releases](https://github.com/mattrobinsonsre/terrapod/releases) page.

## How this is enforced

Compatibility isn't a promise on paper — it's checked three ways:

1. **Per-surface CI gates** (the snapshots in the table above) fail the build the
   moment a route, attribute, wire key, SDK tag, Helm value, config key, or
   migration would break.
2. **The pre-release audit** includes a dedicated breaking-change review of the
   diff since the last tag.
3. **An independent AI back-compatibility review** runs adversarially against the
   release diff to catch anything the gates and the audit missed.

These gates are landing incrementally under
[#550](https://github.com/mattrobinsonsre/terrapod/issues/550): the route,
response-attribute, config-key, and migration (reversibility + expand/contract)
gates are live today; the go-terrapod, provider-schema, and Helm-value-removal
gates and the `Deprecation`/`Sunset` header mechanism follow before `1.0.0`.

If you find a compatibility break that slipped through, it's a bug — please
[open an issue](https://github.com/mattrobinsonsre/terrapod/issues).
