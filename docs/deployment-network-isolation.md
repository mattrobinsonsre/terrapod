# Split-networking deployments

By default, every Terrapod consumer — a human's browser, the `terraform` CLI cloud block, listener pods in remote clusters, runner Jobs uploading state — reaches the API through a single `Ingress`. That works fine when the management plane is freely reachable from every network the consumers live in.

It often isn't. Common shapes where it isn't:

- Management plane lives on a **VPN-only or tailnet-only hostname** that operators can reach interactively, but pods in remote production clusters cannot resolve or route to.
- Management plane sits behind a **public LB with an IP allow-list** that excludes Kubernetes node CIDRs.
- Operators reach Terrapod via **Cloudflare/Tailscale Funnel** but agent pods are inside private VPCs.

The fix isn't to give up on the private management plane — it's to add a **second** internal-only entry point that listener and runner pods can reach over the private network fabric (transit gateway, peered VPC, internal LB), while the management plane stays restricted. That's what the chart's `internalIngress:` block does.

This is independent of the [webhook split](deployment-webhook-ingress.md). They compose: a deployment can have all three Ingresses, two of them, or just the primary.

## Surface split — who needs to reach what?

| Audience | Reaches | Auth | Chart block |
|---|---|---|---|
| Humans (browser, CLI) | UI, admin API, CLI cloud-block, SSO redirect | Session cookie / Bearer API token | `ingress` (primary) |
| GitHub / GitLab / external run-task services | `/vcs-events`, `/task-stage-results/.../callback` | HMAC-signed payload | `webhookIngress` (optional) |
| Listener pods + runner Jobs from other clusters | `/agent-pools/join`, `/listeners/.../events`, `/runs/.../artifacts/...`, registry downloads | X.509 listener cert / runner token | `internalIngress` (optional) |

All three terminate at the same `<fullname>-web` Service — the Next.js BFF. The difference is **which network the request entered through**, not what API it talks to. Listener auth (bearer cert), runner-token auth, and TFE-V2 session auth all work uniformly regardless of Ingress.

## Helm values

```yaml
ingress:                    # primary — humans + CLI + SSO redirect
  enabled: true
  className: tailscale      # or "nginx" with VPN-restricted LB, etc.
  hostname: terrapod.example.com
  tls: true

webhookIngress:             # optional — public inbound (VCS webhooks)
  enabled: true
  className: nginx-public   # or "alb", "cloudflare", "tailscale" with funnel
  hostname: terrapod-webhooks.example.com
  tls: true

internalIngress:            # optional — internal-only (listener + runner)
  enabled: true
  className: traefik-internal  # or internal-LB-class, "alb" with scheme=internal, etc.
  hostname: terrapod-internal.example.com
  tls: true
  # paths defaults to ["/"] — the internal route is trusted, no allow-list needed
```

The chart validates each block:
- `*.hostname` must be set when `*.enabled: true`.
- `web.enabled: true` is required (every Ingress routes to the web BFF).
- `webhookIngress.paths` must be non-empty when `webhookIngress.enabled` (default ships a path allow-list).
- `internalIngress.paths` must be non-empty (default is `["/"]`).

## Listener `apiUrl` + `publicApiUrl` — the runner-side asymmetry

The listener Deployment carries two URL env vars:

| Env var | Helm value | Meaning |
|---|---|---|
| `TERRAPOD_API_URL` | `listener.apiUrl` | The URL the listener (and the runners it spawns) **actually calls**. In a split-networking deployment, this is the `internalIngress` hostname. |
| `TERRAPOD_PUBLIC_API_URL` | `listener.publicApiUrl` (default: `api.config.external_url`, or empty if both are unset) | The **public/canonical** hostname users see in their browsers, in the CLI cloud block, and in `source = "..."` registry URLs in user `.tf` code. Empty means "no redirect needed" — the listener still works, runner Jobs just don't get a `host{}` block. |

When the two values differ, the listener forwards `TP_PUBLIC_API_URL` to each runner Job pod's env. The runner entrypoint detects the mismatch and appends a terraform CLI `host{}` block to `TF_CLI_CONFIG_FILE`:

```hcl
credentials "terrapod.example.com" {
  token = "<runner token>"
}
host "terrapod.example.com" {
  services = {
    "modules.v1"   = "https://terrapod-internal.example.com/api/v2/registry/modules/"
    "providers.v1" = "https://terrapod-internal.example.com/v1/providers/"
  }
}
```

