# Forward proxy & custom CA trust

Some deployments sit in networks that have **no direct internet egress** —
outbound traffic must traverse a corporate HTTP(S) **forward proxy**, and TLS is
often **intercepted** by that proxy (or by an internal registry / VCS / artifact
store) presenting certificates signed by a **private/corporate CA** the default
image trust store doesn't know.

Terrapod has two chart-level knobs for this, deliberately separate and
composable:

- `proxy:` — route every component's outbound HTTP(S) through a forward proxy.
- `caBundle:` — add a custom CA (or CAs) to every component's **outbound** TLS
  trust, so a TLS-intercepting proxy or a privately-signed registry/VCS endpoint
  is trusted.

Both are **off by default** and change nothing unless enabled. They apply to all
long-lived components (API, web/BFF, listener) **and** to the ephemeral runner
Jobs that execute `terraform`/`tofu` — so `terraform init` can fetch **public**
registry and `git::` module sources through the proxy and trust the intercepting
CA.

## What needs egress (and what doesn't)

| Component | Typical outbound egress | Through proxy? |
|---|---|---|
| API | Upstream provider/binary mirrors, VCS APIs, run-task & notification webhooks, the AI provider | Yes, when set |
| web/BFF (Node) | None outbound (proxies to the API in-cluster) | Honours env if set |
| listener | The API (in-cluster service in single-cluster; the **public URL** in split-cluster) | Yes, when set |
| runner Job | The API (binaries/providers/state/config — usually in-cluster), **plus public registry/`git::` module sources** | Yes, when set |
| migrations / bootstrap Jobs | The in-cluster database only | No — DB is in `no_proxy`; DB server-cert trust is the separate `api.databaseCA` |

## `proxy:`

```yaml
proxy:
  enabled: true
  httpProxy: "http://proxy.internal:3128"
  httpsProxy: "http://proxy.internal:3128"   # often the same endpoint
  noProxy: []                                 # extra entries; cluster-internal defaults are appended
```

When enabled, the chart sets `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` (and the
lowercase variants — Go/terraform read uppercase, many libraries read lowercase)
on every component, including runner Jobs. `no_proxy` always carries
cluster-local defaults (`localhost`, `127.0.0.1`, the `*.svc` / `*.cluster.local`
domains, and the in-cluster Service names) and appends your `noProxy` list.

**Split-cluster note (ARC topology).** When the listener + runner live in a
**different cluster** from the API, they reach it over the **public URL**, not
the in-cluster Service name — so that hop legitimately traverses the proxy.
Terrapod does **not** assume the runner→API hop is proxy-exempt: the public host
isn't in `no_proxy`, so it follows your normal proxy rules. If you have a direct
private path to a remote API and want to bypass the proxy for it, add its host to
`proxy.noProxy`.

All Terrapod HTTP clients honour these env vars (Python `httpx` and Go `net/http`
both read the standard proxy environment by default).

## `caBundle:`

```yaml
caBundle:
  enabled: true
  inline: |                       # paste the extra CA PEM(s); the chart makes a ConfigMap
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
  # existingConfigMap: my-corp-ca # OR reference one you already manage
  # existingSecret: my-corp-ca    # OR a Secret
  key: ca-extra.pem               # key within the ConfigMap/Secret
```

A CA bundle is public, non-secret material — prefer a ConfigMap (`inline` creates
one; `existingConfigMap`/`existingSecret` reference your own).

### How the trust is wired

The supplied CA must be **added to**, not **replace**, the image's built-in
public roots — otherwise public endpoints (GitHub, the Terraform registry, your
cloud APIs) would stop verifying. Terrapod does this per runtime:

- **Python / Go / git / curl components (API, listener, runner)** — an
  init container concatenates the image's system roots
  (`/etc/ssl/certs/ca-certificates.crt`) with your CA into a single merged
  bundle on a shared `emptyDir`, and the app trusts the **merged** file via
  `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` (covers Python, curl,
  Go, terraform/tofu) and `GIT_SSL_CAINFO` (git). The merge lands on the writable
  `emptyDir` because the containers run with `readOnlyRootFilesystem`.
- **web/BFF (Node)** — Node's `NODE_EXTRA_CA_CERTS` **appends** to the built-in
  roots, so it trusts the raw source file directly with no merge step.

### Runner Jobs (cross-namespace)

Runner Jobs run in the runner namespace (often distinct from the release
namespace), so they can't mount the release-namespace CA ConfigMap. Instead the
**listener** reads its own mounted copy of the raw CA and ships it into a
**per-run Secret** (owner-referenced to the Job, so it's garbage-collected with
the Job — same lifecycle as the auth/vars Secrets). The runner Job's init
container then merges that with the **runner image's** own system roots. Shipping
the raw source (not the listener's already-merged bundle) means the runner trusts
the runner image's roots plus your CA, not the listener image's roots.

## Relationship to `api.databaseCA`

`caBundle` is **general outbound trust**. It is **separate** from
`api.databaseCA`, which verifies the **database server** certificate
specifically (and is mounted only on the components that talk to the DB). Set
`api.databaseCA` for a privately-signed Postgres endpoint; set `caBundle` for
everything else outbound (proxy MITM, internal registries/VCS/artifact stores).
They can both be set and don't interfere.

## Verifying

After enabling, confirm the env + init container rendered:

```sh
helm template terrapod helm/terrapod \
  --set proxy.enabled=true --set proxy.httpsProxy=http://proxy.internal:3128 \
  --set caBundle.enabled=true --set-string caBundle.inline="$(cat corp-ca.pem)" \
  | grep -E "HTTPS_PROXY|SSL_CERT_FILE|NODE_EXTRA_CA_CERTS|ca-merge"
```

On a live cluster, a runner Job for a workspace whose modules come from a public
registry through the proxy should complete `terraform init` without
`x509: certificate signed by unknown authority` or proxy-connection errors.
