# AGENTS.md

Guidance for contributors and AI coding assistants working in the Terrapod
repository. If you are using an AI assistant (Claude Code, Cursor, Copilot,
Aider, etc.), point it at this file — it captures the architecture, the
contracts, the test tiers, and the conventions that keep changes consistent.

New here? Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup and the
contribution workflow, then come back here for the deeper architecture and
contract rules. **Contributions are very welcome — including AI-assisted
("vibe") contributions** — as long as they follow the contracts below and
ship with tests.

---

## What Terrapod is

Terrapod is a free, open-source **platform** replacement for Terraform
Enterprise. It is **not** a fork of Terraform or OpenTofu — it provides the
collaboration, governance, state management, and UI layer that wraps around
`terraform` or `tofu` as pluggable execution backends.

Terrapod targets **TFE V2 API compatibility for the surface that
`terraform`, `tofu`, and `tfci` consume** — service discovery, the
cloud-block run lifecycle, variable + variable-set management, and the module
+ provider registry CLI download protocols. That subset is mounted at
`/api/v2/` and is treated as a stable contract for those clients. Everything
else — workspace/role/registry management, agent pools, notifications, run
tasks, drift detection, the SSE streams, and the runner protocol — is
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
   `test-org`, or any placeholder where the organization is referenced.
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
  page must actually render in the E2E stack.
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
- **Helm values ↔ schema** — when you add/rename/remove a key in
  `values.yaml`, update `values.schema.json` to match (it uses
  `additionalProperties: false`, so `helm lint` fails otherwise), and make
  sure the template renders it.

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
