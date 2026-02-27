# VCS Integration

Terrapod integrates with **GitHub** and **GitLab** to automatically trigger runs when you push commits or open pull requests / merge requests against a linked repository.

## How It Works

VCS integration has two layers:

1. **VCS connections** (platform-level) -- admin-created resources that configure authentication for a VCS provider. A GitHub connection uses a GitHub App installation; a GitLab connection uses an access token.
2. **Workspace linking** -- each workspace can reference a VCS connection and a repository URL. The workspace tracks a branch (e.g. `main`) and the poller creates runs when changes are detected.

Terrapod's background poller checks your VCS providers every 60 seconds (configurable) and creates two kinds of runs:

- **Branch push -> full plan/apply run** -- when a new commit lands on the tracked branch, Terrapod downloads the code and queues a normal run (plan, then apply if auto-apply is on or manually confirmed).
- **Pull request / merge request -> speculative plan** -- when an open PR/MR targets the tracked branch and has a new head commit, Terrapod queues a plan-only run. The plan shows what _would_ change if the PR were merged, but it can never be applied. A new speculative run is created each time the PR/MR is updated with a new commit.

### Polling-First Design

- **No inbound connections required** -- Terrapod only makes outbound HTTPS calls to VCS provider APIs, so it works behind firewalls and NATs without any ingress configuration.
- **Webhooks are optional** (GitHub only, currently) -- if you want faster feedback (sub-second instead of up to 60s), you can configure GitHub webhooks. The webhook tells the poller to check immediately rather than waiting for the next cycle.

## Prerequisites

- A running Terrapod instance with API access
- Admin access to Terrapod (for creating VCS connections)
- **For GitHub**: a GitHub account or organization where you can create GitHub Apps
- **For GitLab**: a Project or Group Access Token with `read_api` and `read_repository` scopes

## Enabling VCS

Set the following on the Terrapod API server:

```sh
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
   - For an organization: `https://github.com/organizations/{org}/settings/apps/new`
   - For a personal account: `https://github.com/settings/apps/new`

2. Fill in the form:

   | Field | Value |
   |---|---|
   | **App name** | `Terrapod` (must be globally unique -- add your org name if needed) |
   | **Homepage URL** | Your Terrapod URL (e.g. `https://terrapod.example.com`) |
   | **Webhook** | **Uncheck "Active"** (you can enable this later if you want faster feedback) |

3. Set **Repository permissions**:

   | Permission | Access |
   |---|---|
   | **Contents** | Read-only |
   | **Metadata** | Read-only (auto-selected) |

   > **Checks** (Read & write) and **Pull requests** (Read & write) are needed later for PR status reporting. You can add them now or later.

4. Under **Where can this GitHub App be installed?**, choose based on your needs:
   - **Only on this account** -- if all repos are in one org/account
   - **Any account** -- if repos span multiple orgs

5. Click **Create GitHub App**

6. Note the **App ID** shown on the app settings page

7. Scroll down to **Private keys** and click **Generate a private key**. A `.pem` file will download -- keep this safe.

### Step 2: Install the GitHub App

1. From your GitHub App's settings page, click **Install App** in the left sidebar
2. Choose the account/organization where your Terraform repos live
3. Select **All repositories** or **Only select repositories** (pick the repos you want Terrapod to access)
4. Click **Install**
5. Note the **Installation ID** from the URL: `https://github.com/settings/installations/{installation_id}`

### Step 3: Create a GitHub VCS Connection

No platform-level GitHub configuration is needed beyond enabling VCS. The App ID, private key, and installation ID are all stored on the VCS connection itself (encrypted at rest).

```sh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/vcs-connections \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vcs-connections",
      "attributes": {
        "name": "my-github",
        "provider": "github",
        "github-app-id": 12345,
        "github-installation-id": 112887490,
        "github-account-login": "my-org",
        "github-account-type": "Organization",
        "private-key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
      }
    }
  }'
```

The private key is Fernet-encrypted at rest and never returned in API responses.

Note the returned connection ID (e.g. `vcs-01234...`).

#### GitHub Enterprise Server

For GitHub Enterprise Server, include the `server-url` pointing to the API:

