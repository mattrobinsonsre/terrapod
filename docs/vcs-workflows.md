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

### What the status comment says

Terrapod posts a **PR-level status comment** and edits it in place, so the thread stays readable no matter how many times you push. It appears in **both** workflow modes, and it is refreshed as each piece of evidence lands — the resource counts, the gate verdicts and the cost estimate reach the API in three separate uploads, so the comment converges rather than appearing complete at once. It carries one row per affected workspace:

| Workspace | Plan | Cost Δ | Apply | Mergeable |
|---|---|---|---|---|
| [`prod-network`](#) | +3 ~1 -2 | +412 GBP/mo | not applied | yes |
| [`demo-network`](#) | +3 | +11 GBP/mo | will apply on merge | yes |

Each workspace name links to its run. The comment ends with an `*Updated <timestamp>*` line: refreshes are best-effort by design — a failed enqueue is logged rather than failing the run — so the timestamp is how you tell a current row from a stale one.

- **Plan** — the add / change / destroy counts from the plan. A run whose plan did not finish shows its status word (`queued`, `running`, `errored`) instead, and a run with no recorded counts falls back to `changes`.
- **Cost Δ** — the **monthly delta this run introduces**, not the workspace's projected total: what merging costs, positive or negative. A priced plan that changes no spend says `no change`; `—` means the run produced no cost estimate (cost estimation off, or the plan never finished). The same figures are in the run's `cost-estimate` artifact.
- **Apply** / **Mergeable** — where the run stands, and whether the VCS side will let it merge (see *Troubleshooting* for a blocked mergeability check).

Below the table, each workspace with gates that can actually block gets a collapsed block naming every one of them and how it ruled:

```
▸ prod-network — blocked by policy
    🟢 post-plan tasks — run-task, mandatory
    🔴 prod-guardrails — policy, mandatory
    🟢 security scan — security-scan, enforced
    🟢 AI policy gate — ai-policy, mandatory
```

Gates appear in the order the run evaluates them — run tasks, then policy sets, then the security scan, then the [AI policy gate](ai-plan-summary.md#policy-gate) — so the first failing one is the same gate the run's `blocked-by` attribute names. Passing gates are listed too: the comment is an attestation of what was checked, not only an alarm. Advisory policy sets and advisory scans are left out, because they cannot hold a run; read their findings on the run page. A gate that was **overridden** shows as passed, with its name still listed so the override stays visible in the PR.

The AI policy gate is the one that can hold a run **without having ruled**: its verdict is produced after the plan, so a mandatory gate holds the run while the summariser is still ruling, and indefinitely if it never does. That shows as `AI policy gate (awaiting verdict)`, so a run waiting on a verdict that is not coming is visible rather than silent.

A workspace whose mandatory gate failed is **not** offered an apply — `terrapod apply` would be refused while the gate holds the run. Override the gate (or fix the finding and push), and the next comment update offers it.

**How many comments a PR gets.** Where this table is present, the per-workspace comment Terrapod also posts (`### Terrapod — <workspace>`) drops to the one thing the table cannot carry — the AI plan summary — and is not posted at all when there is none. Since [AI plan summaries](ai-plan-summary.md) are off by default, **a default deployment gets exactly one Terrapod comment per PR**; turn them on and each affected workspace adds its narrative alongside the table, once the summary lands.

A run that carries a PR number but has no table keeps the full per-workspace comment, status line and link included — a module-impact run is the case that does this, because its PR number belongs to the module's repository rather than the workspace's. A PR that touches no workspace gets neither comment.

To keep Terrapod off PRs that change nothing it manages, set `trigger_prefixes` (or `working_directory`) on the workspace: a PR touching no matching path never creates a run at all, so it costs no plan and produces no comment.

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

`terrapod help` replies with this table as a new comment, next to the one you
wrote rather than in the status comment further up the thread. An unrecognised
verb gets the same reply, so a typo tells you it was received and misread
rather than leaving you unsure it arrived at all.

Only comments written **after** Terrapod starts tracking a PR are acted on. A
command posted before then — while the App was still missing the Issues
permission, say, or before the workspace was `apply_then_merge` — is not
replayed when tracking begins.

## Command acknowledgement

Every `terrapod ...` comment is reacted to as soon as it is received, so you can
tell a command Terrapod never saw from one it saw and had nothing to do with:

| Reaction | Meaning |
|---|---|
| 👀 | Received. Replaced by one of the below once routed — an 👀 that stays is a command whose outcome is unknown. |
| 👍 | Acted on. For `plan` and `apply` that means queued, not finished — watch the status comment for the outcome. |
| 👎 | Not acted on. A reply on the PR says why. |

A command that is dropped gets a short reply explaining which of the three
reasons applies: Terrapod is not tracking this PR, no apply-then-merge
workspace is affected by it, or the named workspace is not among them. An
unrecognised verb is named back to you (`Terrapod does not recognise
\`plna\``) with the command list, so a typo reads as a typo.

Reactions are best-effort. If the App installation has not accepted the
permission, you get no emoji and everything else — the reply, the status
comment, the commit status — works exactly as before. On GitLab commands
arrive by polling rather than webhook, so the acknowledgement appears within
one poll interval rather than immediately.

## Status comment

One Terrapod-authored comment per workspace **per commit**, edited in place as
that commit's run progresses:

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

**A push gets a new comment rather than a silent edit.** The comment's identity
includes the commit it describes, so a plan triggered by pushing appears at the
foot of the thread next to that push. Within one commit the comment is edited in
place, so `queued → planning → planned → applied` stays a single comment: one
comment per push, not one per status change. Previously a single comment was
edited for the life of the PR, which meant a push-triggered plan produced no
visible change in the thread at all — the edit was often far above the commit
that caused it.

Comments written before the upgrade carry the older identity and are left
where they are; the next status posts one fresh comment.

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

**A commit status that stays "Blocked by …"** — the plan finished and a post-plan gate is holding the run: a mandatory run task, a mandatory policy set, an enforced security scan, or the AI policy gate. The status names which. Override it from the run's matching tab, or discard the run. The check stays unmet meanwhile, so the PR cannot merge past it.

**"No changes — nothing to apply"** — the plan found nothing to do, so no apply was launched. The run still reaches `applied` because it is complete, not because anything was applied.

**Comment didn't trigger anything** — look at the reaction first. **No reaction at all** means the comment never reached the dispatcher: check that it starts with `terrapod` at the beginning of a line (a command inside a code fence is ignored by design), that the GitHub App has Issues permission accepted, and the API pod logs for `vcs_comment_dispatch` events. **👎 with a reply** means it arrived and the reply says why. **👀 that never changes** means dispatch started and did not finish — check the API pod logs. If reactions are absent everywhere but replies still appear, the App has not accepted the permission that lets Terrapod react; that is cosmetic and nothing else is affected.

**Workflow flip rejected** — you can't change `vcs_workflow` while PR runs are in flight on the workspace. Cancel or merge those PR runs first.

## See also

- [Atlantis docs](https://www.runatlantis.io/docs/using-atlantis.html) — the prior art
- [`docs/vcs-integration.md`](vcs-integration.md) — VCS connection setup
- [`docs/rbac.md`](rbac.md) — Terrapod's RBAC (which does *not* gate comment-driven applies)
- [`docs/audit-logging.md`](audit-logging.md) — dual-actor audit model


## `apply_then_merge` and auto-apply are incompatible

A workspace using `apply_then_merge` cannot have auto-apply enabled, in any
mode. The API refuses the pair with a `422` — on workspace create, on PATCH, and
on bulk-update, which checks it against every matched workspace before touching
any of them.

The reason is the ordering the workflow is named for. Under `apply_then_merge`
the apply runs **before** the PR merges, so an auto-apply would apply
infrastructure changes from a branch nobody has approved — which is precisely
what the workflow exists to prevent. Under the default `merge_then_apply` the
review has already happened by the time the apply is queued, so auto-apply is
compatible there.

To move a workspace between them, change one setting at a time: turn auto-apply
off first, then switch the workflow.
