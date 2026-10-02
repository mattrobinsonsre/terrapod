# VCS Integration

Terrapod integrates with **GitHub** and **GitLab** to automatically trigger runs when you push commits or open pull requests / merge requests against a linked repository.

---

## Architecture

```
+---------------------+         +-------------------+
| Terrapod API Server |         | VCS Providers     |
|                     |         |                   |
| +-------+ +------+  |  HTTPS  | +-----------+    |
| | Poller| | Run  |  |-------->| | GitHub    |    |
| | (async| | Queue|  | (every  | | (App)     |    |
| |  task) | |      |  |  60s)  | +-----------+    |
| +---+---+ +---+--+  |         | +-----------+    |
|     |         |      |-------->| | GitLab    |    |
|     |         |      |         | | (Token)   |    |
|     |         |      |         | +-----------+    |
| +---v---------v---+  |         +-------------------+
| | ConfigVersions  |  |
| | + Runs           |  |         +-------------------+
| +------------------+  |         | GitHub Webhook    |
|                     |<---------| (optional, HMAC)  |
|                     |  POST    +-------------------+
+---------------------+
```

### How It Works

VCS integration has two layers:

1. **VCS connections** (platform-level) -- admin-created resources that configure authentication for a VCS provider. A GitHub connection uses a GitHub App installation; a GitLab connection uses an access token.
2. **Workspace linking** -- each workspace can reference a VCS connection and a repository URL. The workspace tracks a branch (e.g. `main`) and the poller creates runs when changes are detected.

Terrapod's background poller checks your VCS providers every 60 seconds (configurable) and creates two kinds of runs:

- **Branch push -> full plan/apply run** -- when a new commit lands on the tracked branch, Terrapod downloads the code and queues a normal run (plan, then apply if auto-apply is on or manually confirmed).
- **Pull request / merge request -> speculative plan** -- when an open PR/MR targets the tracked branch and has a new head commit, Terrapod queues a plan-only run. The plan shows what _would_ change if the PR were merged, but it can never be applied. A new speculative run is created each time the PR/MR is updated with a new commit.

### Webhooks are supported — and polling means they're never required

Terrapod **does support inbound VCS webhooks** for instant triggers, *and* it polls, so it works with or without them. The two are complementary, not either/or:

- **Webhooks are supported for both GitHub and GitLab.** GitHub posts to `POST /api/terrapod/v1/vcs-events/github` (HMAC-SHA256 signature); GitLab posts to `POST /api/terrapod/v1/vcs-events/gitlab` (`X-Gitlab-Token` secret). Configure one and a push or PR/MR triggers a run within ~a second instead of waiting for the next poll cycle. The webhook simply tells the poller to check *now*. Exposing only the webhook path publicly (so the rest of the platform can stay private) is covered in [Optional webhook ingress](deployment-webhook-ingress.md).
- **Polling is the resilient default, so webhooks are optional.** Terrapod polls each connected repo over **outbound HTTPS** every `poll_interval_seconds` (default 60). This requires **no inbound connections**, so it works behind firewalls and NATs with zero ingress configuration — and it means **nothing is lost without a webhook**: the same runs are created on the next poll cycle, just up to 60s later. A webhook is a latency optimization, never a dependency.
- **Why polling-first** — many self-hosted deployments sit in networks where a VCS provider cannot deliver an inbound webhook at all. Polling guarantees the integration works everywhere; webhooks make it faster where inbound delivery is available.

### Sparse fetch + caching

VCS archive fetches use git's smart-HTTP partial-clone protocol via the canonical `git` CLI (`--filter=blob:none` plus a `core.sparseCheckoutCone`-narrowed checkout) — Terrapod fetches only the commit, trees, and the blobs reachable under the configured `working-directory` ∪ `trigger-prefixes`. For a workspace tracking a single subdirectory of a monorepo, the wire fetch is bounded by that subdirectory rather than the whole repo. The CLI is invoked because git's **promisor partial-clone** mechanism (which lazily refetches missing blobs during checkout) is implemented inside the CLI itself; reproducing it in a pure-Python client would mean reimplementing core git internals.

For each `(connection, repo)` the poll cycle pre-computes a single union of every workspace's narrowing paths and shares one fetch across all of them. If any workspace under that group has no narrowing configured, the union collapses to "whole repo" — that workspace's plan/apply must see all files outside its declared paths, so we cannot narrow.

The fetched files are streamed directly to object storage as a gzipped tarball — repo-rooted entries, no wrapper directory — via a kernel pipe. Peak process memory is bounded by the largest single blob plus a 64 KiB chunk; clone state lives on the api pod's ephemeral PVC. The api pod requires per-pod ephemeral storage (`api.ephemeralStorage.enabled: true`, default 10 GiB) to back this pipeline — see [Deployment: VCS archive streaming and ephemeral storage](deployment.md#vcs-archive-streaming-and-ephemeral-storage-required-for-monorepo-workspaces).

Tarballs are cached in object storage at `vcs_archives/{conn_id}/{owner}/{repo}/{sha}-{paths_hash}.tar.gz`. The cache is content-addressed by `(commit SHA, paths hash)` — different path-narrowing sets produce different cache entries. Two callers must agree on the same path set to share a cache entry; the poll cycle's union pre-computation is what makes this happen for a monorepo with multiple workspaces. Entries are evicted by the artifact-retention sweeper after `vcs.archive_cache_retention_days` (default 7).

If the api pod's ephemeral PVC nears capacity (e.g. previous-pod orphans, very large concurrent fetches), the cache automatically sweeps stale tarball temp files AND orphan clone directories older than 5 minutes before reserving more disk. Tunable via `vcs.tmpdir_min_free_bytes` (default 2 GiB).

**Server requirements.** GitHub.com and GitLab.com both support the partial-clone capabilities Terrapod uses (`uploadpack.allowFilter`, `uploadpack.allowAnySHA1InWant`). Self-hosted GitLab >= 13.0 and GitHub Enterprise Server >= 3.0 support them too. If a server rejects a fetch with a capability error, the poller logs and retries on the next cycle — no silent fallback to a full-repo fetch, since that would mask broken servers.

