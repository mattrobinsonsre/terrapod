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

### Which entry is used

An entry keyed to `host/org` covers every repository **under** that org —
`host/org/anything.git` — not just the org path itself. A bare-host entry
(`host`, no `/org`) covers everything on that host.

When both exist, **the more specific one wins**: an entry for `github.com/org-a`
serves that org and the bare `github.com` entry serves the rest. You do not have
to order them; Terrapod emits the scoped entries first so the host-wide one
cannot shadow them.

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
- **VCS connection** — reference an existing
  [VCS connection](vcs-integration.md); Terrapod derives a git-HTTPS token from
  it at run time:
  `{"source":"vcs_connection","vcs_connection_id":"vcs-…","rewrite":"to_https"}`.

  **On GitHub this is the recommended source.** The connection is a GitHub App,
  so Terrapod **mints a fresh installation token per run**, narrowed to
  `contents: read` — the whole of what a clone needs. There is no PAT to rotate,
  and the credential the runner holds can do nothing but read code.

  **On GitLab it is off by default — see the warning below.**

`git_ssh_auth` is static only (VCS connections mint HTTPS tokens, not SSH keys):
`{"private_key":"-----BEGIN …","known_hosts":"github.com ssh-ed25519 …","rewrite":"none"}`.

**`known_hosts` is optional for github.com and gitlab.com.** Their SSH host keys
are baked into the runner image (authoritative — github.com from the GitHub
`/meta` API, gitlab.com verified against GitLab's published fingerprints), so a
`git::ssh://` fetch to those SaaS hosts verifies out of the box. Supply
`known_hosts` only to pin a **self-hosted** GitHub Enterprise / GitLab host; for
github.com/gitlab.com you can leave it blank.

### GitLab: the connection's token cannot be narrowed

A GitLab VCS connection does not hold an app identity Terrapod can mint from. It
holds a **Personal or Group Access Token an operator pasted in**, and there is no
GitLab call that returns a narrower copy of one. So a `vcs_connection` credential
on GitLab means handing the runner Job **that token, whole** — with every
permission and every project it covers, for as long as it is valid — into a
container that is also executing the workspace's own IaC.

Two things make that sharper than it first looks:

- **The connection is chosen in a variable *value*.** It is named in a string
  nothing in the workspace schema constrains, so the connection a run mints from
  is not the one the workspace is configured with and need not be related to it.
  Naming a connection **is** now authorized — see below — but the check is the
  only thing standing between a workspace variable and an operator's standing
  token, where on GitHub the token itself is also narrow and short-lived.
- **Nothing expires it per run.** A GitHub installation token lives an hour and
  reads code; this one is the operator's standing token.

**Every minted credential is bounded by the connection's repository allowlist**, and
that check applies even to the workspace's own connection. It has to: a
`git_http_auth` credential is installed for the scope in its **`key`**, a bare URL
pattern the workspace owner chooses, so `key = github.com` installs the token for
the whole host and the workspace's own configuration can then clone anything the
credential reaches. Without this the allowlist would bound the workspace's
*repo URL* and not the *credential*, which is not what "restricts the connection to
those repositories" means. A run whose repository is outside the allowlist is
refused with a message naming both.

**A connection other than the workspace's own is additionally authorized at mint
time.** A workspace may always use the connection it is configured with. Anything
else is checked against the **workspace owner**, since a run has no live caller, and
the run is **refused with a message naming the credential and the connection**
rather than run without it. Two of the four claims do not apply on this path: there are
no roles to evaluate, so a label claim does not grant here, and nothing is treated
as a platform admin — so a workspace whose claim to a connection rests only on
labels cannot mint from it, and should name its own connection or use a `static`
credential. See
[VCS integration → Naming a VCS connection is authorized](vcs-integration.md#naming-a-vcs-connection-is-authorized).
(GHSA-v8g7-pqrj-8mcm. The check itself arrived in v1.7.7 and v1.8.2; releases
before those performed none. The repository allowlist above is newer still — it
did not exist on either of them, so on those two releases this authorization
check was the whole of the bound.)

So it is **off by default on every supported release**, behind:

```yaml
api:
  config:
    vcs:
      gitlab:
        allow_token_delivery_to_runners: false   # the default
```

With it off, a run whose workspace carries such a variable **fails immediately**
with a message naming the variable, this key, and the alternative — it is never
dropped silently, because a credential that quietly vanishes leaves `init` to
fail later against a private module source with an error naming neither the
credential nor the cause.

**The alternative needs nothing enabled, and is the better answer in most
deployments:** use a **`static`** credential holding a project- or group-scoped
GitLab token you minted for exactly this, with `read_repository` and nothing
else. That is a narrowing GitLab *can* do — it just has to be done when the
token is created, not when it is used.

Turn the switch on only if you have read the above and accept it — a private
runner fleet fetching modules from one group, with a connection token scoped to
that group, is a perfectly reasonable place to. It is an informed opt-in, not a
default anyone should inherit by upgrading.

## Enabling it

Nothing to enable for **static** credentials or for **GitHub** VCS connections —
they are on by default. Create the credential like any variable.

**GitLab VCS connections are the exception**: they need
`api.config.vcs.gitlab.allow_token_delivery_to_runners: true`, and you should
read [the warning above](#gitlab-the-connections-token-cannot-be-narrowed)
before setting it.

### Via the API / SDK

```jsonc
// POST /api/v1/workspaces/{id}/vars
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

## Handled by workload identity

External non-Terrapod registry tokens, `~/.netrc` HTTP-archive auth, and cloud
object-store sources (`s3::`, `gcs::`) resolve through the runner's workload
identity — the same short-lived, no-stored-secret path the rest of a run's cloud
access uses.

## If credentials appear configured but `init` still fails

Before v1.5.4 the runner's `$HOME` sat on the read-only root filesystem, so the
git configuration could not be written at all. The failure was logged at
*warning* and the run continued without credentials, so `init` failed later
against a private module source with an error naming neither the credential nor
the cause — while the line above it said auth had been configured (#1442).

Two things changed. `$HOME` is now a writable volume in the runner Job, and a
credential that cannot be applied **fails the run** with a message naming the
count and the path, rather than being skipped. Terrapod also asks git whether it
actually reads the configuration, because writing a file and git consulting it
are different claims.

If you worked around this with `HOME` or `GIT_CONFIG_GLOBAL` in
`runner_extra_env`, both can be removed after upgrading. Leaving them set is
harmless.

