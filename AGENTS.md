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
- **Enforcement (this is what blocks non-mobile-friendly changes):** the
  `responsive` Playwright project runs the suite at a **phone viewport**
  (`e2e/tests/responsive.spec.ts`) and is the **mobile guard**; the existing
  Desktop Chrome projects are the **desktop guard**. Both run in CI. A new
  frontend page or major component **must add a responsive assertion** to the
  mobile suite (at minimum `expectNoHorizontalPageScroll`, plus the
  touch-model checks relevant to it) **in the same PR** as its desktop spec,
  and **must not** break the mobile guard. Breaking the mobile guard fails CI
  — that is the gate.

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
- **Migrations** — Alembic with async SQLAlchemy and hash-based revision IDs
  (generate with `python3 -c "import secrets; print(secrets.token_hex(6))"`),
  not sequential numbers. Every migration has a real `upgrade()` **and**
  `downgrade()`.
- **Substantial API tempfiles go on the attached PVC, not `/tmp`** — on the
  API pod `/tmp` is RAM-backed. Anything that can hold tens of MB (provider
  archives, VCS tarballs, state snapshots, config tarballs) must be written to
  the configured ephemeral PVC dir, not `/tmp`, or it will OOM the pod.
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