That's terraform's [remote service discovery](https://developer.hashicorp.com/terraform/internals/remote-service-discovery) mechanism, overridden via a CLI `host{}` block. It means:

- User code can refer to `source = "terrapod.example.com/myorg/aws-vpc/aws"` — the **canonical** hostname they'd use from their laptop.
- Humans on the network that can reach `terrapod.example.com` natively (e.g. on tailnet) hit it directly. No CLI config tweaks required.
- Runners in remote clusters resolve the canonical hostname **as a label** but terraform routes the actual HTTPS requests to the internal URL the runner can actually reach.

When the two URLs are the same (single-network deployments), no redirect is generated — `TP_PUBLIC_API_URL` is silently omitted from the Job spec.

## Pattern recipes

### A. Tailscale management, internal LB for agents, Funnel for webhooks

```yaml
ingress:
  enabled: true
  className: tailscale
  hostname: terrapod.example.com         # tailnet-only
  tls: true

internalIngress:
  enabled: true
  className: traefik-internal            # cluster-wide internal Traefik, TG-routed
  hostname: terrapod-internal.example.com
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: lets-encrypt-prod
    external-dns.alpha.kubernetes.io/hostname: terrapod-internal.example.com

webhookIngress:
  enabled: true
  className: tailscale                   # Tailscale Funnel
  hostname: terrapod-webhooks.example.com
  tls: true                              # Tailscale Funnel terminates TLS at the edge;
                                          # `tls: true` here just lets the chart render
                                          # the spec's TLS block — the operator-managed
                                          # Tailscale layer handles the cert.
  annotations:
    tailscale.com/funnel: "true"

api:
  config:
    external_url: https://terrapod.example.com   # canonical, used for self-referential URLs

listener:
  apiUrl: https://terrapod-internal.example.com  # what listener pods call
  # publicApiUrl defaults to api.config.external_url — no need to set
```

Runners receive `TP_API_URL=https://terrapod-internal.example.com` and `TP_PUBLIC_API_URL=https://terrapod.example.com`, auto-generate the host redirect.

### B. Internal LB for everyone (no public surface)

```yaml
ingress:
  enabled: true
  className: internal-nginx
  hostname: terrapod.internal.example.com
  tls: true

# No webhookIngress — no public webhook source. (E.g. VCS poller is the only
# integration path, or webhooks come via a private network connector.)
# No internalIngress — listener.apiUrl can just use the primary ingress.

api:
  config:
    external_url: https://terrapod.internal.example.com

listener:
  apiUrl: https://terrapod.internal.example.com
  # publicApiUrl == apiUrl == external_url — runner sees no asymmetry, no host{} redirect.
```

### C. Single public hostname (smallest deployment)

```yaml
ingress:
  enabled: true
  className: nginx
  hostname: terrapod.example.com
  tls: true

api:
  config:
    external_url: https://terrapod.example.com

listener:
  apiUrl: ""                  # defaults to in-cluster Service (same-release agents)
  # publicApiUrl unset       # nothing for the runner to redirect
```

## Operational checks

After enabling `internalIngress`:

1. **Resource provisioned**: `kubectl get ingress -n terrapod` shows three Ingress objects (or two, if webhookIngress is off).
2. **TLS cert issued**: if using cert-manager, `kubectl get certificate -n terrapod terrapod-internal-tls` shows `Ready=True` within ~30 s of the Ingress being created.
3. **DNS record published**: if using external-dns, the configured private zone should have an A/AAAA record matching `internalIngress.hostname`.
4. **Reachable from agent clusters**: `kubectl run -n default --rm -it --restart=Never --image=curlimages/curl debug -- curl -sS https://terrapod-internal.example.com/.well-known/terraform.json` from an agent cluster should return the service-discovery JSON document. This endpoint is unauthenticated and goes through the BFF the same way listener and runner traffic does, so a 200 here proves both the Ingress routing and BFF→API proxy work.
5. **Listener pods using the internal URL**: `kubectl get deploy -n terrapod -l app.kubernetes.io/component=listener -o jsonpath='{.items[0].spec.template.spec.containers[0].env[?(@.name=="TERRAPOD_API_URL")].value}'` matches the internal hostname.
6. **Runner Job spec carries `TP_PUBLIC_API_URL`**: trigger a plan, then `kubectl get job -n terrapod-runners <job> -o yaml | grep TP_PUBLIC_API_URL` should show the canonical URL.
7. **Runner `terraform.rc` includes the host block**: `kubectl logs -n terrapod-runners <pod> | grep "Configured host{} redirect"` confirms the entrypoint wrote it.

## What this is not

- **Not a multi-tenant networking model.** Terrapod is single-organisation by design. The three Ingresses serve different *audiences* of the same Terrapod instance, not different tenants.
- **Not authentication.** Listener cert and runner-token auth apply on every endpoint regardless of which Ingress the request entered through. An attacker landing on the internal Ingress without a valid cert/token gets the same 401 as on the public one.
- **Not a substitute for in-cluster reachability** for same-cluster deployments. Where the API runs in the same cluster as the listener, `listener.apiUrl: ""` (the chart's in-cluster Service default) is the right answer and no Ingress hop is needed.
