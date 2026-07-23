# Private Module Source Authentication

Terraform/OpenTofu modules can be sourced from private git repositories
(`git::https://…`, `git::ssh://…`, scp-style `git@host:…`) and other private
locations. Terrapod already authenticates modules from its own **private
registry** automatically (with the run's short-lived runner token). This page
covers **private, non-registry git module sources** — the ones that used to need
a hand-rolled `pre_init` hook or a custom runner image.

You declare a git credential **once** — as a sensitive workspace variable scoped
to a host/org — and Terrapod authenticates every matching module fetch during
`init`, **with the credential never appearing in any run log**.

## How it works

A git credential is a **sensitive variable** in one of two categories:

| Category | Scope (`key`) | Value | Materialized as |
|---|---|---|---|
| `git_http_auth` | a URL pattern — `github.com`, `github.com/myorg`, `gitlab.example.com` | `{username, token}` **or** a VCS-connection reference | a git credential helper (token supplied out-of-band) |
| `git_ssh_auth` | a URL pattern | `{private_key, known_hosts}` | `~/.ssh` key + a per-host ssh config |

Because these are ordinary variables, they get **encryption at rest**, **per-run
delivery via the per-run Kubernetes Secret** (never the Job spec, never a log),
and — crucially — **variable sets**: define a credential once and assign it to
many workspaces (define-once, assign-many, least-privilege). Multiple entries
(e.g. one per org) cover multiple orgs.

Values are always sensitive: the API forces `sensitive=true` for these categories
and never returns the stored value.

## Protocol rewriting (ssh ↔ https)

Module `source` URLs become **protocol-agnostic** — Terrapod routes each fetch
over whichever protocol it holds a credential for, **with no change to your
module source strings**. Each entry carries an optional `rewrite`:

- **`to_https`** (on a `git_http_auth` entry) — `git::ssh://git@host/org/repo` and
  scp-style `git@host:org/repo` are fetched over `https://host/org/repo` using the
  token. Keep your `ssh://` sources as-is: **no SSH keys, no deploy-key setup**.
- **`to_ssh`** (on a `git_ssh_auth` entry) — `git::https://host/org/repo` is
  fetched over `ssh://git@host/org/repo` using the deploy key.

The rewrite target is **tokenless** — the credential is supplied by git's
credential helper out-of-band, so nothing sensitive ever reaches a command line
or `git config --list`.

## Value sources (`git_http_auth`)

The token can come from two sources:

- **Static** — a personal access token you supply:
  `{"source":"static","username":"x-access-token","token":"ghp_…","rewrite":"to_https"}`
  (`username` defaults to `x-access-token` if omitted).
- **VCS connection (recommended)** — reference an existing
  [VCS connection](vcs-integration.md); Terrapod **mints a short-lived
  git-HTTPS token** from it at run time (a GitHub-App installation token, or the
  GitLab connection's access token):
  `{"source":"vcs_connection","vcs_connection_id":"vcs-…","rewrite":"to_https"}`.
  No PAT to rotate — the token is minted per run.

`git_ssh_auth` is static only (VCS connections mint HTTPS tokens, not SSH keys):
`{"private_key":"-----BEGIN …","known_hosts":"github.com ssh-ed25519 …","rewrite":"none"}`.

**`known_hosts` is optional for github.com and gitlab.com.** Their SSH host keys
are baked into the runner image (authoritative — github.com from the GitHub
`/meta` API, gitlab.com verified against GitLab's published fingerprints), so a
`git::ssh://` fetch to those SaaS hosts verifies out of the box. Supply
`known_hosts` only to pin a **self-hosted** GitHub Enterprise / GitLab host; for
github.com/gitlab.com you can leave it blank.

## Enabling it

Nothing to enable — it's on by default. Create the credential like any variable.

### Via the API / SDK

```jsonc
// POST /api/terrapod/v1/workspaces/{id}/vars
{ "data": { "attributes": {
  "key": "github.com/myorg",
  "category": "git_http_auth",
  "value": "{\"source\":\"vcs_connection\",\"vcs_connection_id\":\"vcs-…\",\"rewrite\":\"to_https\"}"
} } }
```

### Via the provider

```hcl
resource "terrapod_variable" "github_org" {
  workspace_id = terrapod_workspace.app.id
  key          = "github.com/myorg"
  category     = "git_http_auth"
  sensitive    = true
  value = jsonencode({
    source            = "vcs_connection"
    vcs_connection_id = terrapod_vcs_connection.github.id
    rewrite           = "to_https"
  })
}
```

Assign it to many workspaces at once by putting it in a **variable set** instead.

## Log safety

The runner streams its output to the UI, so credential handling is **log-safe by
construction**: tokens live only in `0600` files read out-of-band by git's
credential helper; SSH keys are `0600`; the `insteadOf` rewrite targets are
tokenless; nothing sensitive is ever passed as a command-line argument. Do not
enable `GIT_TRACE` / `GIT_CURL_VERBOSE` in a workspace variable — those would make
git print credential headers into the run log.

## Not covered here (fast-follows)

External non-Terrapod registry tokens, `~/.netrc` HTTP-archive auth, and cloud
object-store sources (`s3::`, `gcs::`) are handled by the runner's workload
identity today and gain first-class credential kinds on the same framework in a
follow-up.
