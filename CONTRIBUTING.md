# Contributing to Terrapod

Thanks for your interest in Terrapod! Contributions are genuinely welcome —
whether you're fixing a typo, filing a well-described bug, or building a whole
feature. **AI-assisted ("vibe") contributions are welcome too** — if you pair
with an AI coding assistant, point it at [`AGENTS.md`](AGENTS.md), which
captures the architecture, contracts, and conventions it needs to produce
changes that fit.

This guide covers the workflow. For the deeper architecture and the hard
contract rules, read [`AGENTS.md`](AGENTS.md).

## Start with an issue

**Every change beyond a genuinely trivial tweak starts with a GitHub issue.**
A trivial tweak is a typo, a one-line comment, or a formatting-only change —
those can go straight to a PR. Everything else — a feature, a bug fix, a
refactor, a behaviour change, a new endpoint, a doc set — gets an issue first,
and the pull request references it (`closes #123`).

Why: it gives every change a searchable, reviewable home, lets us agree on the
approach before you invest in the code, and keeps the project's history easy
to follow. We hold ourselves to the same bar — the maintainers open issues for
their own non-trivial work too.

So the flow is:

```
issue  →  branch  →  pull request  →  review  →  merge
```

If you're not sure whether something needs an issue, open one — it costs a
minute and saves rework.

> Good first contributions: browse issues labelled
> [`good first issue`](https://github.com/mattrobinsonsre/terrapod/labels/good%20first%20issue)
> and [`help wanted`](https://github.com/mattrobinsonsre/terrapod/labels/help%20wanted).

## Set up a local environment

Everything builds, lints, and tests **in Docker** — there's no local Python
environment to install. You'll need Docker and (for the full local stack) a
local Kubernetes (e.g. Rancher Desktop) plus [Tilt](https://tilt.dev/).

```sh
make test      # run the test suite in Docker
make lint      # ruff check + format --check
make dev       # bring up the full local stack on Kubernetes (Tilt)
make dev-down   # tear it down
```

See [`docs/local-development.md`](docs/local-development.md) for the full local
setup, and [`docs/getting-started.md`](docs/getting-started.md) for a tour of
the running platform.

## Make your change

The platform core is **Python** (FastAPI + async SQLAlchemy), which keeps the
contribution barrier low; the consumer ecosystem (the Go SDK, the Terraform
provider, and the migration/publish CLIs) is **Go**. The repository layout and
the per-component build commands are in [`AGENTS.md`](AGENTS.md#repository-layout).

Two contracts are worth internalising before you start (both detailed in
[`AGENTS.md`](AGENTS.md)):

- **API ↔ Consumer** — if you change the API, update every consumer it affects
  (go-terrapod first, then the provider / frontend / migration tool).
- **Code ↔ Tests** — every change ships with tests at the right tier (unit /
  services-api / integration / e2e). A change that alters a user-visible
  surface or a behaviour the system depends on (state machine, locking, SSE,
  RBAC) ships its end-to-end coverage in the same PR.

## Verify before you push

Lint passing tells you nothing about whether the build or tests pass. Run the
check that matches what you changed:

- **Python** → `make test`
- **Frontend** → `npm run build` from `web/`
- **Helm** → `helm template ./helm/terrapod -f helm/terrapod/values-local.yaml`
- **Go** → `go build ./...` and `go test ./...` in the relevant module

## Open the pull request

- Base your branch on `main`; don't stack a PR on another open branch.
- Reference the issue (`closes #123`).
- Use [conventional commit](https://www.conventionalcommits.org/) style for the
  title (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, …).
- Keep the description honest about what's covered and what isn't (e.g. "UI
  verified via build, not yet browser-clicked").

CI runs the Python, frontend, Helm, and Go jobs in containers; a gate job
aggregates the results. A green CI is required to merge.

## A couple of house rules

These keep the public repository clean — please read them, they're firm:

- **No internal references.** The repository is world-readable forever. Don't
  put company names, private hostnames, internal repo/cluster names, or
  specific customer/deployment details in commits, PRs, code comments, or docs.
  Describe real-world motivation in generic shapes ("a multi-workspace
  monorepo").
- **Respect peer projects.** Other open-source projects in this space are
  respected peers, not rivals to put down. Neutral, factual technical
  comparison is fine; disparagement (even passive-aggressive) is not. Describe
  what Terrapod does and let it stand on its own merits.

Full detail on both is in [`AGENTS.md`](AGENTS.md#content-hygiene-hard-requirements).

## Maintainers

Terrapod is built and maintained by a small core team with site-reliability
and platform-engineering backgrounds — it's a platform built by the kind of
people who operate it. Matt Robinson
([@mattrobinsonsre](https://github.com/mattrobinsonsre)) currently leads the
project; [@karl0r](https://github.com/karl0r) and
[@mhempstock](https://github.com/mhempstock) are maintainers with full access.
Any maintainer can review and merge; the lead has the final say for now. The
best way to join the team is to start contributing — we'd love the help.

## License

By contributing, you agree that your contributions are licensed under the
project's [GPLv3](LICENSE) license.