```sh
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
   - **Scopes**: `read_api`, `read_repository`
3. Click **Create group access token**
4. Copy the token value -- it will only be shown once

#### Project Access Token (for a single repo)

1. Go to your project **Settings > Access Tokens**
2. Create a new token with the same settings as above
3. Copy the token value

### Step 2: Create a GitLab VCS Connection

No platform-level configuration is needed for GitLab -- the access token is stored (encrypted) on the VCS connection itself.

```sh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/vcs-connections \
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

The token is Fernet-encrypted at rest and never returned in API responses.

Note the returned connection ID (e.g. `vcs-01234...`).

#### Self-Hosted GitLab

For a self-hosted GitLab instance, include the `server-url`:

```sh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/vcs-connections \
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

---

## Linking a Workspace to a Repository

Once you have a VCS connection, create (or update) a workspace with VCS settings. This is the same regardless of whether the connection is GitHub or GitLab.

```sh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/workspaces \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "name": "my-infra",
        "execution-mode": "remote",
        "auto-apply": false,
        "vcs-repo-url": "https://github.com/my-org/my-infra-repo",
        "vcs-branch": "main",
        "vcs-working-directory": "terraform/"
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
| `vcs-working-directory` | Subdirectory containing Terraform files | Repository root |
| `vcs-connection` (relationship) | VCS connection to use for authentication | (required) |

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

## Push and Verify

1. Push a commit to the tracked branch of your repository
2. Wait up to 60 seconds (or less if you configured webhooks)
3. Check the workspace runs:

```sh
curl https://terrapod.example.com/api/v2/workspaces/ws-{id}/runs \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

You should see a new run with `"source": "vcs"` and `"vcs-commit-sha"` set to your commit hash.

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

## Optional: GitHub Webhooks for Faster Feedback

If Terrapod is accessible from GitHub (not behind a firewall), you can add webhooks for near-instant run triggering:

1. Edit your GitHub App settings
2. Check **Active** under Webhook
3. Set **Webhook URL** to: `https://terrapod.example.com/api/v2/vcs-events/github`
4. Set a **Webhook secret** (a random string)
5. Subscribe to events: **Push**, **Pull request**
6. Save

Then set the webhook secret in Terrapod:

```sh
TERRAPOD_VCS__GITHUB__WEBHOOK_SECRET=your-webhook-secret-here
```

When a push event arrives, the webhook handler validates the HMAC-SHA256 signature and triggers an immediate poll for the affected repository. The poller still does all the work -- the webhook just makes it faster.

> GitLab webhook support is not yet implemented. GitLab connections use polling only.

## Managing VCS Connections

### List Connections

```sh
curl https://terrapod.example.com/api/v2/organizations/default/vcs-connections \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Show a Connection

```sh
curl https://terrapod.example.com/api/v2/vcs-connections/vcs-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Delete a Connection

```sh
curl -X DELETE https://terrapod.example.com/api/v2/vcs-connections/vcs-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

> Deleting a connection does not remove VCS settings from workspaces that reference it. Those workspaces will stop triggering VCS runs (the poller skips workspaces with missing/inactive connections).

## Disconnecting VCS from a Workspace

To stop VCS-driven runs for a workspace, clear the VCS connection:

```sh
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

## Troubleshooting

### Runs not being created

1. **Check VCS is enabled**: Verify `TERRAPOD_VCS__ENABLED=true` is set and the API logs show "VCS poller started"
2. **Check connection**: Verify the VCS connection exists and has status "active"
3. **Check workspace config**: Ensure `vcs-repo-url` and the `vcs-connection` relationship are both set
4. **Check permissions**: The VCS provider credentials must have read access to the repository
5. **Check logs**: Look for "VCS poll cycle" or error messages in the API server logs

### GitHub authentication errors

- Verify the App ID matches the one shown on your GitHub App settings page
- Verify the private key is the correct PEM file for this App (not a different App)
- For GitHub Enterprise Server, ensure `api_url` is set correctly (should end in `/api/v3`)
- Installation tokens are cached for 50 minutes -- if you change permissions, it may take up to 50 minutes to take effect

### GitLab authentication errors

- Verify the access token has `read_api` and `read_repository` scopes
- Verify the token has not expired
- For self-hosted GitLab, ensure the `server-url` is correct and reachable from the Terrapod API server
- Check that the token's role has sufficient access to the target projects

### Webhook signature validation fails (GitHub)

- Ensure the webhook secret configured in Terrapod (`TERRAPOD_VCS__GITHUB__WEBHOOK_SECRET`) exactly matches the one set in the GitHub App settings
- The webhook secret is case-sensitive
