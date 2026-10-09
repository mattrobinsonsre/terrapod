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

## Clients that share a range with the proxy

The rule above — the right-most entry that is not itself a trusted proxy —
assumes proxies and clients are different machines on different networks. Where
they are not, the client's own entry is skipped as infrastructure and the scan
falls through to the peer, which in Terrapod is always the BFF pod. Every such
client then shares one bucket.

The default list makes this likely rather than exotic, because it is deliberately
broad:

| Your clients reach the API from | Attributed correctly with the default list? |
|---|---|
| The public internet | Yes |
| A Tailscale tailnet (`100.64.0.0/10`) | **No** — collapses to one bucket |
| A corporate VPN on `10.x` / `172.16.x` / `192.168.x` | **No** — collapses to one bucket |

**The remedy is to narrow `trusted_proxy_cidrs` to the pod network your BFF
actually runs on**, which is the only thing that genuinely needs trusting:

```yaml
api:
  config:
    rate_limit:
      trusted_proxy_cidrs: ["10.42.0.0/16"]   # your cluster's pod CIDR
```

This cannot be the shipped default because the pod network differs per cluster,
and some — EKS with custom CNI networking, for instance — place pods inside
`100.64.0.0/10` themselves, so simply dropping the CGNAT entry would collapse
attribution for those deployments instead.

## Verifying it

**Not from the audit log.** `audit_logs.actor_ip` records the socket peer
(`request.client.host`) and never reads `X-Forwarded-For`, so through the BFF it
is always a pod address — whether attribution is working or not. An earlier
version of this page suggested checking it, which could not distinguish the two
cases and would have read as a permanent failure.

Verify by behaviour instead, from two clients that reach the ingress from
different addresses. Exhaust the unauthenticated limit from the first:

```sh
for i in $(seq 1 120); do
  curl -so /dev/null -w '%{http_code} ' https://terrapod.example.com/api/terrapod/v1/auth/providers
done
```

Then make a single request from the second. If it succeeds, the two are in
different buckets and attribution is working. If it is also `429`, they are
sharing one — check the table above before anything else.

To confirm the header cannot be forged, send a bogus entry from a machine whose
address is already attributed correctly and repeat the test. If forging it moved
you into a different bucket, your peer is trusted when it should not be.

## The OIDC issuer paths have their own bucket

The two [cloud identity](cloud-identity.md) issuer documents —
`/.well-known/openid-configuration` and `/.well-known/jwks.json` — are limited in a
**dedicated `api_oidc_issuer` bucket**, at the authenticated limit
(`rate_limit.authenticated_requests_per_minute`, not the lower unauthenticated one), even
though both are anonymous by design. They are not exempt, and they do not share a bucket with
anything else.

The isolation matters in both directions, which is why it is not simply an exemption:

- **Nothing else can starve them.** A `429` on the JWKS does not degrade one caller — it
  breaks token verification for *every* federated run, at every cloud, until the limit
  window rolls. Sharing the anonymous per-IP bucket would let unrelated anonymous traffic
  through the same ingress do exactly that.
- **They cannot starve anything else.** A machine polling the JWKS harder than it should
  consumes its own bucket and nobody else's.

Legitimate volume here is very low — AWS and GCP both cache a JWKS for hours, and the
responses carry a derived `Cache-Control` (half `key_propagation_seconds`) which is the real
defence. The limit is a backstop for a client that ignores it, generous enough never to be
reached in normal use.

**So if a cloud is getting `429` on the JWKS, the bucket is not the usual suspect** — check
attribution above, and whether something is fetching it in a loop. Note also that these two
paths are served through the public `webhookIngress`, so they are attributed like any other
request arriving there.

## Why this matters beyond fairness

The per-credential bucketing that protects live log streaming (#1075) has a
ceiling on how many distinct credentials one source may present per minute, and
that ceiling is keyed on the attributed address. So attribution is not only
about fairness between users — it is what bounds credential churn, and what
keeps the login limit meaningful.
