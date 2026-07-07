# Responsive / mobile audit (#719 Stage 0)

Per-route treatment decisions for the first-class mobile pass. Derived from
a static scan of every `src/app/**/page.tsx` for the mobile-hostile patterns
in the #719 touch model: unwrapped tables (horizontal overflow), nested
inner-scroll panes, `useState` tabs (reset on reload), and `title=` hover
tooltips (invisible on touch). Governing constraints (from AGENTS.md): **one
DRY, viewport-driven implementation; desktop never sacrificed.**

Treatment legend:
- **card** — below `md`, the table becomes a stacked card/row list surfacing
  the fields that matter (one `<ResponsiveList>`, table on wide).
- **scroll** — acceptable last-resort `overflow-x-auto` fallback for a
  low-traffic, genuinely tabular admin grid.
- **drop-inner** — remove the nested scroll region so the page is the single
  scroll container on mobile (esp. the run-log viewer).
- **url-tab** — move `useState` tab state into the URL (survives reload/back).
- **tooltip→tap** — replace hover-only `title=` info with a tap-visible
  equivalent (or make it decorative only).

## Priority surfaces (day-to-day + the hard cases)

| Route | Issues | Treatment | Stage |
|---|---|---|---|
| `/workspaces` | 1 table + **4 inner-scroll**, 9 hover tooltips | **card** list + **drop-inner** (filter/label popovers shouldn't nest-scroll) + tooltip→tap | 2 |
| `/workspaces/[id]` | **4 tables** (1 wrapped), **13 hover tooltips**, dense multi-tab | **card** the var/state/run tables + tooltip→tap + tab bar responsive (already url-synced) | 2–3 |
| `/workspaces/[id]/runs/[runId]` | **2 inner-scroll** (log viewer), 8 hover tooltips, 6–7 stacked panels | **drop-inner** (page follows the log tail) + run-page IA split + SSE cadence + tooltip→tap | 3 |
| `/catalog` | clean | card grid already; verify at 390px | 2 |
| `/registry/modules`, `/registry/providers` | clean lists | verify; likely fine | 2 |
| `/registry/modules/[name]/[provider]` | 3 tables (2 wrapped) + 1 inner-scroll | card the unwrapped table + drop-inner | 2 |

## Admin surfaces (mostly tabular — card the primary ones, scroll-fallback the rest)

| Route | Issues | Treatment | Stage |
|---|---|---|---|
| `/admin/agent-pools/[id]` | 2 tables (1 wrapped), **url-tab** | card + **url-tab** | 1/4 |
| `/admin/variable-sets/[id]` | 2 unwrapped tables, **url-tab** | card + **url-tab** | 1/4 |
| `/admin/roles` | 1 table, **url-tab** | card + **url-tab** | 1/4 |
| `/admin/execution-hooks/[id]` | 1 table (wrapped), **url-tab** | **url-tab** (table already wrapped) | 1/4 |
| `/admin/autodiscovery` | 2 tables + **2 inner-scroll**, 5 tooltips | card + drop-inner + tooltip→tap | 4 |
| `/admin/vcs-connections` | 1 table, **6 tooltips** | scroll + tooltip→tap | 4 |
| `/admin/users`, `/admin/roles`, `/admin/variable-sets`, `/admin/policy-sets`, `/admin/provider-templates`, `/admin/execution-hooks`, `/admin/catalog`, `/admin/agent-pools`, `/admin/binary-cache`, `/admin/bulk-update` | 1–2 unwrapped tables | card the list where cheap, else **scroll** fallback | 4 |
| `/admin/audit-log` | table already wrapped | verify | 4 |
| `/catalog/[id]` | 1 table + 1 inner-scroll, 6 tooltips | card + drop-inner + tooltip→tap | 4 |
| `/labels` | 2 unwrapped tables | card | 4 |
| `/settings/tokens` | table wrapped | verify | 4 |
| `/settings/sessions` | 1 unwrapped table | card | 4 |
| `/registry/providers/[name]` | 1 unwrapped table | card/scroll | 4 |

## Clean / no work (verify only at 390px)

`/` (dashboard), `/login`, `/catalog`, `/registry/modules`, `/registry/providers`,
`/api-docs`, `/slack/link`, `/auth/*`, `/app/[...path]`.

## Cross-cutting (global foundation — Stage 1)

- **Nav** (`components/nav-bar.tsx`) — ~22 top-level items → grouped adaptive nav (see the nav-IA mockup / #719). Mobile menu currently dumps all vertically.
- **`title=` hover tooltips** — 96 across the app; audit each for real-info-on-hover and convert to tap-visible.
- **URL-state tabs** — 4 pages hold the tab in `useState` (agent-pools/[id], variable-sets/[id], roles, execution-hooks/[id]); move to URL + land the reload/back regression suite.
- **Clickable rows** — ~46 `onClick`/`cursor-pointer` row/card targets; scroll-vs-tap review.
- **Viewport/safe-area** — `viewport-fit=cover`, kill horizontal page scroll globally.

## Totals (scan)

24 `<table>` across 22 routes, only 7 wrapped in `overflow-x-auto`; 11
inner-scroll regions; 96 `title=` tooltips; 4 `useState` tabs; ~46 clickable
rows. The worst offenders by density are the two workspace pages and the run
detail page — the surfaces on the incident-response critical path, which is
exactly where mobile must be excellent.
