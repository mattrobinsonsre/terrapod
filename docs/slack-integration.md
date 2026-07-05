# Slack integration

Terrapod integrates with Slack so run notifications land in a channel and —
in later phases — operators can approve or discard runs from the message
itself. This guide gets the integration **connected end to end**.

It is written for the common real-world situation where **two different people
set this up**: a **Slack admin** (often IT, who owns the Slack workspace) and a
**Terrapod operator** (the SRE running the platform). Neither needs access to
the other's system. Each part below says who does it.

> **What you get.** An outbound Socket Mode connection, the `/terrapod` account
> linking command, and opt-in per-workspace run notifications with interactive,
> RBAC-checked Approve/Discard — all on one app and connection. Startup does a
> logged connectivity check (`auth.test`); it does **not** post to any channel.

---

## How it connects (why there's nothing to expose)

Terrapod talks to Slack over **Socket Mode**: the Terrapod API opens an
**outbound** WebSocket *to* Slack and receives events over it. Slack never needs
to reach into your network — there is **no public URL, no inbound firewall rule,
no ingress change**. This is the same outbound-only posture as Terrapod's runner
listeners, and it means the integration works even when the Terrapod management
plane is private (VPN/tailnet/IP-restricted).

(An alternative "Request URL" mode, where Slack POSTs to a public endpoint, is
possible via the [split webhook ingress](deployment-webhook-ingress.md) — but
Socket Mode is the default and needs none of that.)

---

## Who does what

| Step | Slack admin (owns the Slack workspace) | Terrapod operator (owns the deployment) |
|---|---|---|
| 1 | Create the Slack app from the manifest | — |
| 2 | Install it and collect the **three tokens** | — |
| 3 | — | Store the three tokens in a Kubernetes Secret |
| 4 | — | Turn the integration on in Helm values and deploy |
| 5 | — | Verify the bot is online (API logs show `slack.bot_authenticated`) |

