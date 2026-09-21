# Rate limiting and client attribution

Terrapod's rate limiter buckets requests per caller. Working out *which* caller
made a request is the whole problem, because the API never sees the browser: by
architectural rule every request arrives through the Next.js BFF, so the socket
peer is always a BFF pod.

Client attribution therefore comes from `X-Forwarded-For`, and that header is
only trustworthy if two things hold.

## 1. Terrapod must trust the peer

`api.config.rate_limit.trusted_proxy_cidrs` lists the peers whose
`X-Forwarded-For` may be believed. It defaults to the private ranges plus CGNAT:

```yaml
api:
  config:
    rate_limit:
      trusted_proxy_cidrs:
        - 10.0.0.0/8
        - 172.16.0.0/12
        - 192.168.0.0/16
        - 100.64.0.0/10   # Tailscale and similar overlays
        - fd00::/8
```

The peer is always an in-cluster BFF pod, so this trusts Terrapod's own
component and nothing publicly routable. Narrow it to your pod CIDR if you
prefer.

When the peer is trusted, the client is the **right-most** entry in the header
that is not itself a listed proxy. When it is not trusted, the header is ignored
entirely and the peer is used.

Setting this to `[]` is supported but has a consequence worth understanding:
every unauthenticated caller then shares one bucket, because they share one BFF
pod — and an anonymous stranger can exhaust the login budget for everybody.

## 2. Your ingress must sanitise the header

Terrapod relies on the ingress either **overwriting** `X-Forwarded-For` with the
real client address, or **appending** the real client address to whatever
arrived. Either way the right-most entry is the truth. It does NOT work if the
ingress passes a client-supplied header through untouched — then the client
chooses its own entry, and its own rate-limit bucket.

The two common ingresses are safe by default:

| Ingress | Default | Effect |
|---|---|---|
| ingress-nginx | `use-forwarded-headers: "false"` | ignores incoming `X-Forwarded-*` and fills them from the observed connection |
| Traefik | appends the client's remote address | the client can prepend, but Traefik's entry is last |

**Configurations that break the assumption:**

- **ingress-nginx with `use-forwarded-headers: "true"`.** Operators enable this
  to see real client IPs behind a CDN or external load balancer. NGINX then
  passes the incoming header through, so a direct caller controls it entirely.
  If you need this, put the CDN's egress ranges in `trusted_proxy_cidrs` as well
  — the scan skips trusted entries, so the first untrusted one from the right is
  still the client.
- **Traefik with `forwardedHeaders.insecure: true`**, which trusts forwarded
  headers from all sources. Traefik's own documentation recommends it for tests
  only.
- Traefik's behaviour for an untrusted source is not stated explicitly in its
  documentation; if you depend on it, verify against your own deployment rather
  than on this table.

## Verifying it

The audit log records the attributed address. Make a request through your real
ingress and check what was recorded:

```sql
SELECT actor_ip, action, resource_type, timestamp
FROM audit_logs ORDER BY timestamp DESC LIMIT 5;
```

If `actor_ip` is a pod address rather than your client address, attribution is
not working and every unauthenticated caller is sharing a bucket.

Then confirm the header cannot be forged — from a machine that reaches the
ingress, send a bogus entry and check it is not what gets recorded:

```sh
curl -H 'X-Forwarded-For: 203.0.113.99' https://terrapod.example.com/api/v1/auth/providers
```

A well-configured ingress records your real address, not `203.0.113.99`.

## Why this matters beyond fairness

The per-credential bucketing that protects live log streaming (#1075) has a
ceiling on how many distinct credentials one source may present per minute, and
that ceiling is keyed on the attributed address. So attribution is not only
about fairness between users — it is what bounds credential churn, and what
keeps the login limit meaningful.