### Provider Dispatch

The `VCSProvider` protocol defines the interface for VCS operations. The poller dispatches to the correct provider based on the VCS connection's `provider` field (`github` or `gitlab`). Each provider implements:

| Operation | Description |
|---|---|
| `get_branch_sha()` | Get the current HEAD SHA of a branch |
| `get_default_branch()` | Get the repository's default branch name |
| `download_archive()` | Download a tarball of the repository at a specific SHA |
| `list_open_prs()` | List open pull requests / merge requests targeting a branch |
| `parse_repo_url()` | Extract owner/repo from a repository URL |

**Source files:**
- `services/terrapod/services/vcs_provider.py` -- VCSProvider protocol
- `services/terrapod/services/vcs_poller.py` -- Background polling loop
- `services/terrapod/services/github_service.py` -- GitHub provider
- `services/terrapod/services/gitlab_service.py` -- GitLab provider

---

## Prerequisites

- A running Terrapod instance with API access
- Admin access to Terrapod (for creating VCS connections)
- **For GitHub**: a GitHub account or organization where you can create GitHub Apps
- **For GitLab**: a Project or Group Access Token with `api` scope (required for commit statuses and MR comments; `read_api` + `read_repository` is sufficient if you don't need status reporting)

---

## Enabling VCS

Set the following on the Terrapod API server:

```zsh
TERRAPOD_VCS__ENABLED=true
```

Or in Helm values:

```yaml
api:
  config:
    vcs:
      enabled: true
      poll_interval_seconds: 60  # How often to check for new commits
```

---

## GitHub Setup

GitHub integration uses a **GitHub App** for fine-grained permissions and org-level installation. The App is configured at the Terrapod platform level; individual workspaces reference the installation via a VCS connection.

### Step 1: Create a GitHub App

1. Go to **GitHub Settings > Developer settings > GitHub Apps > New GitHub App**
   - For an organization: `https://github.com/organizations/default/settings/apps/new`
   - For a personal account: `https://github.com/settings/apps/new`

![GitHub Apps List](images/github-app-list.png)

2. Fill in the form:

   | Field | Value |
   |---|---|
   | **App name** | `Terrapod` (must be globally unique -- add your org name if needed) |
   | **Homepage URL** | Your Terrapod URL (e.g. `https://terrapod.example.com`) |
   | **Webhook** | **Uncheck "Active"** (you can enable this later if you want faster feedback) |

3. Set **Repository permissions**:

   | Permission | Access | Purpose |
   |---|---|---|
   | **Contents** | Read-only (read & write if using apply-then-merge auto-merge — see [VCS Workflows](vcs-workflows.md)) | Download repository archives; auto-merging a PR creates a commit on the target branch |
   | **Metadata** | Read-only (auto-selected) | Repository metadata |
   | **Checks** | Read & write | Post check runs on commits |
   | **Commit statuses** | Read & write | Post plan/apply status to commits |
   | **Pull requests** | Read & write | Post and update PR comments, read mergeability state |
   | **Issues** | Read & write (only if using apply-then-merge) | Receive and post PR comments — GitHub serves PR comments through the Issues API |

   ![GitHub App Permissions](images/github-app-permissions.png)

   These permissions also cover [workspace autodiscovery](autodiscovery.md). No App reinstall is needed if you enable autodiscovery later — `Contents: read` and `Pull requests: read & write` are what the autodiscovery scanner uses.

4. Under **Where can this GitHub App be installed?**, choose based on your needs:
   - **Only on this account** -- if all repos are in one org/account
   - **Any account** -- if repos span multiple orgs

5. Click **Create GitHub App**

6. Note the **App ID** shown on the app settings page

![GitHub App General Settings](images/github-app-general.png)

7. Scroll down to **Private keys** and click **Generate a private key**. A `.pem` file will download -- keep this safe.

![GitHub App Private Key](images/github-app-private-key.png)

### Step 2: Install the GitHub App

1. From your GitHub App's settings page, click **Install App** in the left sidebar
2. Choose the account/organization where your Terraform repos live
3. Select **All repositories** or **Only select repositories** (pick the repos you want Terrapod to access)
4. Click **Install**
5. Note the **Installation ID** from the URL: `https://github.com/settings/installations/{installation_id}`

![GitHub App Installation](images/github-app-installation.png)

### Step 3: Create a GitHub VCS Connection

No platform-level GitHub configuration is needed beyond enabling VCS. The App ID, private key, and installation ID are all stored on the VCS connection itself (encrypted at rest).

![VCS Connections](images/admin-vcs-connections.png)

```zsh
curl -X POST https://terrapod.example.com/api/terrapod/v1/vcs-connections \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vcs-connections",
      "attributes": {
        "name": "my-github",
        "provider": "github",
        "github-app-id": 12345,
        "github-installation-id": 67890,
        "github-account-login": "my-org",
        "github-account-type": "Organization",
        "private-key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
      }
    }
  }'
```

The private key is stored encrypted at rest (protected by database encryption) and never returned in API responses.

Note the returned connection ID (e.g. `vcs-01234...`).

### GitHub App Authentication Details

- **JWT generation**: RS256-signed JWT from the app private key (10-minute lifetime, via PyJWT)
- **Installation tokens**: cached for 50 minutes (valid 60 min), used for all GitHub API calls
- The `server_url` on the connection determines the GitHub API URL (default: `https://api.github.com`)

#### GitHub Enterprise Server

For GitHub Enterprise Server, include the `server-url` pointing to the API:

```json
"server-url": "https://github.your-company.com/api/v3"
```

---

## GitLab Setup

GitLab integration uses a **Project or Group Access Token** for repository access. Terrapod supports both GitLab.com and self-hosted GitLab instances.

### Step 1: Create an Access Token

#### Group Access Token (recommended for multiple repos)

1. Go to your GitLab group **Settings > Access Tokens**
2. Create a new token:
   - **Name**: `Terrapod`
   - **Expiration**: set an appropriate expiration (or leave blank for no expiry)
   - **Role**: `Reporter` (minimum for read access)
   - **Scopes**: `api` (required for commit statuses and MR comments; `read_api` + `read_repository` is sufficient if you don't need status reporting)
3. Click **Create group access token**
4. Copy the token value -- it will only be shown once

#### Project Access Token (for a single repo)

1. Go to your project **Settings > Access Tokens**
2. Create a new token with the same settings as above
3. Copy the token value

> **This token is not handed to runners by default.** Terrapod uses it for its
> own calls to GitLab -- polling, fetching archives, commit statuses, MR
> comments. It does **not** give it to a runner Job, even when a workspace asks
> for it with a `vcs_connection` [git module credential](module-auth.md), unless
> `api.config.vcs.gitlab.allow_token_delivery_to_runners` is set to `true`.
>
> The reason is that there is nothing to narrow. A GitHub connection is an app
> identity, so Terrapod mints a fresh per-run token scoped to reading contents;
> a GitLab connection *is* this stored token, and GitLab has no call that returns
> a narrower copy of one. Delivering it means delivering it whole, with every
> permission and every project it covers, into a container that is also running
> the workspace's own IaC -- and the connection is named in a variable *value*,
> so any workspace owner can name any connection. See
> [Module Source Auth](module-auth.md#gitlab-the-connections-token-cannot-be-narrowed).

### Step 2: Create a GitLab VCS Connection

No platform-level configuration is needed for GitLab -- the access token is stored (encrypted) on the VCS connection itself.

```zsh
curl -X POST https://terrapod.example.com/api/terrapod/v1/vcs-connections \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vcs-connections",
      "attributes": {
        "name": "my-gitlab",
        "provider": "gitlab",
        "token": "glpat-xxxxxxxxxxxxxxxxxxxx"
      }
    }
  }'
```

The token is stored encrypted at rest (protected by database encryption) and never returned in API responses.

Note the returned connection ID (e.g. `vcs-01234...`).

#### Self-Hosted GitLab

For a self-hosted GitLab instance, include the `server-url`:

```zsh
curl -X POST https://terrapod.example.com/api/terrapod/v1/vcs-connections \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vcs-connections",
      "attributes": {
        "name": "my-gitlab-onprem",
        "provider": "gitlab",
        "server-url": "https://gitlab.your-company.com",
        "token": "glpat-xxxxxxxxxxxxxxxxxxxx"
      }
    }
  }'
```

#### Step 3 (optional): Configure a GitLab webhook for instant triggers

Polling already picks up pushes and merge requests within `poll_interval_seconds` (default 60). To get near-instant (~1s) triggers, add a webhook in GitLab — this is **optional**; everything works on polling alone.

1. Set a webhook secret on the connection (or rely on the global `vcs.gitlab.webhook_secret`). The per-connection secret is write-only and takes precedence; set it via `PATCH` on the connection with a `webhook-secret` attribute.
2. In GitLab, go to the project (or group) **Settings → Webhooks → Add new webhook**:
   - **URL**: `https://terrapod.example.com/api/terrapod/v1/vcs-events/gitlab`
   - **Secret token**: the secret from step 1 (GitLab sends it verbatim in the `X-Gitlab-Token` header — Terrapod compares it timing-safe; unlike GitHub there is no HMAC signature).
   - **Trigger** on **Push events**, **Tag push events**, and **Merge request events**.
3. GitLab's "Test" button (or any real push/MR) should return 200 and trigger an immediate poll.

If the webhook can't reach Terrapod (e.g. the management plane is private), that's fine — polling continues to deliver the same runs. To expose only the webhook path publicly, see [Optional webhook ingress](deployment-webhook-ingress.md).

---

## Linking a Workspace to a Repository

Once you have a VCS connection, create (or update) a workspace with VCS settings. This is the same regardless of whether the connection is GitHub or GitLab.

```zsh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/workspaces \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "name": "my-infra",
        "execution-mode": "agent",
        "auto-apply": false,
        "vcs-repo-url": "https://github.com/my-org/my-infra-repo",
        "vcs-branch": "main",
        "working-directory": "terraform/"
      },
      "relationships": {
        "vcs-connection": {
          "data": {
            "id": "vcs-01234...",
            "type": "vcs-connections"
          }
        }
      }
    }
  }'
```

### Workspace VCS Fields

| Field | Description | Default |
|---|---|---|
| `vcs-repo-url` | Repository URL (HTTPS or SSH format) | (required) |
| `vcs-branch` | Branch to track | Repo's default branch |
| `working-directory` | Subdirectory containing Terraform files | Repository root |
| `trigger-prefixes` | Directories that trigger runs (overrides working directory filtering) | `[]` (uses working directory) |
| `vcs-connection` (relationship) | VCS connection to use for authentication | (required) |

### Trigger Prefixes

By default, when a workspace has a `working-directory` set, only commits that change files within that directory trigger runs. This works well for simple layouts, but breaks down in monorepos where shared modules live outside the working directory.

**`trigger-prefixes`** lets you specify exactly which directories should trigger runs. When set, it **replaces** (not appends to) the default working directory filtering.

**Default behaviour (no trigger prefixes):**
- `working-directory: "environments/dev"` → only changes in `environments/dev/` trigger runs

**With trigger prefixes:**
- `trigger-prefixes: ["environments/dev", "modules"]` → changes in `environments/dev/` OR `modules/` trigger runs

**Example — monorepo with shared modules:**

```
repo/
  environments/
    dev/          ← workspace working directory
      main.tf     ← references ../../modules/vpc
    staging/
  modules/
    vpc/          ← shared module
    rds/
```

```zsh
curl -X PATCH https://terrapod.example.com/api/v2/workspaces/ws-XXXX \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "trigger-prefixes": ["environments/dev", "modules"]
      }
    }
  }'
```

Now commits to `modules/vpc/main.tf` will trigger runs for this workspace.

**Notes:**
- Maximum 20 entries
- Paths are evaluated **relative to the repo root**, not relative to `working-directory`
- Paths are normalized (leading/trailing slashes stripped)
- When `trigger-prefixes` is set, the working directory is NOT automatically included — add it explicitly if needed
- Set to `[]` (empty list) to revert to default working directory filtering

**The decision is cached per pull-request head.** Working out that a pull request touches nothing under the prefixes costs one call to the VCS provider, and the answer cannot change until the pull request is pushed to — so Terrapod records it in Redis (`tp:pr_skip:…`, 30-day TTL) rather than recomputing it on every poll cycle. The key includes the resolved prefixes, so **editing `trigger-prefixes` or `working-directory` re-evaluates every open pull request on the next cycle** — you do not have to wait out the TTL or push a commit to see the effect of a change. If you ever need to force a re-evaluation without changing anything (for instance after force-pushing the tracked branch), delete the key: `DEL tp:pr_skip:{workspace-id}:{pr-number}:{head-sha}:*`, or simply push to the pull request.

**Sparse fetch implication.** `working-directory` and `trigger-prefixes` also narrow the actual git fetch — Terrapod only downloads blobs reachable under those paths. For a workspace tracking `environments/dev` of a 500 MB monorepo, the wire fetch is bounded by the size of `environments/dev/` plus any declared `trigger-prefixes`, not the full repo.

### Supported URL Formats

**GitHub:**
- `https://github.com/org/repo`
- `https://github.com/org/repo.git`
- `git@github.com:org/repo.git`

**GitLab:**
- `https://gitlab.com/group/project`
- `https://gitlab.com/group/subgroup/project`
- `https://gitlab.example.com/group/project.git`
- `git@gitlab.com:group/project.git`

---

## VCS-Driven Run Flow

### Branch Push

```
Developer pushes to "main"
    |
    v (next poll cycle, or immediate if webhook)
Poller calls get_branch_sha("main")
    |
    v (new SHA detected, differs from vcs_last_commit_sha)
Download tarball at new SHA
    |
    v
Create ConfigurationVersion (source="vcs")
    |
    v
Queue Run (plan + apply)
    |
    v
Update workspace.vcs_last_commit_sha
```

### Pull Request / Merge Request

```
Developer opens PR targeting "main"
    |
    v (next poll cycle)
Poller calls list_open_prs(target_branch="main")
    |
    v (new head SHA detected for this PR)
Check deduplication: run exists for (workspace, PR#, head SHA)?
    |
    NO --> Download tarball at head SHA
           Create ConfigurationVersion
           Queue Run (plan-only, speculative)
    |
    YES --> Skip (already have a run for this commit)
```

---

## Push and Verify

1. Push a commit to the tracked branch of your repository
2. Wait up to 60 seconds (or less if you configured webhooks)
3. Check the workspace runs:

```zsh
curl https://terrapod.example.com/api/v2/workspaces/ws-{id}/runs \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

You should see a new run with `"source": "vcs"` and `"vcs-commit-sha"` set to your commit hash.

![VCS-Triggered Runs](images/workspace-runs-populated.png)

---

## Pull Request / Merge Request Speculative Plans

Terrapod automatically creates **speculative (plan-only) runs** for open pull requests (GitHub) or merge requests (GitLab) that target the workspace's tracked branch.

- When a PR/MR is opened or updated with new commits, the poller detects the new head SHA and creates a plan-only run
- Speculative runs show what _would_ change if the PR/MR were merged, but they can never be applied
- A new speculative run is created each time the PR/MR receives a new commit
- Duplicate runs are avoided: if a run already exists for a given PR/MR + commit SHA, no new run is created

You can identify speculative runs in the API response by:
- `"plan-only": true`
- `"vcs-pull-request-number"` is set (e.g. `42`)
- `"message"` starts with "Speculative plan for PR #..."

### Pull requests from forks

A pull request opened **from a fork** gets no speculative plan unless the
workspace opts in. The setting is `allow-fork-pr-plans` and it defaults to
**false** ([GHSA-gp5w-76rw-c452](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-gp5w-76rw-c452)).

A speculative plan executes the pull request author's configuration — provider
blocks, `external` data sources, `local-exec` provisioners — with everything
the run receives: `env`-category variables, sensitive variable values, values
resolved from OpenBao/Vault, the git credentials Terrapod mints for private
module sources, and the Kubernetes Job's cloud workload identity. There is no
smaller credential set to hand it instead: a plan needs those credentials to
refresh state and those variables to evaluate the configuration at all.

Someone opening a pull request from a fork has no write access to the base
repository and cannot merge, so that speculative plan is the only path by
which their code ever runs against the workspace's credentials. That is the
boundary the setting draws.

**Pull requests opened from a branch within the repository itself are
unaffected and always plan.** Their author already has write access and can
get code applied by merging, so gating them would buy almost nothing and would
cost the plan-on-pull-request loop the whole integration exists for — a
reviewer with no plan is being asked to approve blind.

In [`apply_then_merge`](vcs-workflows.md) mode a pull request push creates a
full plan-and-apply-capable run rather than a speculative one, and the gate
covers that too — a fork pull request produces no run of either kind. The
stake there is higher, because in that mode a `terrapod apply` comment applies
the run. That command requires push access to the repository, which a fork
author does not have — but the plan itself is created by the push, before any
comment, so this gate is what stands between a fork branch and the workspace's
credentials.

Turn it on where the trade is worth making: a public module repository taking
community contributions, backed by a workspace that holds nothing worth
taking.

| Where | How |
|---|---|
| Web UI | **Plans on fork pull requests** on the workspace Configuration tab |
| API | `allow-fork-pr-plans` on workspace create and `PATCH` |
| Provider | `allow_fork_pr_plans` on `terrapod_workspace` |
| Autodiscovery | `allow-fork-pr-plans` on the rule, materialised onto every workspace it creates |

Setting it on an [autodiscovery rule](autodiscovery.md) matters more than it
looks. Without it, enabling the setting across a fleet holds only until
autodiscovery creates the next workspace — which presents as the setting not
working rather than as a new workspace correctly defaulting off.

**What counts as a fork.** Terrapod compares the pull request's head
repository with its base one: a different repository on GitHub, a different
source project on GitLab. It deliberately does not read GitHub's
`head.repo.fork` flag, which says the head repository is *itself* a fork of
something — true for a pull request raised inside a fork against that same
fork, which is same-repository and trusted. Anything Terrapod cannot
positively establish as same-repository counts as a fork, including a pull
request whose head repository has since been deleted.

**Module impact runs follow the same rule.** A pull request on a module
repository creates speculative plans on the workspaces that consume that
module (see [Module impact
analysis](registry.md#module-impact-analysis)), each with its own credentials.
A fork pull request reaches only those consuming workspaces that have opted
in — one consumer opting in does not volunteer another consumer's credentials.

### Run VCS Metadata

Runs created by the VCS poller carry metadata:

| Field | Description |
|---|---|
| `vcs-commit-sha` | The commit that triggered the run |
| `vcs-branch` | Branch name (tracked branch for pushes, head ref for PRs/MRs) |
| `vcs-pull-request-number` | PR/MR number (null for branch push runs) |

---

## Commit Status Reporting

Terrapod automatically posts **commit statuses** back to your VCS provider for all VCS-driven runs. This gives you inline feedback on pull requests and branch pushes without leaving GitHub or GitLab.

### How It Works

Whenever a VCS-triggered run changes state (queued, planning, planned, applied, errored, etc.), Terrapod posts a commit status to the VCS provider. For PR/MR runs, Terrapod also posts (or updates) a **comment on the PR/MR** with a summary of the run status and a link to the run page.

- **Commit statuses** appear as status checks on the commit (e.g. the green checkmark or red X on a PR)
- **PR/MR comments** are updated in place -- one comment per workspace per PR, not a new comment on every status change
- Clicking the status link or comment link navigates directly to the run page in Terrapod

### Configuration

Set `external_url` so that commit status links point to your Terrapod UI:

```yaml
api:
  config:
    external_url: "https://terrapod.example.com"
```

Or via environment variable:

```zsh
TERRAPOD_EXTERNAL_URL=https://terrapod.example.com
```

Without `external_url`, commit statuses are still posted but without clickable links.

### Required Permissions

**GitHub App** -- ensure the following repository permissions are enabled:

| Permission | Access |
|---|---|
| **Commit statuses** | Read & write |
| **Pull requests** | Read & write |

**GitLab** -- the access token needs `api` scope (not just `read_api`) to post commit statuses and MR comments.

### Status Mapping

| Run Status | GitHub State | GitLab State | Description |
|---|---|---|---|
| `queued` | `pending` | `pending` | Waiting for runner |
| `planning` | `pending` | `running` | Plan in progress |
| `planned` (plan-only) | `success` | `success` | Plan finished |
| `planned` (full run) | `pending` | `running` | Awaiting confirmation |
| `applying` | `pending` | `running` | Apply in progress |
| `applied` | `success` | `success` | Apply complete |
| `errored` | `failure` | `failed` | Run failed |
| `discarded` | `failure` | `failed` | Plan discarded |
| `canceled` | `error` | `canceled` | Run canceled |

### PR/MR Comment Format

PR comments include a hidden HTML marker so Terrapod can find and update them. Each workspace gets its own comment on a PR -- if multiple workspaces track the same repo, each posts its own comment.

When a PR is updated with a new commit, old speculative runs are automatically **canceled** (the old comment is updated to show the canceled status), and a new run is created for the latest commit.

### Stale PR Run Cancellation

When the VCS poller detects a new commit on an open PR, it cancels any existing non-terminal runs for that workspace + PR number before creating a new speculative run. This keeps PRs clean with only the latest run visible.

---

## Optional: GitHub Webhooks for Faster Feedback

If Terrapod is accessible from GitHub (not behind a firewall), you can add webhooks for near-instant run triggering:

1. Edit your GitHub App settings
2. Check **Active** under Webhook
3. Set **Webhook URL** to: `https://terrapod.example.com/api/terrapod/v1/vcs-events/github`
4. Set a **Webhook secret** (a random string)
5. Subscribe to events:
   - **Push** — accelerates branch push detection
   - **Pull request** — accelerates new PR / new head SHA / PR-closed detection (the `closed` action releases workspace locks in apply-then-merge mode)
   - **Issue comment** — only required for [apply-then-merge](vcs-workflows.md); delivers `terrapod ...` PR commands sub-second
   - **Pull request review** — only required for apply-then-merge; refreshes mergeability after approvals
6. Save

![GitHub App Webhook Settings](images/github-app-webhook.png)

Then set the webhook secret in Terrapod. Create a K8s Secret:

```zsh
kubectl -n terrapod create secret generic terrapod-github-webhook \
  --from-literal=webhook_secret=your-webhook-secret-here
```

Then reference it in Helm values:

```yaml
api:
  config:
    vcs:
      github:
        existingSecret: "terrapod-github-webhook"
        existingSecretKey: "webhook_secret"
```

When a push event arrives, the webhook handler validates the HMAC-SHA256 signature and triggers an immediate poll for the affected repository. The poller still does all the work -- the webhook just makes it faster.

### Per-connection webhook secret

The Helm value above sets a single **global** webhook secret used to validate
every GitHub installation's webhooks. If you connect more than one GitHub
installation, you can instead set a **per-connection** secret so that one
installation's secret can't be used to forge another's webhooks.

Set it when creating or editing a VCS connection — in the admin UI
(*Admin → VCS Connections → Webhook Secret*), via the API as the write-only
`webhook-secret` attribute, or via the Terraform provider's
`terrapod_vcs_connection.webhook_secret`. It is never returned by the API
(`has-webhook-secret` indicates only whether one is set). When a connection
has its own secret, that connection's webhooks are validated against it; when
it doesn't, validation falls back to the global secret — so existing
single-secret deployments are unaffected.

> GitLab webhooks are supported too — see [Configure a GitLab webhook for instant triggers](#step-3-optional-configure-a-gitlab-webhook-for-instant-triggers) above. GitLab connections also work on polling alone if you don't configure a webhook.

---

## Sizing polling for a large estate

The defaults suit most installs and need no attention. This section is for
planning a large estate — many repositories, or a busy monorepo — where it is
worth knowing how Terrapod spends its VCS API budget and which knobs adjust it.

**Terrapod polls economically by design.** Three things keep the cost down:

- **Lookups are deduplicated per cycle.** The cost is per *distinct* repository +
  tracked branch, not per workspace. Twenty workspaces on twenty directories of one
  monorepo cost the same as one workspace, because they share the same branch-head
  and PR lookups.
- **Module repositories poll on their own, longer interval.** Module repos are far
  more numerous than they are active, so they default to 300s while workspace repos
  default to 60s.
- **Webhooks make the interval a floor rather than a wait.** With webhooks
  configured, pushes and PR events arrive immediately and polling becomes the
  safety net for an undelivered event — so a long interval costs nothing in
  responsiveness.

A cycle costs roughly two calls per distinct repository + branch (the branch head,
and the list of open PRs/MRs). A push adds an archive download; autodiscovery adds
a tree listing. So:

```
calls per hour ≈ (3600 / poll_interval_seconds) × 2 × distinct repo+branch pairs
```

You should not have to rely on that arithmetic, though: **Terrapod measures the
real consumption and shows it per connection**, on **Admin → VCS connections**.

### Reading the consumption indicator

Each connection reports a saturation verdict, the rate it is being spent at as a
share of the budget, and when it runs out.

**It deliberately does not lead with "4,991 of 5,000 remaining".** A budget level
cannot tell you whether a configuration is straining the limit, because the budget
refills on a fixed window — right after a reset it reads healthy however fast it is
being spent. A connection consuming 11,400 calls/hour against a 5,000/hour budget
looks completely fine for part of every hour, and then runs stop appearing across
every workspace on it. What answers the question is the rate against the refill.

| Verdict | Meaning |
|---|---|
| **Idle** | No calls observed in the window |
| **Within budget** | Projected spend comfortably fits before the reset |
| **Approaching limit** | Projected spend is most of what is left — worth acting on |
| **Over budget** | Projected spend exceeds what is left; it *will* run out before the reset |
| **Exhausted** | The provider is refusing calls now |

Expanding the breakdown shows the **top consumers** — repositories, workspaces,
modules and policy sets, whichever is actually spending — and the **totals by
label**. Labels are how an estate divides, so the label view is what tells you
where to split when a connection has outgrown one budget.

The reading is an observation taken from the rate-limit headers providers already
return on every response, not a live query, so it costs no additional API calls.
A provider that reports no rate limit at all (a self-hosted GitLab with limiting
switched off) shows **Not reported** rather than a fabricated verdict.

The same numbers are on the API (`GET /api/terrapod/v1/vcs-connections`, see
[API reference](api-reference.md#vcs-connections)), in go-terrapod, and as
Prometheus metrics (see [Monitoring](monitoring.md#vcs-api-budget)).

### The three knobs

**Lengthen the module interval first.** It is the one with the most headroom, and
because both module pollers have webhook accelerators, lengthening it costs nothing
when webhooks are configured:

```yaml
api:
  config:
    vcs:
      # Module repos — new version tags and module-impact PR analysis.
      module_poll_interval_seconds: 900
      # Workspace repos, and VCS-connected policy sets. Without webhooks this
      # decides how long a push waits, so lengthen it once webhooks are in place.
      poll_interval_seconds: 120
```

**Configure webhooks** ([above](#optional-github-webhooks-for-faster-feedback) for
GitHub, and the GitLab equivalent) and the workspace interval becomes a fallback
floor, which is what makes a longer one comfortable.

**Give a busy repository its own VCS connection.** Allowances are per credential —
a GitHub App's is per *installation*, a GitLab token's is per token — so a second
app or token is a second budget. Terrapod supports as many connections as you like
and each workspace names the one it uses, so this needs no new concepts: create a
second app or token, add it as a connection, and repoint some workspaces' `vcs-connection`.

Split **by repository**, not by workspace. Workspaces sharing a repository already
share one deduplicated lookup, so separating *them* across connections increases
total calls; separating repositories divides the work. Putting the busiest monorepo
on its own connection, or module repositories on a different one from workspace
repositories, also keeps one credential's problems local to it.

**Adding one extra connection and attaching everything to it only scales so far** —
it moves the ceiling once and then you are in the same position. The division that
keeps working is one connection **per team**, because that is the boundary along
which an estate actually grows: each team's repositories get their own budget, and
a team that starts polling harder spends its own rather than everyone's. The
label totals in the breakdown are there to make that division a decision you can
read off rather than guess at — the labels are already on the workspaces and
modules, so the rollup shows which slice of the connection each team is using.

---

## Module Registry VCS Publishing

VCS connections are also used for **automatic module publishing** in the private module registry. When a module is connected to a VCS repository, Terrapod watches for new git tags and publishes matching versions automatically.

For full details on setup, tag patterns, and behaviour, see the [VCS-Driven Module Publishing](registry.md#vcs-driven-module-publishing) section in the registry documentation.

---

## Managing VCS Connections

### List Connections

```zsh
curl https://terrapod.example.com/api/terrapod/v1/vcs-connections \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Show a Connection

```zsh
curl https://terrapod.example.com/api/terrapod/v1/vcs-connections/vcs-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Delete a Connection

```zsh
curl -X DELETE https://terrapod.example.com/api/terrapod/v1/vcs-connections/vcs-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

> Deleting a connection does not remove VCS settings from workspaces that reference it. Those workspaces will stop triggering VCS runs (the poller skips workspaces with missing/inactive connections).

---

## Disconnecting VCS from a Workspace

To stop VCS-driven runs for a workspace, clear the VCS connection:

```zsh
curl -X PATCH https://terrapod.example.com/api/v2/workspaces/ws-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "vcs-repo-url": ""
      },
      "relationships": {
        "vcs-connection": {
          "data": null
        }
      }
    }
  }'
```

---

## Security Considerations

### Credential Storage

All VCS credentials are stored in PostgreSQL and protected by database encryption-at-rest (e.g. RDS encryption, Cloud SQL encryption, Azure Database encryption):
- **GitHub**: The App private key (PEM) is stored in the `token` column
- **GitLab**: The access token is stored in the `token` column

Credentials are never returned in API responses.

### Code from a pull request runs with the workspace's credentials

A speculative plan executes the configuration on the pull request branch with
everything the run receives — secrets, resolved variables and the Job's cloud
identity. For a pull request raised within the repository that is the point of
the product; for one raised **from a fork** it hands those credentials to
someone who has neither write access nor the ability to merge, so fork pull
requests do not plan unless the workspace sets `allow-fork-pr-plans`. See
[Pull requests from forks](#pull-requests-from-forks).

### Network Requirements

| Direction | Protocol | Destination | Purpose |
|---|---|---|---|
| Outbound | HTTPS | GitHub API (`api.github.com` or GHE URL) | Branch SHA, PR list, tarball download |
| Outbound | HTTPS | GitLab API (`gitlab.com` or self-hosted URL) | Branch SHA, MR list, tarball download |
| Inbound (optional) | HTTPS | Terrapod API (`/api/terrapod/v1/vcs-events/github`) | GitHub webhooks (faster feedback) |

No inbound connections are required for basic operation. The poller makes outbound HTTPS calls only.

---

## Troubleshooting

### Runs not being created

1. **Check VCS is enabled**: Verify `TERRAPOD_VCS__ENABLED=true` is set and the API logs show "VCS poller started"
2. **Check connection**: Verify the VCS connection exists and has status "active"
3. **Check workspace config**: Ensure `vcs-repo-url` and the `vcs-connection` relationship are both set
4. **Check permissions**: The VCS provider credentials must have read access to the repository
5. **Check logs**: Look for "VCS poll cycle" or error messages in the API server logs
6. **Check database encryption**: Ensure your managed database has encryption-at-rest enabled

### A poll cycle reported a rate limit

Terrapod absorbs short rate-limit bursts on its own: 429s and secondary-rate-limit
403s are retried with backoff, honouring `Retry-After` and falling back to
`X-RateLimit-Reset`. So one appearing on the workspace means the allowance was
still exhausted after those retries, which is a sizing question rather than a
fault.

Check what Terrapod recorded:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$TERRAPOD_URL/api/v2/workspaces/$WS_ID" \
| jq '.data.attributes | {vcs_last_error: ."vcs-last-error", at: ."vcs-last-error-at"}'
```

A rate limit reads as an HTTP **429**, or a **403** whose body mentions a secondary
rate limit. If that is what you see, [Sizing polling for a large
estate](#sizing-polling-for-a-large-estate) has the three knobs — lengthen the
module interval, add webhooks, or give a busy repository its own connection.

Two things that look similar but are not rate limits: a **consistent** 404 on one
repository is permission drift (removed from the App installation, or a narrowed
token scope), and a GitHub installation token is cached for 50 minutes, so a
permission change can take that long to take effect.

### GitHub authentication errors

- Verify the App ID matches the one shown on your GitHub App settings page
- Verify the private key is the correct PEM file for this App (not a different App)
- For GitHub Enterprise Server, ensure `server-url` is set correctly (should end in `/api/v3`)
- Installation tokens are cached for 50 minutes -- if you change permissions, it may take up to 50 minutes to take effect

### GitLab authentication errors

- Verify the access token has `read_api` and `read_repository` scopes
- Verify the token has not expired
- For self-hosted GitLab, ensure the `server-url` is correct and reachable from the Terrapod API server
- Check that the token's role has sufficient access to the target projects

### Webhook signature validation fails (GitHub)

- Ensure the webhook secret configured in Terrapod (`TERRAPOD_VCS__GITHUB__WEBHOOK_SECRET`) exactly matches the one set in the GitHub App settings
- The webhook secret is case-sensitive

### Speculative plans not appearing for PRs/MRs

- **Is the PR/MR from a fork?** Fork pull requests do not plan unless the
  workspace sets `allow-fork-pr-plans` (off by default). The poller logs
  `vcs.pr.fork_plan_skipped` with the workspace id and PR number each time it
  skips one. Pull requests from a branch in the repository itself are never
  affected by this — see [Pull requests from forks](#pull-requests-from-forks)
- The PR/MR must target the workspace's tracked branch (e.g., `main`)
- Check that no run already exists for the same PR/MR number + head SHA (deduplication)
- Verify the VCS connection has permission to list pull requests / merge requests


### Naming a VCS connection is authorized

A VCS connection holds a GitHub App installation or a GitLab access token, and it
reaches **every repository that credential can reach**. Naming one on a workspace is
therefore a grant rather than a reference — and a connection's id is returned to
anyone with `read` on a workspace using it, so the id is discoverable by design.

**Four claims, any one of which is enough.** A caller may name a connection when:

| Claim | How it is granted |
|---|---|
| Platform `admin` | Admins may name any connection. |
| The connection's **owner** | `owner-email` on the connection matches the caller. |
| A **role reaching its labels** | `labels` on the connection, matched by the caller's roles with the same allow/deny evaluation every labelled resource gets — see [RBAC → VCS connections are a labelled resource](rbac.md#vcs-connections-are-a-labelled-resource). |
| Already **owns a workspace using it** | The access is one the caller already holds, so naming it again gains them nothing. |

The last of those is kept from the release that first closed this finding, where
`owner-email` and `labels` did not yet exist. It carried a consequence that those
two attributes now remove: there is **no longer any need for a platform admin to
create the first workspace on a connection**. Set the owner, or label the
connection and point a role at it, and the team can create its own from the start.

The claim is checked wherever a connection is named:

- **workspace create** and **workspace update** — both the `vcs-connection-id`
  attribute and the `vcs-connection` relationship, so neither spelling slips
  past. Update is checked only when the connection actually **changes**, so an
  edit that leaves it alone does not start failing for whoever administers the
  workspace today;
- **registry module** create and update — a module names a connection and a
  repository URL, and the registry poller then clones that repository with that
  connection's credential and publishes it as a module the caller owns;
- **run time**, when a `git_http_auth` credential with `source: vcs_connection`
  is minted. A workspace may always use its own connection; anything else is
  checked against the **workspace owner**, since there is no live caller at run
  time. Two of the four claims do not apply on this path: there are no roles to
  evaluate, so a label claim does not grant here, and nothing is treated as a
  platform admin. A workspace whose claim rests only on labels should name its
  own connection, or hold a `static` credential with a token the operator scoped
  themselves — see [Private module source auth](module-auth.md).

A refusal is a **403** on the API, and on the run-time path the run is **errored with
the reason** rather than run without the credential, so an `init` failure never has to
be traced back to a missing credential.

An operator who needs the previous behaviour — any authenticated user naming any
connection id — can set:

```yaml
api:
  config:
    vcs:
      require_connection_authorization: false
```

Prefer delegating with `owner-email` or `labels` over either turning this off or
granting someone admin. (GHSA-v8g7-pqrj-8mcm)

<a id="restricting-a-connection-to-specific-repositories"></a>

### Restricting a connection to specific repositories

Holding a claim to a connection says nothing about **which** repository it may be
pointed at. The repository URL is an ordinary string on the workspace, so an
entitled caller could point an entitled connection at anything its credential can
read. `allowed-repositories` closes that, and it is the control to reach for when
one GitHub App installation covers an organization broader than the team using it.

**It is empty by default, and empty means any repository the credential can
reach.** Narrowing is opt-in, so upgrading changes nothing until an operator sets
it on a connection.

```zsh
curl -X PATCH "$TERRAPOD/api/terrapod/v1/vcs-connections/vcs-<id>" \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{"data": {"type": "vcs-connections", "attributes": {
        "allowed-repositories": ["platform-team/*", "shared/terraform-modules"]}}}'
```

Patterns are globs, matched against **both** spellings of the target so an
operator can write whichever reads better:

- the repository's `owner/name` path, with any `.git` suffix removed — so
  `platform-team/*` matches `https://github.com/platform-team/service.git` and
  the SSH form of the same repository;
- the full URL as stored — so `https://github.example.com/platform-team/*`
  additionally pins the host.

Three details worth knowing before writing one:

- **`*` crosses `/`.** `platform-team/*` matches a nested subgroup path such as
  `platform-team/infra/sub/service`, which a shell glob would not. Pin the depth
  explicitly if that matters.
- **Patterns are case-sensitive.** `Platform-Team/*` does not match
  `platform-team/service`.
- **A narrowed connection refuses a blank repository URL**, and a blank pattern
  in the list matches nothing rather than everything — so a stray empty string
  cannot quietly turn a restriction into an allow-all.

The allowlist is enforced at eight points, not only where the URL is set:

| Where | Effect when the repository is out of scope |
|---|---|
| Workspace **create** | **403**, naming the repository and the patterns. |
| Workspace **update** | **403**. Re-checked on **every** update that leaves a connection attached, not only when the connection changes — otherwise an entitled owner could repoint an allowlisted connection by editing `vcs-repo-url` alone. |
| `GET /api/terrapod/v1/workspaces/{id}/vcs-refs` | **403**. This endpoint answers "which branches and tags does this repository have" at workspace-**read**, which makes it an existence oracle for private repositories the credential can reach. |
| The **config fetch**, where the source actually arrives | The fetch fails and the run **errors with the reason**. This is the path the poller and run triggers take, where there is no live caller to refuse — so a workspace whose URL was set *before* an operator narrowed the connection stops fetching rather than quietly cloning something out of scope. |
| **Every minted git credential** | The mint is refused. A `git_http_auth` / `git_ssh_auth` variable carries its own URL pattern, so the credential's scope is set on the *variable* rather than on a workspace's `vcs-repo-url` — checking only where a workspace names a repository would leave the connection mintable for anything the pattern covered, including the workspace's own connection. |
| Registry module **create** | **403**. |
| Registry module **update** | **403**. |
| Registry module **VCS update** | **403**. |

That last row is the one to plan for when narrowing an existing connection:
workspaces already pointing outside the new patterns keep their configuration
and start failing their next run. Find them first — see
[the runbook](runbooks.md#a-run-cannot-fetch-its-repository).

**Know what it does not cover.** The registry pollers clone a module's
repository to publish versions and to run module-impact analysis, and **those
fetches are not re-checked** against `allowed-repositories`. A module's repository
URL can only be *set* through a checked path — create, update and VCS-update all
enforce the allowlist — so an entitled caller can no longer point a module at
something out of scope. What remains is a module whose URL predates a narrowing:
it keeps being cloned, where a workspace in exactly that position stops at its
next config fetch. Find those the same way you find the workspaces, and fix or
remove them in the same pass.

Keep the connection's credential itself scoped regardless — a GitHub App installed
on only the repositories it needs, or a project- or group-scoped GitLab token, is
the control that bounds every path at once, and the allowlist is then defence in
depth over an already-narrow credential.

Clearing the list (`"allowed-repositories": []`) restores "any repository the
credential can reach". Sending the attribute is what changes it; omitting it from
a `PATCH` leaves it alone. (GHSA-v8g7-pqrj-8mcm)