The handoff between them is exactly three secret strings. (Channels are opt-in
per workspace afterwards — see [Run notifications](#run-notifications-opt-in-per-workspace).)

---

## Part A — Slack admin: create the app and hand over the tokens

You need permission to create/install apps in the target Slack workspace. If
your workspace requires admin approval for apps, you (or a workspace owner) will
approve it in step 2.

### A1. Create the app from Terrapod's manifest

The manifest pins the exact name, scopes, slash command, and Socket Mode
setting so there's nothing to decide.

1. Go to **https://api.slack.com/apps** → **Create New App** → **From an app manifest**.
2. Choose the target workspace.
3. Paste the contents of [`slack-app-manifest.json`](slack-app-manifest.json)
   (JSON tab) — the operator will send you this file, or grab it from the
   Terrapod repo. Review and **Create**.

**Set the app icon** (recommended, so `@terrapod` posts are recognisable): the
manifest can't carry an icon, so upload one in the UI — **Basic Information →
Display Information → App icon** → upload [`images/slack-app-icon.png`](images/slack-app-icon.png)
(the Terrapod mark, 512×512, shipped in the repo) → **Save Changes**.

### A2. Generate the three tokens

All three come from the app you just created (left sidebar of the app's page):

| # | Token | Where | Notes |
|---|---|---|---|
| 1 | **App-Level Token** (`xapp-…`) | **Basic Information → App-Level Tokens → Generate Token and Scopes** | Add the **`connections:write`** scope. This is what Socket Mode dials out with. |
| 2 | **Bot User OAuth Token** (`xoxb-…`) | **OAuth & Permissions → Install to Workspace** → then copy the token | Installing is what mints this. Approve the install if prompted. |
| 3 | **Signing Secret** | **Basic Information → App Credentials → Signing Secret → Show** | Belt-and-braces; used only if you ever switch to Request-URL mode. |

### A3. Add the bot to a channel

In Slack, pick or create the channel Terrapod should post to (e.g. `#integration`)
and run `/invite @terrapod` in it. Note the **channel name** for the operator.

### A4. Hand over

Give the Terrapod operator, over a secure channel (password manager share, not
plain email/chat):

- the three tokens from A2, and
- the channel name from A3.

That's the entire Slack-side job. You do **not** need any Terrapod access.

---

## Part B — Terrapod operator: wire it into Terrapod

You never touch Slack. You take the three tokens + channel and configure Terrapod.

### B1. Store the tokens in a Kubernetes Secret

Secrets are delivered to the API via `secretKeyRef` — they are **never** put in
Helm values or a ConfigMap. Create the Secret in Terrapod's namespace:

```sh
kubectl -n <terrapod-namespace> create secret generic terrapod-slack \
  --from-literal=bot-token='xoxb-…' \
  --from-literal=app-token='xapp-…' \
  --from-literal=signing-secret='…'
```

(The key names `bot-token` / `app-token` / `signing-secret` are the chart
defaults; override them under `api.config.slack.existingSecretKeys` if you must.)

### B2. Turn it on in Helm values

```yaml
api:
  config:
    slack:
      enabled: true
      socket_mode: true                # outbound WebSocket; no ingress change
      existingSecret: terrapod-slack   # the Secret from B1
```

Apply with `helm upgrade` (or your GitOps flow).

### B3. Verify

- In Slack, the **Terrapod** app shows as **online**.
- API logs show `slack.socket_mode_connected` then `slack.bot_authenticated`
  (the connectivity check is logged, not posted to any channel).

If all three are true, the connection is live.

---

## Account linking (`/terrapod`)

Before anyone can act on runs from Slack (approve/discard, later), their Slack
identity must be linked to their Terrapod identity. This is a **one-time,
explicit login** — Slack membership alone never implies Terrapod permission.

From any channel the bot is in:

- **`/terrapod link`** — Terrapod replies (only to you) with a *Connect your
  Terrapod account* button. Click it, log in to Terrapod as you normally would,
  and you land on a **confirmation screen** that names the Slack user + team
  being linked to your Terrapod account. The binding is recorded only when you
  click **Confirm & link** — so opening someone else's link never binds your
  account silently. The link is **single-use and expires in 10 minutes**.
- **`/terrapod status`** — shows whether you're linked, and as whom.
- **`/terrapod unlink`** — removes your binding.

The binding is durable **identity**, not permission: every future Slack action
re-checks your Terrapod RBAC live, so a link never grants standing access, and if
your Terrapod permissions change the next action reflects it immediately. You can
also view/remove your links from the Terrapod web UI. Under the hood the connect
link carries a Terrapod-signed, single-use token, so no one can forge a binding
for someone else's Slack id; and because the browser shows the Slack identity on
an explicit confirm step before binding, a link tricked into someone else's
browser can't silently bind their Terrapod account to an attacker's Slack user.

## Run notifications (opt-in, per workspace)

Run notifications are **opt-in per workspace** — set the workspace's **Slack
channel** (Workspace → Settings → *Slack notifications*, the `slack-channel` API
attribute, or `slack_channel` on the `terrapod_workspace` resource) and that
workspace posts to it. Leave it empty and the workspace stays silent. There is
**no deployment-wide fan-out** — a channel receives traffic solely because
someone pointed a workspace at it. The Slack app must already be a member of the
channel (and needs the `files:write` scope to attach plan output — see
Troubleshooting).

Everything about one run lives in **one thread**, so the channel stays quiet:

| Event | Message |
|---|---|
| **Needs approval** | A run reached `planned` and awaits a manual apply. Posts a **parent** message with interactive **Approve** / **Discard** buttons and the AI review (where enabled); the **plan output** (`.txt`) is attached as a threaded reply. |
| **Approve / Discard clicked** | The parent is edited to drop the buttons and record **who acted** (*Approved by …*). |
| **Applied / Errored** | The result threads **under** the approval message as a reply (so approvers get pinged) — with the AI failure analysis on errors. Auto-applied runs (no approval step) post a single standalone message instead. |
| **Drift detected** | A standalone message with the plan threaded under it. |

Speculative/PR plan-only runs, intermediate states, and drift-with-no-changes are
deliberately suppressed to keep channels quiet. Deep links in every message use
the external users' URL (`external_url`) only — never an internal machine-to-
machine host — and are omitted if `external_url` is unset.

**AI review timing:** where AI plan summaries are enabled, the *needs-approval*
and *errored* messages **wait for the AI review to finish** before posting, so
it's in the message from the first post rather than racing it. The wait is
bounded by the model call's own timeout (the summary always settles), and if the
model is disabled or fails the message still posts — just without the review.

**Delivery is guaranteed.** The approval prompt is never lost to that wait: a
background safety net posts any needs-approval message that the AI step didn't
deliver within a few minutes (e.g. a runner that died mid-plan), without the
review if it genuinely never arrived. So a run that needs approval always
reaches the channel — worst case a couple of minutes later and review-less —
rather than waiting silently.

### Approving from Slack (RBAC)

Clicking **Approve** / **Discard** carries no standing permission. Every click is
authorised live: Terrapod resolves your linked identity → your **current**
Terrapod roles → your capabilities on that workspace, and requires `run:apply`
before it confirms or discards. If you haven't linked, you get an ephemeral nudge
to `/terrapod link`; if you're linked but lack permission on that workspace, an
ephemeral "no permission" — neither touches the run. On success the message is
edited to record who approved, so a button can't be pressed twice.

## Running multiple Terrapod deployments in one Slack workspace

Terrapod is single-org and self-hosted — a company often runs **several
deployments** (per team, per environment). They can all talk to the **same
Slack workspace**, but two Slack-workspace-level resources are singletons and
must be made distinct per deployment:

1. **The slash command.** A Slack workspace treats a slash command as unique, so
   only one app can own `/terrapod`. Give each deployment's Slack app a distinct
   command in its manifest (e.g. `/terrapod-prod`, `/terrapod-staging`) and set
   the matching **`api.config.slack.command`** so Terrapod answers it.
2. **The app identity.** Each deployment installs its own Slack app; give them
   distinct **names** (and icons) in the manifest so `@terrapod-prod` vs
   `@terrapod-staging` are recognisable. Set a short **`api.config.slack.label`**
   (e.g. `prod`) — it's rendered in the footer of every run message and the
   approval, so a **shared channel** clearly attributes each Terrapod.

Everything else already isolates per deployment: each has its own database and
account-link table, its own signing key, and its own outbound Socket Mode
connection — so account linking, RBAC, approvals, and notifications never cross
between deployments. Only the command name and app identity need to be unique.

```yaml
# deployment "prod"
api:
  config:
    slack:
      enabled: true
      command: "/terrapod-prod"   # matches this app's manifest command
      label: "prod"               # shown in every message footer
```

## Reference

**Helm values** (`api.config.slack`):

| Value | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch for the integration. |
| `socket_mode` | `true` | Outbound Socket Mode (the only supported mode today). |
| `command` | `/terrapod` | The slash command this deployment answers — must match the command in its Slack app manifest. Give each deployment sharing one Slack workspace a distinct command (see below). |
| `label` | `""` | Short per-deployment label shown in every Slack message (e.g. `prod`), so a shared channel can tell deployments apart. Empty → omitted. |
| `existingSecret` | `""` | Name of the K8s Secret holding the three tokens. |
| `existingSecretKeys.botToken` / `.appToken` / `.signingSecret` | `bot-token` / `app-token` / `signing-secret` | Keys within that Secret. |

**Secret → env mapping** (set on the API deployment via `secretKeyRef`):

| Secret key | Env var |
|---|---|
| bot-token | `TERRAPOD_SLACK__BOT_TOKEN` |
| app-token | `TERRAPOD_SLACK__APP_TOKEN` |
| signing-secret | `TERRAPOD_SLACK__SIGNING_SECRET` |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot shows offline; no `slack.socket_mode_connected` log | App-level token missing or lacks `connections:write`; `enabled` false; wrong Secret name. |
| `enabled: true` but nothing connects; log shows `slack.disabled_socket_mode_off_unsupported` | `socket_mode` is set to `false`. Socket Mode is the only supported mode — set `socket_mode: true`. |
| Clicked Approve/Discard, the run acted, but the message still shows the buttons | The action succeeded in Terrapod; only the in-place message edit (which drops the buttons / adds *Approved by …*) failed — a transient Slack error. Re-clicking is a no-op (the run has already moved on); check the run in the UI. |
| A workspace's run notifications never appear | The workspace's **Slack channel** is unset (notifications are opt-in per workspace), or the bot isn't in that channel (`/invite @terrapod`). |
| Messages post but the plan `.txt` never attaches | The app is missing the **`files:write`** bot scope. Add it under **OAuth & Permissions → Scopes**, then **Reinstall** the app (scope changes require reinstall). Apps created from the current manifest already include it. |
| `not_in_channel` / `channel_not_found` in logs | Invite the bot to the channel, or use a channel the bot can post to (`chat:write.public` covers public channels). |
| `invalid_auth` in logs | Bot token wrong or revoked — reinstall the app and update the Secret. |
| Log shows tokens missing while `enabled: true` | The Secret isn't wired: check `existingSecret` and that the Secret has all three keys. |

---

## Security notes

- **No inbound exposure.** Socket Mode is outbound-only; nothing about this
  opens a path into your network.
- **Tokens are secrets.** They live in a Kubernetes Secret and reach the pod via
  `secretKeyRef` — never in Helm values, ConfigMaps, or source control. Treat
  the bot and app-level tokens like any credential; rotate them (regenerate in
  the Slack app, update the Secret) if exposed.
- **Least privilege.** The manifest requests only `chat:write`,
  `chat:write.public`, `commands`, and `files:write` (the last for the plan-output
  attachment). Interactive approvals are authorised against the acting user's
  **Terrapod** identity and RBAC, not by Slack channel membership — so being in
  the channel never implies permission to apply.
