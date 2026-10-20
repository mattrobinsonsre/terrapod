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
| **PATCH** (`0.58.0 → 0.58.1`) | Bug fixes and security fixes, plus **opt-in configuration** that changes nothing until you set it. No behaviour change for an existing configuration, no new API surface, no schema migration. Always safe to take. |
| **MINOR** (`0.58.0 → 0.59.0`) | New, **backward-compatible** features. Existing routes, response attributes, wire protocol, SDK methods, Helm values, and config keys keep working unchanged. Additive only. Safe to take without config changes. |
| **MAJOR** (`0.x → 1.0`, `1.x → 2.0`) | The only release that may contain a **breaking change** to a stable surface — and only after the deprecation window below. Read the migration notes before upgrading — for the next one, [Upgrading to 2.0](upgrading-to-2.0.md). |

> **What "opt-in configuration" means in a PATCH, and what it does not.** A patch
> may add a Helm value, a config key or an environment variable **whose default
> leaves the deployment behaving exactly as before**. The test is not whether a
> release adds surface; it is whether taking it can change anything for an
> operator who does not touch their values. An unset key cannot.
>
> This is narrower than it sounds, and the exclusions are the point. A patch
> still may not add an API route or a response attribute (a consumer running an
> older version would face a surface that appears and disappears across a patch),
> change the runner or listener wire protocol, carry a schema migration, or alter
> what any existing configuration does. Those remain MINOR.
>
> The earlier wording said "no new surface", which conflated *adding a key* with
> *being risky to take* — and they are not the same thing. It also made the
> honest fix for a gap in an older release line impossible to ship there: an
> operator on the previous minor could be left with a mechanism that only works
> for one of something, told the fix exists, and offered it only via a minor
> carrying a release of unrelated features. Where the fix is a defaulted-off key,
> that trade is not worth making.

