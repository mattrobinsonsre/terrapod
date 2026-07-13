# AGENTS.md

Guidance for contributors and AI coding assistants working in the Terrapod
repository. If you are using an AI assistant (Claude Code, Cursor, Copilot,
Aider, etc.), point it at this file — it captures the architecture, the
contracts, the test tiers, and the conventions that keep changes consistent.
For a quick, machine-friendly map of the whole repo — entry points, the
codebase layout, the feature catalogue, and how to enable each feature — see
[`llms.txt`](llms.txt) at the repo root.

New here? Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup and the
contribution workflow, then come back here for the deeper architecture and
contract rules. **Contributions are very welcome — including AI-assisted
("vibe") contributions** — as long as they follow the contracts below and
ship with tests.

---

## What Terrapod is

Terrapod is a free, open-source **platform** replacement for Terraform
Enterprise. It is **not** a fork of Terraform or OpenTofu — it provides the
collaboration, governance (label-based RBAC **and OPA/Rego policy-as-code**),
state management, and UI layer that wraps around
`terraform` or `tofu` as pluggable execution backends.

Terrapod targets **TFE V2 API compatibility for the surface that
`terraform`, `tofu`, and `tfci` consume** — service discovery, the
cloud-block run lifecycle, variable + variable-set management, and the module
+ provider registry CLI download protocols. That subset is mounted at
`/api/v2/` and is treated as a stable contract for those clients. Everything
else — workspace/role/registry management, agent pools, **policy sets
(OPA/Rego — the open-source equivalent of TFE's Sentinel)**, notifications,
run tasks, drift detection, the SSE streams, and the runner protocol — is
Terrapod-native and lives at `/api/terrapod/v1/`.

The verified CLI-consumed endpoints are catalogued in
[`docs/tfe-cli-surface.md`](docs/tfe-cli-surface.md). When extending the API:
if a route is on that list it stays at `/api/v2/`; otherwise it goes under
`/api/terrapod/v1/`.

## Repository layout

| Path | What it is | Language |
|---|---|---|
| `services/terrapod/` | API server (FastAPI) **and** the runner listener — one codebase, different entrypoints. Implicit namespace package (no `__init__.py`). | Python 3.13 |
| `web/` | Next.js frontend + BFF (the single ingress; proxies `/api/*` to the API). | TypeScript / React |
| `go-terrapod/` | Public, canonical Go SDK for the Terrapod API. Source of truth for the Go-side view of every endpoint. | Go |
| `provider/` | `terraform-provider-terrapod` — a thin wrapper over go-terrapod. | Go |
| `migrate/` | `terrapod-migrate` — TFE/HCP + Atlantis migration CLI. | Go |
| `publish/` | `terrapod-publish` — registry publish CLI (client-signed provider/module uploads). | Go |
| `helm/terrapod/` | The Helm chart (the only supported deployment mechanism). | YAML |
| `alembic/` | Async Alembic migrations (hash-based revision IDs). | Python |
| `docs/` | User + operator documentation. | Markdown |

The four Go modules at the repo root (`go-terrapod/`, `provider/`,
`migrate/`, `publish/`) each have their own `go.mod` so their dependency
surfaces stay independent.

## Build, lint, and test

**Everything runs in Docker via `make` — there is no local Python
environment to set up.**

```sh
make test          # pytest in Docker (with a Postgres + Redis + S3-emulator stack)
make lint          # ruff check + format --check in Docker
make test-down     # tear down the test containers
make dev           # local Kubernetes dev stack (Tilt)
make dev-down      # stop the dev stack
```

Per-surface verification before you push (lint alone is **not** enough):

- **Python** changes → `make test`
- **Frontend** changes → `npm run build` from `web/` (the Next.js prerender
  step catches things `tsc` and ESLint cannot)
- **Helm** changes → `helm template ./helm/terrapod -f helm/terrapod/values-local.yaml`
- **Go** changes → `go build ./...` + `go test ./...` in the relevant module

## Architecture principles

1. **API-first** — every UI action is backed by a public API endpoint; the
   V2 API is the contract.
2. **OpenTofu-friendly** — support both `terraform` and `tofu` as execution
   backends. Terrapod is the platform, not the engine.
3. **Postgres + native object storage** — Postgres for relational data;
   native cloud object storage (S3, Azure Blob, GCS) with a filesystem
   fallback for dev.
4. **Kubernetes-native** — deployed exclusively via the Helm chart.
5. **ARC-pattern execution** — a runner *listener* is a stateless,
   event-driven thin launcher. It connects to the API over outbound SSE,
   receives run notifications, creates Kubernetes Jobs, and reports Job
   metadata back. The listener holds **zero run state**; the API owns the run
   lifecycle via a periodic reconciler.
6. **Bring your own auth** — local accounts, OIDC, SAML; no baked-in IdP.
7. **Modern UX** — the web UI is a first-class concern.
8. **BFF (Backend For Frontend) — ALL traffic goes through the BFF (hard
   requirement)** — the Next.js frontend is the single ingress entry point and
   proxies `/api/*` to the API. The browser never talks to the API directly.
   Every feature — including SSE and streaming — must work through the full
   proxy chain. New SSE endpoints must be added to the `headers()` config in
   `web/next.config.js` with `Content-Encoding: none`, or SSE silently fails
   through the BFF.
9. **Single organization (hard requirement)** — Terrapod does **not** support
   multiple organizations at any level. There is no `org_name` column
   anywhere. The literal organization name is always `default` — in code,
   tests, fixtures, docs, and comments. Never use `{org}`, `<org>`,
   `test-org`, or any placeholder where the organization is referenced. This
   is a **deliberate design choice, not a missing feature**: organizations
   are a SaaS multi-tenancy mechanism, a self-hosted install is already one
   tenant (the deployment is the tenant boundary — for separate tenants, run
   an instance per tenant), and it aligns with HashiCorp's own current
   guidance to minimize organizations and consolidate onto one. Segmentation
   within a deployment uses label-based RBAC. Full rationale:
   [`docs/architecture.md` → Why a single organization](docs/architecture.md#why-a-single-organization).
10. **RFC3339 timestamps** — all datetimes are timezone-aware UTC; the API
    always serialises them as RFC3339 with a trailing `Z` (e.g.
    `2025-01-01T00:00:00Z`), never `+00:00`. Required for `go-tfe`
    compatibility.
11. **Multi-replica safe, no leader election** — the API runs with multiple
    replicas behind a load balancer. All background work uses the distributed
    scheduler (`services/scheduler.py`), which coordinates via Redis. Never use
    in-process state (module globals, `asyncio.Event`, in-memory queues) for
    cross-replica coordination, and never use raw `asyncio.create_task()` for
    background work.
12. **Original implementation only** — Terrapod targets API *compatibility*
    with the public TFE V2 specification, but all implementation code must be
    entirely original. Referencing the public API docs to understand the
    contract is expected; copying implementation logic from any HashiCorp
    proprietary source is not.
13. **No sync work in async handlers (hard requirement)** — FastAPI + uvicorn
    runs a single event loop per worker. Any synchronous CPU-heavy or blocking
    I/O call inside an `async def` handler starves the whole replica. Wrap such
    calls in `asyncio.to_thread(...)` / `run_in_executor(...)`, or use an
    async-native alternative (`httpx.AsyncClient`, `aiofiles`,
    `asyncio.subprocess`, `asyncpg`, `redis.asyncio`). When a plain `def`
    endpoint genuinely needs sync libraries, prefer `def` — FastAPI runs it in
    a threadpool for you; that rescue does **not** apply to `async def`.

## The API ↔ Consumer contract (hard)

Terrapod's API has several classes of consumer, each with its own contract.
**Every API change must update every consumer it affects.**

- **Web UI** (`web/`) — SSR fetches + client `fetch()` calls.
- **go-terrapod** (`go-terrapod/`) — the canonical Go SDK; the source of
  truth for the Go-side shape of every resource.
- **terraform-provider-terrapod** (`provider/`) — imports go-terrapod for
  every API call; holds only Terraform-plugin-framework code.
- **terrapod-migrate** (`migrate/`) and **terrapod-publish** (`publish/`) —
  both import go-terrapod for every Terrapod-side write.

The workflow when extending the API:

1. Add the endpoint to the appropriate router (`/api/v2/` only if it's on the
   CLI-surface list; otherwise `/api/terrapod/v1/`).
2. Add a typed method to **go-terrapod** + a test (the shape matches the
   JSON:API response).
3. Add the consumer code that needs it (provider resource, frontend page,
   migration/publish writer).

When a JSON:API attribute name changes, update go-terrapod's struct
field/tag, the provider's matching attribute, every frontend `fetch` that
references it, and the migration tool if it touches that field. go-terrapod is
the single source of truth for the Go-side view; the provider and migration
tools do not carry their own JSON:API marshaling.

## The Code ↔ Tests contract (hard)

Every code change ships with tests at the right tier(s). **No new endpoint,
service function, UI surface, SSE event, or hard invariant lands without its
accompanying tests.**

| Tier | Where | What it exercises | DB / Redis |
|---|---|---|---|
| **Unit** | `services/tests/{auth,runner,storage}/`, introspection tests | Pure functions, single-class behaviour, source-introspection invariants | Mocked / none |
| **Services-API** | `services/tests/{services,api}/` | Handler + service-layer logic with mocked DB/Redis (the bulk of tests) | `AsyncMock` |
| **Integration** | `services/tests/integration/` | Multi-row workflows needing a real engine (FKs, unique constraints, CASCADE, the run state machine) | Real Postgres |
| **E2E** | `e2e/tests/*.spec.ts` (Playwright) | Full user flows through the real BFF proxy chain, real DB/Redis, real SSE, real RBAC | Full stack |

Routing rules of thumb:

- A **router** endpoint → a services-API test in `services/tests/api/`
  (happy path + auth/RBAC + error responses).
- A **service function** with mockable deps → a services-API test in
  `services/tests/services/` (`AsyncMock`-driven). Reach for integration only
  when it depends on Postgres-row-level semantics.
- A **run state-machine transition** or multi-step lifecycle → an integration
  test (real DB).
- A **new SSE event** → (a) a services-API test asserting it's published from
  the right path, **and** (b) an E2E spec asserting the UI re-renders on
  receipt without a manual reload.
- A **new frontend page / RBAC gate / user-facing flow** → an E2E spec. For
  RBAC, include a **negative-path** spec with a non-admin session asserting the
  action is blocked. `tsc` + ESLint are necessary but **not** sufficient — the
  page must actually render in the E2E stack. It must **also** carry a
  responsive guard (see *Responsive / mobile-first UI* below) — a page that
  only works on desktop is an incomplete change, not a done one.
- A **new hard invariant** ("X must never happen") → a source-introspection
  test that reads the implementation and asserts the absence of the forbidden
  pattern, so the invariant fails CI loudly if a future change violates it.
- A **behaviour-changing fix for a reported bug** → a regression test named
  after the failure mode that fails on the pre-fix code and passes after.

E2E isolation invariants (higher worker/shard counts make violations flaky,
not deterministic): every test creates its own uniquely-named resources and
tears them down; never assert global counts or absolute list positions; never
depend on another spec having run; no fixed `setTimeout` waits — use
Playwright's auto-waiting.

## Responsive / mobile-first UI (hard requirement)

Mobile is a **first-class target**, not an afterthought — a UI change that is
not mobile-friendly is **not done** and must not merge. The full brief and the
staged plan live in issue **#719**; the rules a contributor must follow:

- **Two axes — width drives *layout*, pointer drives *touch-friendliness*.**
  These are independent signals; never infer one from the other (a wide
  tablet/foldable is touch; a narrow desktop window is not). **Viewport width**
  governs layout density — cards↔table, tab-bar↔`<select>` picker, hidden
  columns — and is expressed in **CSS** (`sm:`/`md:`/`lg:`, `@container`), or in
  JS via `useIsMobile()` only where *layout behaviour* must branch.
  **Pointer type** governs touch-friendliness — destructive-action `confirm()`
  guards, avoiding nested scroll traps, tap-target sizing — via the `touch:` /
  `fine:` CSS custom variants (in `globals.css`) or `useIsTouch()`
  (`= matchMedia('(pointer: coarse)')`, reflecting the *primary* pointer, in
  `web/src/lib/use-media-query.ts`). So the same page can be a roomy
  desktop-width layout **and** touch-safe at once. Still **never** branch on the
  user agent for either axis.
- **One DRY, viewport-driven implementation.** No forked `Mobile*`/`Desktop*`
  component trees, and **never** branch on the user agent
  (`navigator.userAgent`, device-detect libraries, server-side "is this a
  phone"). Adapt on **actual available width**: CSS first — Tailwind
  responsive utilities (`sm:`/`md:`/`lg:`) and CSS container queries
  (`@container`); reach for JS (`useMediaQuery`/`useIsMobile` in
  `web/src/lib/use-media-query.ts`) only where *behaviour* must branch (e.g.
  bottom-sheet vs inline panel), keyed to the same breakpoints, SSR-safe.
- **Desktop is never sacrificed.** Every change is breakpoint-scoped; at
  desktop widths the result is pixel-identical to before. A desktop window
  narrowed to phone width adapting is *expected*, not a regression.
- **Touch model** (touch is not a small mouse): no hover-only affordances (no
  hover-reveal actions, no info that only appears in a `:hover` tooltip); no
  reliance on double-tap or right-click; **no inner scrollbars nested inside a
  scrolling page** (the page is the scroll container on mobile); tap targets
  ≥44px; inputs ≥16px (no iOS zoom-on-focus); **no horizontal *page* scroll**
  at any width; **URL is the source of truth for tab/view state** (it must
  survive reload / back / deep-link — no `useState`-only tabs). Prefer
  drill-down to a route over cramming panels into one dense page.
- **Confirmation guards on mutating actions (hard).** Two tiers, and they are
  not the same: **(1) an irreversible destructive action — a delete or remove
  with no undo (delete variable / notification / run task, remove run trigger /
  remote-state consumer, delete state version, delete workspace) — MUST prompt a
  `confirm()` in BOTH modes** (touch *and* precise pointer). Losing data on a
  single stray click is a desktop hazard too, not just a touch one. **(2) Any
  other single-tap mutation that is reversible or lower-stakes (enable/disable
  toggles, lock/unlock, queue a destroy run, …) MUST prompt a `confirm()` on
  touch** (via `useIsTouch()`), where a mis-tap is easy; on a precise pointer it
  may proceed without one. Do NOT rely on a bespoke inline two-step
  (Delete→Confirm swap) as the guard — use the native `confirm()` so the tiers
  stay uniform. Form *submits* after deliberate data entry (create/edit save)
  are not "single-tap mutations" and don't need a guard.
- **Actions are real buttons, not clickable text (hard).** Any control that
  performs an action — a row action (Edit / Delete / Enable-Disable / Verify /
  Download / Rollback / Remove), a form Save/Cancel, a toggle — MUST render as a
  proper button with a background, padding, and rounded corners (e.g.
  `px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600`;
  destructive variants use `bg-red-900/40 … text-red-300`), giving it a real
  tap target (~44px). Do NOT ship "little clickable chunks of text" — a bare
  coloured-text `<button>` (e.g. `text-xs text-brand-400` with no background) —
  as an action affordance: it reads as a link, is a poor tap target on touch,
  and is easy to miss on desktop. Genuine *navigation* (to another
  page/resource) may still be a text link; *actions* are buttons.
- **Enforcement (this is what blocks non-mobile-friendly changes):** the
  `responsive` Playwright project runs the suite at a **phone viewport**
  (`e2e/tests/responsive.spec.ts`) and is the **mobile guard**; the existing
  Desktop Chrome projects are the **desktop guard**. Both run in CI. A new
  frontend page or major component **must add a responsive assertion** to the
  mobile suite (at minimum `expectNoHorizontalPageScroll`, plus the
  touch-model checks relevant to it) **in the same PR** as its desktop spec,
  and **must not** break the mobile guard. Breaking the mobile guard fails CI
  — that is the gate.

## Internationalisation — every UX string is translated (hard requirement)

The web UI is fully internationalised with **next-intl** (#767). Locale is
resolved per-request from the `NEXT_LOCALE` cookie → `Accept-Language` → `en`
(no `/[locale]/` URL segment); the nav globe switcher writes the cookie. `en`
(US English) is the **source** catalog (`web/messages/en.json`); every other
locale deep-merges over it, so a partial catalog always renders (English
fallback, never a `MISSING_KEY`). The AI plan-summary/chat is translated at
**view time** by the model (locale-agnostic — works for every locale without a
catalog entry).

The rule a contributor must follow — **a UX change is not accepted unless its
multi-language implementation ships in the same PR**:

- **No hardcoded user-facing strings.** Every label, button, heading,
  placeholder, `<option>`, table header, empty state, toast/error/success
  message, `confirm()` text, tooltip, and badge word goes through
  `useTranslations(...)`/`getTranslations(...)` — never a raw English literal in
  JSX. **Do** leave code identifiers, terraform/HCL keywords, resource
  addresses, product names, env vars, and CLI flags untranslated (they're not
  UX copy). This is **enforced**: the `i18n:lint` gate (`npm run i18n:lint`, in
  CI) is an AST guard that fails on a new raw JSX literal not routed through
  next-intl, ratcheting against a committed baseline
  (`web/scripts/i18n-hardcoded-allowlist.json`). A genuine non-copy literal is
  suppressed with an `i18n-ignore` comment on the line.
- **A locale is complete or it is not offered — no partial locales ship.** Add
  the key to `web/messages/en.json` (the source), then to **every offered
  locale** (`web/messages/<code>.json` for each code in `locales` in
  `src/i18n/config.ts`). The `i18n:check` gate (`npm run i18n:check`, run in CI)
  fails the build if any offered locale is missing a key or has an extra one, so
  a half-translated language can never merge. English deep-merge stays only as a
  crash guard (never render `MISSING_KEY`), **not** as a licence to ship a
  partial language. If you can't translate a new string into every offered
  locale, either translate it (the catalogs are machine-translatable in bulk —
  see the fill pipeline) or drop that locale from `locales` until it is caught
  up. `de` is the maintained reference; **`en-GB` is the one exception** — a
  British dialect *override* that carries only the spelling deltas from the
  American source (the shared strings are genuinely identical, not a gap), so it
  is gated as a subset, not full parity.
- **Preserve ICU + tags.** Placeholders (`{name}`, `{count, plural, one {…}
  other {…}}`, `#`, escaped `'{'`/`'}'`) and rich-text tag names (`<code>`,
  `<strong>`, `<link>`, …) are structural — translate only the human words
  between them. A lone `'` in a value starts an ICU quote; escape a literal
  apostrophe as `''`. Every catalog string must parse as valid ICU.
- **A new frontend page/component ships its i18n in the same PR** as the
  component itself — the same way it ships its responsive assertion and its E2E
  spec. A page with raw English literals is an incomplete change, not a done
  one.

## Conventions

- **Issue-first** — every change beyond a genuinely trivial tweak (a typo, a
  one-line comment, a formatting-only change) starts with a GitHub issue, and
  the PR references it (`closes #N`). The flow is **issue → branch → PR →
  merge**. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **Conventional commits** — `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
  etc.
- **Branches** — feature branches off `main`; never push directly to `main`;
  never stack a PR on another open feature branch (always base on `main`).
- **Namespace package (hard requirement)** — `services/terrapod/__init__.py`
  must **not** exist. Its absence enables PEP 420 implicit namespace packages
  so each Docker image can include only the sub-packages it needs.
- **Migrations — reversible + expand/contract (hard requirement, gated)** —
  Alembic with async SQLAlchemy and hash-based revision IDs (generate with
  `python3 -c "import secrets; print(secrets.token_hex(6))"`), not sequential
  numbers. Every migration has a real `upgrade()` **and** `downgrade()` (no
  stubs — enforced by `tests/db/test_migration_contract.py`). Because the API
  runs multiple replicas, a rolling upgrade runs **old and new code against the
  same database at once**, so a migration must never **contract** the schema
  (drop/rename/retype a column or table in `upgrade()`) in the same release the
  code stops using it — that breaks the old replica still serving traffic.
  Follow **expand → migrate → wait a release → contract**: add the new column
  and dual-write/backfill in release N, switch reads in N, and only drop the old
  column in N+1 (or later). Every `upgrade()`-side contraction is ledgered
  (`tests/db/migration_contractions.json`); a new one fails CI until you
  consciously acknowledge it followed this discipline (regenerate the ledger
  with `UPDATE_API_CONTRACT=1 pytest tests/db/test_migration_contract.py`).
- **Substantial API tempfiles go on the attached PVC, not `/tmp`** — on the
  API pod `/tmp` is RAM-backed. Anything that can hold tens of MB (provider
  archives, VCS tarballs, state snapshots, config tarballs) must be written to
  the configured ephemeral PVC dir, not `/tmp`, or it will OOM the pod.
- **The API route contract (heading to v1.0.0)** — every HTTP route is pinned
  in a committed snapshot (`services/tests/api/api_route_contract.json`), and
  `tests/api/test_route_contract.py` fails CI on any diff. Removing or renaming
  a route is a **breaking change** for a consumer that lags the server across
  version skew (the `terraform`/`tofu` `cloud` backend + `go-tfe` on `/api/v2/`,
  or a runner/listener on `/api/terrapod/v1/`) — it requires a MAJOR bump or a
  documented deprecation, **not** a snapshot regen. **Adding** a route is
  additive: accept it by regenerating the snapshot in the same PR —
  `UPDATE_API_CONTRACT=1 pytest tests/api/test_route_contract.py` — a conscious,
  reviewed act so new surface never slips in silently. (More of the "no breaking
  changes" program lands under #550.)
- **The config-channel contract (hard requirement)** — a three-way contract
  binding the **config code**, the **Helm chart**, and the **chart tests**:
  the in-code config models (the API's Pydantic `Settings`, the listener's
  `RunnerConfig`) ↔ the chart that feeds them (`values.yaml` /
  `values.schema.json` / `configmap-api.yaml` / `configmap-runner.yaml` /
  the Deployment templates) ↔ the `helm-smoke` CI job that renders the chart
  and asserts the wiring. Change one leg, update all three in the same PR.
  `Settings` is rendered into `config.yaml` by `configmap-api.yaml`;
  `RunnerConfig` into `runners.yaml` by `configmap-runner.yaml`. The rules:
  - **Non-sensitive settings flow through a ConfigMap, end to end.** Adding one
    has **three legs that must all land in the same PR**: (1) a `values.yaml`
    entry with a rationale comment, (2) a matching `values.schema.json`
    constraint (the schema uses `additionalProperties: false`, so `helm lint`
    fails if `values.yaml` carries an undeclared key — include template-only
    `| default` keys too; K8s pass-through objects like `podSecurityContext` /
    `nodeSelector` / `affinity` / `tolerations` use schema `type: object` with
    no property restrictions), and (3) a **render line in the ConfigMap
    template**
    (`configmap-api.yaml` for `Settings`, `configmap-runner.yaml` for
    `RunnerConfig`). Miss leg 3 and the value is silently inert: the operator
    sets it in `values.yaml`, it never reaches the pod, and the code default
    wins. `helm template -f <profile>` must show the key in the rendered
    ConfigMap.
  - **Secrets** (tokens, private keys, passwords, connection strings) go via
    **`secretKeyRef`** / env on the Deployment, and are **never** rendered into
    a ConfigMap. Prefer a first-class `existingSecret`/key block in the
    Deployment template over relying on `extraEnv`.
  - **The chart never sets a non-sensitive setting via a `TERRAPOD_*` env var.**
    Deployment `env:` is reserved for secrets (`secretKeyRef`) and unavoidable
    runtime values (Downward API like `POD_NAME`, and the proxy/TLS env vars
    `HTTP(S)_PROXY`/`NO_PROXY`/`SSL_CERT_FILE` that libraries only read from
    env). Everything else is ConfigMap config. (Pydantic's env-var override
    still exists for an operator in a pinch, but the chart doesn't rely on it.)
  - **Verification leg — the `helm-smoke` CI job.** Chart behaviour is tested
    the house way: `helm template` then grep the **rendered** output (not the
    template text), the same pattern as the existing Ingress / embedded-Postgres
    smoke checks. A config-channel change must add/keep assertions there: the new
    key renders into the right ConfigMap (e.g. `grep -q "rate_limit:"` in the
    rendered `config.yaml`), and the rendered **Deployment** env carries no
    non-sensitive `TERRAPOD_*` (secrets via `secretKeyRef` and runtime values
    like `POD_NAME` are the only env). Grepping the rendered YAML — not Python
    source-introspection of the Go templates — is what catches a missing render
    line or a smuggled-in env var, and it's where chart invariants live.
- **The chart has three first-class value profiles — keep all three working.**
  `values.yaml` (production defaults), `values-local.yaml` (the Tilt dev loop),
  and `values-eval.yaml` (the `make eval` kind/k3d quickstart). Any chart value /
  default / schema change must keep **all three** rendering (`helm lint` +
  `helm template -f <profile>`) **and** the eval stack booting — the eval-boot CI
  job (kind + k3d matrix) is what enforces the last part. `values-eval.yaml` must
  stay **batteries-included / zero-external-deps** (it deploys in-cluster
  Postgres/Redis via `postgresql.deploy`/`redis.deploy`, filesystem storage, a
  local admin): if a new feature defaults to *requiring* an external dependency,
  override it OFF in `values-eval.yaml`, or one-command eval breaks. The embedded
  `postgresql.deploy`/`redis.deploy` datastores are **eval/dev only** (single
  replica, no HA/backups) and must stay **off by default** so production is
  unaffected.
- **Keep [`llms.txt`](llms.txt) current (it's a first-class deliverable, not an
  afterthought)** — `llms.txt` is the machine-friendly map AI assistants land on
  to understand and operate Terrapod. It is only useful if it stays accurate, so
  treat it like the API↔consumer contract: **a change that adds, renames, or
  removes a user-visible feature, a doc page, a Helm enable-key, or a top-level
  entry point MUST update `llms.txt` in the same PR** (and the matching feature
  tables in `README.md` / `docs/index.md`). The hard rule that file lives by is
  *no hallucinated endpoints, Helm values, or config keys* — every entry must
  resolve to something real in the repo, so when the underlying thing moves, the
  map moves with it. A stale or inaccurate `llms.txt` is a defect: it actively
  misleads an agent (and the operator it's advising), which is worse than no map
  at all. If you add a feature and only the deep doc knows about it, an agent
  pointed at the repo can't discover it — so wire it into `llms.txt` too.
- **Client operations use bounded backoff/retry by default (hard requirement)**
  — every outbound HTTP call a Terrapod process makes (runner → API result
  POSTs and artifact/state uploads, listener → API status/log/heartbeat, API →
  upstream registries / VCS / binary cache, notification + run-task webhook
  deliveries) MUST use a bounded retry with backoff. A transient timeout or 5xx
  must never silently drop the operation — a single un-retried `plan-result`
  POST once left `has_changes` unknown and falsely flagged a workspace as
  drifted. Pair it with idempotency: retry is only safe when the server handler
  is idempotent, so the rule is **make the handler idempotent, then retry** —
  don't skip the retry because a write isn't idempotent; fix the idempotency.
  Retry transient failures (timeouts, connection errors, 5xx); a definitive
  **4xx is final and never retried**. Deliberately best-effort calls (e.g.
  telemetry) may skip retry but MUST say so in a comment. When in doubt, retry.

## Content hygiene (hard requirements)

These protect the public repository. Git history, PRs, and source are
world-readable forever.

- **No internal references** — commit messages, PR text, **and source
  comments / docstrings** must never reference any company, internal
  hostname, internal repo/project name, internal cluster name, or specific
  customer/deployment. When describing an issue motivated by a real
  deployment, anonymise it ("a multi-workspace monorepo with large tarballs"),
  never name the source. If you catch a leak in your own draft, scrub it
  before pushing.
- **Respect peer open-source projects** — peer projects (such as Terrakube)
  are respected fellow projects, not competitors to disparage. Never denigrate
  them — not in commits, PRs, comments, docs, issues, release notes, or
  conversation — and never passive-aggressively. Honest, factual, neutral
  technical comparison is fine; comparison framed to rank or belittle is not.
  Position Terrapod on its own merits.

## Where to learn more

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — setup + the contribution workflow.
- [`docs/`](docs/) — user and operator documentation
  ([`docs/index.md`](docs/index.md) is the entry point;
  [`docs/local-development.md`](docs/local-development.md),
  [`docs/architecture.md`](docs/architecture.md), and
  [`docs/api-reference.md`](docs/api-reference.md) are good next reads).
