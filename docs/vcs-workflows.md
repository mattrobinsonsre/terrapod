# VCS Workflows

Terrapod offers two workflow modes per workspace for how PR/MR changes drive runs and merges.

## Credit and positioning

[Atlantis](https://www.runatlantis.io/) pioneered the apply-then-merge workflow described below. Almost every concept on this page — comment-driven applies, per-project locks, mergeability gating, automerge — comes from Atlantis, and we are following their model deliberately because it is the right model and the community already understands it.

Terrapod offers apply-then-merge as one workflow inside a broader platform. **Atlantis remains the right tool for many users** — teams who want a focused GitOps-only workflow with no separate platform UI, teams who don't need state management / RBAC / a registry / audit logs, and teams whose mental model is *"my Terraform automation lives in my PR comments, full stop"*. If apply-then-merge from PR comments is *all* you need, Atlantis is likely the better fit and we recommend evaluating it first.

If you also want a UI, state management, label-based RBAC, a private registry, audit logs, and the option to mix this with a more traditional merge-then-apply workflow, that's where Terrapod earns its keep.

## The two modes

| Mode | Run on PR/MR push | Apply runs against | Merge happens | Authorization | Where it shines |
|---|---|---|---|---|---|
| **merge_then_apply** (default — TFE/HCP standard) | speculative plan-only | merged commit on the default branch | before apply | Terrapod RBAC | central control plane; protected default branch is the source of truth |
| **apply_then_merge** (opt-in — Atlantis standard) | full plan-and-apply with saved tfplan | the PR/MR head commit | after a successful apply | **VCS repo permissions + branch protection** | per-PR review of real diff and apply outcome before code lands |

The toggle lives on each workspace (`vcs_workflow`); flipping it is a deliberate operational decision and is rejected while PR runs are in flight on that workspace.

## Authorization model for apply-then-merge — read this carefully

> If you can merge the PR, you can apply it.

In `apply_then_merge` mode, **Terrapod's role-based and label-based RBAC do not apply to comment-driven actions.** Authorization is delegated to your VCS provider:

- Anyone who can comment on the PR can issue `terrapod apply`.
- The apply only proceeds when the PR's mergeability state is clean — branch protection (required reviews, status checks, code owner approval) becomes the gate.
- Audit log entries for comment-driven actions reference the **VCS user id and login** directly; there is no Terrapod identity in the chain.

This is deliberate and matches Atlantis exactly. It sidesteps a brittle mapping between VCS identities and Terrapod identities, and means there's a single source of truth for who can change infra: the repo's branch-protection settings.

**Recommended**: configure branch protection to **require a linear history** (rebase or squash before merge). Apply-then-merge applies against the PR head commit; if the PR is behind the default branch, the apply outcome may diverge from what eventually merges. With required-linear-history, the PR head is what gets merged, so the commit you applied is the commit that lands.

The workspace settings page surfaces this contract as a banner when you switch a workspace into `apply_then_merge`.

## How apply-then-merge runs work

A PR push in `apply_then_merge` mode does **not** trigger a speculative plan-only run. It triggers a **full run that saves the plan file**, then sits in `planned` waiting on a user action. When the user comments `terrapod apply`, the apply phase consumes the exact saved tfplan — so the user reviews and approves the same plan that gets applied, with no re-plan in between.

```
PR opened / pushed
    ↓
full run created (NOT speculative), workspace lock acquired
    ↓
plan phase runs → tfplan saved to storage
    ↓
status comment posted on PR: summary + "Run `terrapod apply` to apply these changes"
    ↓
[run sits in `planned`, workspace remains locked]
    ↓
user comments `terrapod apply`
    ↓
mergeability check (branch protection, required reviews, …)
    ↓
apply phase runs against the saved tfplan
    ↓
if auto_merge: merge PR; if not: comment prompts `terrapod merge`
    ↓
workspace lock released
```

If a user pushes a new commit before apply, the existing run is canceled and a new full run is created — same workspace lock, new plan, new tfplan.

### Lock semantics — this is the tradeoff

While a PR's run sits in `planned`, the **workspace is locked**. A second PR touching the same workspace can't plan until the first PR is merged, discarded, or its run is canceled. The PR comment thread explains the wait.

This matches Atlantis's per-project lock and is the price of "the user reviews the exact plan that gets applied". For workspaces with many concurrent PRs, consider whether `merge_then_apply` (with its speculative plans not holding the lock) is a better fit.

### Stale-plan handling

`tofu apply tfplan` refuses to apply if state drifted between plan and apply (e.g. a sibling PR applied and changed state). Terrapod surfaces that on the PR comment with a prompt to comment `terrapod plan` to refresh. No bespoke staleness logic — we lean on the tool.

## PR comment vocabulary

Commands must start with `terrapod` (or the configured mention prefix — e.g. `@terrapod-bot`) at the beginning of a line. Mid-sentence mentions are ignored.

| Command | Effect |
|---|---|
| `terrapod plan` | Cancel the current run + plan a fresh one against the PR head |
| `terrapod plan -W <workspace>` | Same, scoped to one workspace (monorepo) |
| `terrapod apply` | Apply the current `planned` run for all PR-affected workspaces |
| `terrapod apply -W <workspace>` | Apply a single workspace |
| `terrapod unlock` | Release the workspace lock if stuck |
| `terrapod merge` | Force-merge despite incomplete applies (audit-logged) |
| `terrapod help` | List commands |

Code-fenced blocks don't match — discussing the bot in a code sample never accidentally triggers a command.

## Status comment

One Terrapod-authored comment per PR, edited in place:

```
| Workspace             | Mode             | Plan      | Apply       | Mergeable |
|---|---|---|---|---|
| accounts-alpha-net    | apply_then_merge | + 3 ~ 1   | applied     | yes       |
| accounts-beta-compute | apply_then_merge | + 0 ~ 2   | not applied | yes       |
| shared-network        | merge_then_apply | + 0 ~ 0   | will apply on merge | yes |

Comment `terrapod apply -W accounts-beta-compute` to apply remaining changes.
Auto-merge will fire when all workspaces are applied.
```

The table covers every workspace whose runs reference this PR, regardless of mode. `merge_then_apply` workspaces show "will apply on merge" in the Apply column to make the mode distinction explicit.

## Monorepo behaviour

A PR can touch multiple workspaces. `terrapod apply` (no `-W`) operates on all apply-then-merge workspaces ready to apply, in the order their plans finish. `-W <name>` scopes to one.

**Auto-merge** fires only when every PR-affected workspace meets its per-mode required state:

- `apply_then_merge` → successful applied run for the head SHA (or `has_changes=false`, which auto-counts)
- `merge_then_apply` → speculative plan succeeded

If the user wants to merge despite incomplete applies, `terrapod merge` is the force escape hatch. The per-workspace state at merge time is recorded in the audit log; unapplied workspaces get a banner on their detail page indicating known drift between code and infrastructure.

## Webhook + polling

Hook-and-poll: webhooks accelerate, polling is the source of truth. Every behaviour described here works without webhooks configured — Terrapod's poll cycle (default 60s) handles new commits, new comments, new reviews, and PR-closed events.

If you configure webhooks, the same events arrive in seconds instead of up to one poll interval. Either path produces the same outcome; a Redis dedup key ensures each command is processed exactly once even when webhook and poll race.

## GitHub App permissions

If you're moving an existing Terrapod installation onto apply-then-merge, the GitHub App needs two permission upgrades:

| Permission | Required for |
|---|---|
| **Issues: Read & Write** | Posting and reading PR comments (PR comments use GitHub's Issues API) |
| **Contents: Read & Write** | Performing the auto-merge / `terrapod merge`. GitHub's `PUT /repos/{o}/{r}/pulls/{n}/merge` endpoint creates a commit on the target branch, which requires `contents: write` — verified via the `X-Accepted-GitHub-Permissions` response header. Note that this is *Contents*, not *Pull requests* (which `Read` is sufficient for, since the App reads PR state but doesn't modify it). |

If you only need apply-then-merge without auto-merge or `terrapod merge`, `Contents: Read` is sufficient — the apply phase doesn't touch the merge API.

Webhook event subscriptions:
- `issue_comment` — receive `terrapod ...` commands sub-second
- `pull_request_review` — refresh mergeability after approvals
- `pull_request` (events: `closed`) — release the workspace lock and reconcile PR-session state when a PR is merged or closed without an apply

Existing installations have to accept the permission upgrade once via the GitHub org-admin UI. Until accepted, default-mode workflows are completely unaffected; apply-then-merge is simply unavailable.

## GitLab token scope

Project / Group access tokens need `api` scope (the existing requirement covers the new endpoints). Webhook events: enable `Comments` and `Merge request events`.

## Troubleshooting

**"Apply blocked by mergeability"** — your branch protection rejected the apply. Read the reason on the PR status comment (or the run detail page). Fix on the VCS side (resolve conflicts / get approval / rerun status checks), then comment `terrapod apply` again.

**"PR #X currently holds the lock"** — another PR is mid-flight on the same workspace. Wait for it to merge/discard, or merge/discard it yourself, then push your PR to retrigger the plan.

**Stale plan after a sibling apply** — `terrapod plan` to replan against the new state, then `terrapod apply`.

**Comment didn't trigger anything** — check (a) the comment starts with `terrapod` at the beginning of a line, (b) the verb is one of the supported commands, (c) the workspace is in `apply_then_merge` mode, (d) the GitHub App has Issues permission accepted, (e) `tilt logs` (local) or `kubectl logs` (cluster) on the API pod for `vcs_comment_dispatch` events.

**Workflow flip rejected** — you can't change `vcs_workflow` while PR runs are in flight on the workspace. Cancel or merge those PR runs first.

## See also

- [Atlantis docs](https://www.runatlantis.io/docs/using-atlantis.html) — the prior art
- [`docs/vcs-integration.md`](vcs-integration.md) — VCS connection setup
- [`docs/rbac.md`](rbac.md) — Terrapod's RBAC (which does *not* gate comment-driven applies)
- [`docs/audit-logging.md`](audit-logging.md) — dual-actor audit model
