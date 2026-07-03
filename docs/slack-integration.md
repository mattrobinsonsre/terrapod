# Slack integration

Terrapod integrates with Slack so run notifications land in a channel and —
in later phases — operators can approve or discard runs from the message
itself. This guide gets the integration **connected end to end**.

It is written for the common real-world situation where **two different people
set this up**: a **Slack admin** (often IT, who owns the Slack workspace) and a
**Terrapod operator** (the SRE running the platform). Neither needs access to
the other's system. Each part below says who does it.

> **Phase status.** Today the integration establishes the connection and posts
> a connectivity check to a channel. Interactive approve/discard and the
> `/terrapod` slash command build on this same app and connection — no Slack
> reconfiguration is needed when they land, so setting this up now is not throwaway work.

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
| 2 | Install it and collect the **three tokens** + channel | — |
| 3 | — | Store the three tokens in a Kubernetes Secret |
| 4 | — | Turn the integration on in Helm values and deploy |
| 5 | — | Verify the bot is online and greeted the channel |

The handoff between them is exactly three secret strings and one channel name.

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
      socketMode: true                 # outbound WebSocket; no ingress change
      defaultChannel: "#integration"   # the channel from A3
      existingSecret: terrapod-slack   # the Secret from B1
```

Apply with `helm upgrade` (or your GitOps flow).

### B3. Verify

- In Slack, the **Terrapod** app shows as **online**.
- The channel receives a **connectivity message** from Terrapod shortly after the API pod starts.
- API logs show `slack.socket_mode_connected`.

If all three are true, the connection is live.

---

## Reference

**Helm values** (`api.config.slack`):

| Value | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch for the integration. |
| `socketMode` | `true` | Outbound Socket Mode. `false` → Request-URL mode (needs the webhook ingress). |
| `defaultChannel` | `""` | Channel for the connectivity check / fallback posts. |
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
| Connected, but no channel message | Bot not invited to the channel (`/invite @terrapod`), or `defaultChannel` wrong/empty. |
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
  `chat:write.public`, and `commands`. Interactive approvals (a later phase) are
  authorised against the acting user's **Terrapod** identity and RBAC, not by
  Slack channel membership — so being in the channel never implies permission to
  apply.