> **The one exception: closing a security hole.** A MINOR may *tighten* a
> permission on a surface that was wrongly open, because leaving it open is
> itself the harm and a deprecation window would mean shipping a known hole for
> two more minors. This is narrow — it covers removing access that should never
> have been granted, never removing a route, an attribute, or a config key. It
> has been used three times before v1.9.0, which uses it again — see below.
> In **v1.4.0**, when GPG signing-key registration
> went from "any authenticated caller" to requiring `registry:admin`; an
> unprivileged account could otherwise add a trust anchor for the whole provider
> registry. Twice in **v1.7.0**: retrying a run went from `run:cancel` to
> requiring what creating that run requires (`run:plan`, `run:apply` or
> `run:apply-destroy`), because on an auto-applying workspace a retry *is* an
> apply and `run:cancel` is a `plan`-tier capability; and notification webhooks
> and run-task callbacks stopped being delivered to loopback and link-local
> addresses, which are a request-forgery primitive rather than a destination
> anyone intends — an operator who does intend one names it in
> `outbound_requests.allowed_hosts`.
> When it is used, the release notes say so plainly and the affected doc pages
> carry the migration step, so an operator meets it before their next apply
> fails rather than after — see
> [registry-publishing.md](registry-publishing.md#1-register-the-publishers-gpg-public-key).
>
> **v1.9.0 uses it more widely, and once outside its own wording.** Five tightenings
> close reported findings: fork pull requests no longer plan by default, a non-admin
> cannot join a secret-bearing variable set, using a VCS connection requires a claim,
> a scoped token cannot widen itself, and every PR-comment command that acts requires push
> access. Those are permission tightenings, which is what this exception is for. The
> sixth is not: **`terrapod merge` was removed outright**, and a comment verb is a
> command surface, not a permission. It is recorded in
> [`deprecations.md`](deprecations.md#removed-without-a-deprecation-window-v190)
> rather than stretched silently, because the alternative was shipping a critical
> finding — anyone who could comment could merge — for two more minors.


> **Stability note.** As of **`1.0.0`**, the MAJOR/MINOR/PATCH contract below is
> absolute and enforced in CI: no stable surface is removed, renamed, or retyped
> outside a MAJOR release that has completed the deprecation window. (Before
> `1.0.0`, these were the guarantees we held ourselves to, but SemVer formally
> permitted breaking changes in a `0.MINOR` bump; any such change was called out
> loudly in the release notes.)

## The stable surfaces

Each surface below is a contract. Within a MAJOR version, **nothing on it is
removed, renamed, or retyped** — changes are additive only. Each is guarded by
an automated CI gate (a committed snapshot that fails the build on any removal),
so a breaking change cannot merge by accident.

| Surface | What's frozen | CI gate |
|---|---|---|
| **`/api/v2/` CLI API** | The routes + response attributes the `terraform`/`tofu` `cloud` backend and `go-tfe` consume ([`tfe-cli-surface.md`](tfe-cli-surface.md)) | route + attribute snapshots |
| **`/api/v1/` runner + listener wire protocol** | Every route, the `runs/next` attributes, the SSE event names, and the runner artifact/body keys (`has_changes`, `policy-results`, `job-status`, …) | route + attribute snapshots |
| **go-terrapod SDK** | Exported methods + struct JSON tags | SDK ↔ server contract test |
| **terraform-provider-terrapod** | Resource/data-source attribute schema | provider schema |
| **Helm values** | `values.yaml` keys | schema (`additionalProperties: false`) + removal snapshot |
| **Config keys** | Every `Settings` key (`config.yaml` / `TERRAPOD_*`) | config-key snapshot |
| **Database schema** | Reversible + **expand/contract**-safe migrations | migration contract gate |

> `/api/v1/` is canonical. The former prefix `/api/terrapod/v1/` is still served
> for the deprecation window and carries `Deprecation` / `Sunset` headers; it is
> covered by the same stability guarantees until its published sunset. See
> [deprecations.md](deprecations.md).

The **rest** of the `/api/v1/` management API (beyond the runner/listener
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
- **runner ↔ listener:** normally not a pair that can skew, because both default to
  the chart's `appVersion` — but `runners.image.tag` and `listener.image.tag` are
  **independently settable**, so pinning one and not the other creates a skew the
  wire protocol above does not cover. Pinning only one of the two tags is what makes
  any of the following reachable at all, and the general cause is the same each time:
  **the listener image is what builds the runner Job's pod spec**, so a feature whose
  runner-side half depends on something in that spec is a feature that couples the
  two images. The coupled features are:

  - **[Per-workspace cloud identity](cloud-identity.md)**, from v1.10.0. The
    in-memory volume the runner's credential phase writes its tokens into is created
    by the *listener*. A newer runner launched by a listener that predates the feature
    finds no volume, hits the container's read-only root filesystem, and fails closed
    with an `EROFS` error naming a filesystem path rather than a stale image.
    **Upgrade listeners before (or with) runners**, and before enabling cloud identity.
  - **[Ansible configure runs](runners.md#the-job-a-configure-runs-in)**, from v2.0.0.
    This one couples in **both** directions, because the two images each supply half of
    the pod: the listener adds the execution-environment sidecar and the shared volume,
    and the *runner* image is what carries the small static agent the sidecar is probed
    for. A newer runner under an older listener gets no sidecar and nothing to drive;
    an older runner under a newer listener fails in the `agent-copy` init container,
    which copies that agent out of the runner image and finds no such file. Either way
    the failure is confined to configure runs — the sidecar is only added when the
    workspace has a configure operation, so plan and apply are unaffected — and either
    way it fails closed at pod startup rather than part way through a playbook.
    **Upgrade the two together.**
- **runner ↔ execution environment:** **no window, because there is no protocol
  to skew.** An execution environment carries no Terrapod code at all — the agent
  that drives it is copied in from the runner image at pod startup — so the agent
  and the orchestrator that talks to it over loopback are always the same build,
  and an EE never needs rebuilding for a Terrapod release. What an EE must satisfy
  is not a protocol but a short fixed contract: `ansible-playbook`,
  `ansible-galaxy`, `ansible`, `ssh` and `git` on `PATH`, `ansible` runnable as the
  uid the image runs as, and a uid that can read the shared volume. It is asserted
  at the EE's startup probe, for operator-supplied images as much as for Terrapod's
  own, so a non-conforming image fails at startup naming what is absent rather
  than part way through a playbook.

  **Widening that contract is a breaking change for operator-supplied images**, and
  is treated as one: an image built against today's contract must keep working, so
  a new requirement waits for a MAJOR or arrives as something the agent tolerates
  the absence of. Terrapod's own images are checked against the same contract at
  build time and again against the published digest, and a gate asserts the
  startup-probe requirement never exceeds what those builds check — otherwise an
  image of ours could pass its build and fail its own probe.
- **chart ↔ listener:** the chart renders the runner ConfigMap and the listener
  reads it, and `RunnerConfig` **ignores keys it does not recognise** — so a
  runner-Job setting introduced in release `N` is inert on a listener image from
  an earlier one, silently rather than with an error. An ordinary `helm upgrade`
  moves them together (the listener Deployment carries a `checksum/runner-config`
  annotation, so a ConfigMap change rolls it), which is why this is reachable
  only where `listener.image.tag` is pinned behind the chart. The current example
  is [`runners.applyServiceAccount`](runners.md#a-separate-serviceaccount-for-the-apply-phase):
  an older listener runs the apply phase on `runners.serviceAccount`, so a
  deployment that believes its plans are on a narrower cloud role is not getting
  the split. **Unpin, or upgrade listeners with the chart, before relying on a
  new runner-Job setting.**
- **API ↔ go-terrapod / provider / `terraform` CLI:** an SDK/provider/CLI built
  against API version `N` keeps working against API `≥ N` within the same MAJOR.

### Runtime version handshake

The API advertises its running version as `terrapod-version` in the
`/.well-known/terraform.json` discovery document, and `go-terrapod`'s
`Client.VersionCheck` compares it against the SDK's build-pinned version using
exactly the policy above: compatible when they share the same MAJOR and the API
is at least as new as the SDK (`api >= sdk`). The provider (a startup warning
diagnostic), `terrapod-migrate`, and `terrapod-publish` (a stderr warning) all
run this check so a version-skew surfaces as a clear, actionable message instead
of a confusing downstream failure. It only ever **warns** — never blocks — and a
dev build on either side skips the check.

## Deprecation window

Nothing on a stable surface is removed without notice. To remove or rename
anything:

1. **Announce** — mark it deprecated in the release notes, list it in
   [`deprecations.md`](deprecations.md), and (for API responses) emit
   `Deprecation` / `Sunset` / `Link` HTTP headers; keep the old behaviour working.
2. **Grace period** — leave the deprecated surface in place until **both** of the
   following have passed, whichever is later:

   - **2 minor releases**, and
   - **8 weeks** of wall-clock time from the release that announced it.

   The wall-clock floor matters because minors can land quickly: two of them may
   elapse in under three weeks, which is not long enough for an operator to
   notice a deprecation, schedule the work, and redeploy. A deprecation window
   measured only in releases shrinks whenever we ship faster, which is exactly
   backwards.

3. **Remove** — only in a MAJOR release, on or after the published `Sunset` date,
   with the removal listed in the migration notes.

The live list of what is currently deprecated and when it sunsets is
[`deprecations.md`](deprecations.md).

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

All of these gates are live as of `1.0.0`, landed under
[#550](https://github.com/mattrobinsonsre/terrapod/issues/550): the route,
response-attribute, **runner/listener wire-protocol**, config-key, migration
(reversibility + expand/contract), **Helm-values-removal**, **go-terrapod
exported-surface**, and **provider-schema** snapshot gates all fail CI on a
removal/rename, and the `Deprecation`/`Sunset` header mechanism (see
[`deprecations.md`](deprecations.md)) is ready for the first deprecation. Every
public surface in the table above is now guarded end to end.

If you find a compatibility break that slipped through, it's a bug — please
[open an issue](https://github.com/mattrobinsonsre/terrapod/issues).
