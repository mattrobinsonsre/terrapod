# Optional split webhook ingress

By default, every inbound request to Terrapod — UI loads, admin API calls, the entire CLI surface, **and** VCS webhooks + run-task callbacks — reaches the cluster through a single `Ingress`. That works fine when the management plane is allowed to be publicly reachable.

When it isn't — when the management plane should be private (corporate VPN, Tailscale, IP allow-list, ACL) but webhooks still need to land from `github.com`, `gitlab.com`, and run-task services — Terrapod ships an optional **second Ingress** that exposes only the must-be-public-receivable surface. The primary `ingress:` block stays private; the new `webhookIngress:` block is the public hole.

## Surface split

Two endpoints have to accept connections from the public internet:

| Endpoint | Caller | Auth on the request |
|---|---|---|
| `POST /api/terrapod/v1/vcs-events/github` (and future `/gitlab`) | github.com / gitlab.com webhook delivery | HMAC-SHA256 with the connection's `webhook_secret` |
| `PATCH /api/terrapod/v1/task-stage-results/{id}/callback` | external run-task services (policy engines, scanners) | Short-lived HMAC-derived `access_token` bound to the specific result ID |

Everything else stays on the management ingress:
- The whole web UI
- All admin API (`/api/terrapod/v1/*` apart from the two routes above)
- `terraform login` and CLI cloud-block (`/api/v2/*`, `/.well-known/terraform.json`)
- OIDC / SAML callbacks — these run in the **operator's browser**, not server-to-server, so they don't need to be publicly reachable; the operator's browser already has access to the management plane (that's how they hit the login page)
- State uploads, agent-pool joins, listener heartbeats, the whole runner protocol

## Helm values

```yaml
ingress:                    # private/restricted — management plane
  enabled: true
  className: nginx          # or "alb", or restricted-by-VPC ACL, or whatever
  hostname: terrapod.internal.example.com
  tls: true
  annotations: {}           # plus your access-control annotations

webhookIngress:             # OPTIONAL — public-internet inbound surface
  enabled: true
  className: tailscale      # or "nginx" with a public LB, "cloudflare", etc.
  hostname: terrapod-webhooks.example.com
  tls: true
  annotations: {}
  # paths defaults to the full allow-list; trim if you don't run external
  # run tasks:
  # paths:
  #   - /api/terrapod/v1/vcs-events

web:
  enabled: true
api:
  enabled: true
```

When `webhookIngress.enabled` is true, the chart auto-derives `api.config.public_webhook_url` from `webhookIngress.hostname`. That's the base URL Terrapod hands to external services — the **run-task callback URL** the dispatcher builds, and (in a future UX iteration) the **VCS webhook URL** the UI displays to operators for copy-paste into GitHub App settings. Override it explicitly via `api.config.public_webhook_url` if needed.

When `webhookIngress.enabled` is false (default), `public_webhook_url` falls back to `external_url` at the server side — single-ingress deployments behave exactly as before.

## Mechanism-specific recipes

### Tailscale Funnel

[Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel) exposes a tailnet service to the public internet via Tailscale's edge. Combined with the [Tailscale Kubernetes Operator](https://tailscale.com/kb/1236/kubernetes-operator), it's the cleanest "everything private except webhooks" deployment:

```yaml
ingress:
  enabled: true
  className: tailscale       # management plane on the tailnet
  hostname: terrapod.example.ts.net
  tls: false                 # operator provisions TLS

webhookIngress:
  enabled: true
  className: tailscale
  hostname: terrapod-webhooks.example.ts.net
  tls: false
  annotations:
    tailscale.com/funnel: "true"     # this hostname gets the public Funnel cert
  paths:
    - /api/terrapod/v1/vcs-events     # trim out task-stage-results if unused
```

Prerequisites (operator-side, not chart-managed):
- Tailscale Kubernetes Operator installed and watching `IngressClass: tailscale`.
- Funnel enabled at the tailnet level (Admin Console → Settings → Funnel).
- ACL allows the operator's tag to advertise Funnel.

Note: Funnel only listens on ports 443, 8443, 10000; only allows HTTPS; and has an undocumented bandwidth cap. None of those constraints bite the webhook surface (kilobyte payloads), but if you ever want to run state uploads through the same hostname, switch back to a single ingress on a regular LB.

### Public ALB + WAF

For AWS deployments where the management plane is private but a webhook surface needs a public ALB with WAF protection:

```yaml
ingress:
  enabled: true
  className: alb
  hostname: terrapod.internal.example.com
  annotations:
    alb.ingress.kubernetes.io/scheme: internal
    alb.ingress.kubernetes.io/load-balancer-attributes: "..."

webhookIngress:
  enabled: true
  className: alb
  hostname: terrapod-webhooks.example.com
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/wafv2-acl-arn: "arn:aws:wafv2:..."
```

### Cloudflare Tunnel

Same shape — `className: nginx` plus a Cloudflare Tunnel running as a sidecar that fronts the public hostname.

## What if I configure both ingresses with the same hostname?

You can. The two Ingresses then become two `rules:` entries on the same name, and your Ingress controller has to merge them. For most controllers that "works" but it's not a useful split — set one of them with a `paths:` list (which the chart already does for `webhookIngress`) so the controller has clear path-priority semantics. Easier and clearer to use two distinct hostnames.

## What if I don't configure webhookIngress at all?

That's the default. All traffic, including webhooks, goes through `ingress`. If the management plane is itself public, that's fine. If it isn't, GitHub webhooks won't reach you — fall back to Terrapod's polling-first VCS integration (the poller runs every 60s by default and is the source of truth either way; webhooks are an accelerator on top of it).

## See also

- `helm/terrapod/values.yaml` — full `webhookIngress:` block schema.
- `services/terrapod/api/routers/vcs_events.py` — GitHub webhook receiver (HMAC validated).
- `services/terrapod/api/routers/run_tasks.py` — run-task callback receiver (token validated).
- `services/terrapod/services/run_task_dispatcher.py:120` — the server-side site that hands out the callback URL using `public_webhook_url`.
