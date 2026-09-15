# Vault as a variable value source

A workspace variable can hold a **reference** to a HashiCorp Vault secret
instead of a literal value. Terrapod reads the secret at run time and delivers
it through the same per-run Kubernetes Secret every other variable uses, so:

- **Vault stays the source of truth.** Nothing is copied into Terrapod's
  database — only the path is stored.
- **Dynamic secrets work.** A `database/creds/…` or `aws/creds/…` reference
  mints a fresh, short-lived credential on every run, which is the case that
  actually replaces a Vault Agent sidecar.
- **It inherits the variable model.** Variable sets, workspace scoping,
  precedence and the encrypted-at-rest pipeline all apply unchanged, because a
  Vault-backed variable is an ordinary `env` or `terraform` variable that
  happens to resolve its value elsewhere.

This replaces the pattern of running a Vault Agent injector to land secrets as
files on runner pods. That works, but the wiring lives in Kubernetes pod
annotations rather than in Terrapod — invisible to workspaces, variable sets
and RBAC, and scoped per agent pool rather than per workspace.

> **Read the security note before you configure this.** Terrapod becomes a
> credential broker, and the Vault policy — not Terrapod's RBAC — is the access
> boundary. See [Who can read what](#who-can-read-what).

---

## What you need to do in Vault

Terrapod authenticates as its own Kubernetes ServiceAccount by default, so
there is no credential to store anywhere. Vault validates the ServiceAccount
token by calling the Kubernetes TokenReview API.

That means Vault has to reach the cluster. If it cannot — a managed Vault, HCP
Vault Dedicated, a Vault in another network — use [`jwt` auth](#vault-outside-the-cluster-jwt-auth)
instead, which needs no reach-back. The policy (step 3) is the same either way.

Everything below runs against your Vault with a token that can manage auth
methods and policies.

### 1. Enable the Kubernetes auth method

```sh
vault auth enable kubernetes
```

### 2. Let Vault validate ServiceAccount tokens

Vault needs to reach the Kubernetes API and be allowed to call TokenReview.
Run this **from a pod in the cluster** (Vault itself, if it runs there) so the
projected ServiceAccount files are present:

```sh
vault write auth/kubernetes/config \
  kubernetes_host="https://$KUBERNETES_PORT_443_TCP_ADDR:443" \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token
```

The ServiceAccount whose token you use as `token_reviewer_jwt` must be bound to
the `system:auth-delegator` ClusterRole:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: vault-token-reviewer
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
  - kind: ServiceAccount
    name: vault           # the ServiceAccount Vault runs as
    namespace: vault
```

**This is the step that most often goes wrong.** Without the binding, Vault
cannot verify the token Terrapod presents and every login fails with
`permission denied` — from Vault, not from Kubernetes, which makes it look like
a policy problem when it is not.

If Vault runs **outside** the cluster, supply `kubernetes_host` and
`kubernetes_ca_cert` for your API server and a `token_reviewer_jwt` minted for
a ServiceAccount with the same binding.

### 3. Write a policy — narrowly

This policy is the real limit on what Terrapod can read. Grant the least it
needs:

```sh
vault policy write terrapod - <<'EOF'
# Static secrets this Terrapod may read.
path "secret/data/apps/*" {
  capabilities = ["read"]
}

# A dynamic engine: each read mints a fresh credential.
path "database/creds/app-readonly" {
  capabilities = ["read"]
}
EOF
```

Note the `data/` segment for kv-v2 paths in the *policy* — it is part of the
API path even though `vault kv get secret/apps/x` hides it. Terrapod's
reference does not include it; see [Writing a reference](#writing-a-reference).

### 4. Bind a role to Terrapod's ServiceAccount

```sh
vault write auth/kubernetes/role/terrapod \
  bound_service_account_names=terrapod \
  bound_service_account_namespaces=terrapod \
  policies=terrapod \
  ttl=20m
```

`bound_service_account_names` is the ServiceAccount the **API** pods run as. By
default the chart derives it from the release name (so a release called
`terrapod` gives a ServiceAccount called `terrapod`); `api.serviceAccount.name`
overrides it. Confirm rather than assuming — a mismatch here is the second most
common cause of a login failure:

```sh
kubectl -n terrapod get pod -l app.kubernetes.io/component=api \
  -o jsonpath='{.items[0].spec.serviceAccountName}'
```

---

## Vault outside the cluster: `jwt` auth

Kubernetes auth makes Vault call the cluster's TokenReview API to check each
login. A Vault outside the cluster often cannot reach that API. `jwt` auth
removes the call: Terrapod presents a ServiceAccount token projected with an
audience of its own, and Vault checks the token's signature against the
cluster's published signing keys. Nothing is stored on either side.

### In Vault

Find the cluster's issuer — the URL its ServiceAccount tokens are signed as:

```sh
kubectl get --raw /.well-known/openid-configuration | jq -r .issuer
```

Enable JWT auth and point it at that issuer. When Vault can reach the issuer's
discovery document (managed Kubernetes services publish it at a public URL):

```sh
vault auth enable jwt
vault write auth/jwt/config \
  oidc_discovery_url="https://<issuer>" \
  bound_issuer="https://<issuer>"
```

Add `oidc_discovery_ca_pem=@issuer-ca.pem` if the issuer's certificate is not
publicly trusted. When Vault cannot reach the issuer at all, give it the
cluster's ServiceAccount signing public key instead —
`jwt_validation_pubkeys=@sa.pub` (PEM), with `bound_issuer` as above. That key
only changes when the cluster rotates it, and Vault must be updated when it does.

Write the policy as in [step 3](#3-write-a-policy--narrowly), then bind a role
to Terrapod's ServiceAccount:

```sh
vault write auth/jwt/role/terrapod \
  role_type=jwt \
  bound_audiences=vault \
  user_claim=sub \
  bound_subject=system:serviceaccount:terrapod:terrapod \
  policies=terrapod \
  ttl=20m
```

`bound_subject` is `system:serviceaccount:<namespace>:<ServiceAccount>` for the
**API** pods — see step 4 for how to confirm the ServiceAccount's name.
`bound_audiences` must contain the audience Terrapod requests, `vault` unless
you change it.

### In Terrapod

```yaml
api:
  config:
    vault:
      enabled: true
      instances:
        - name: hcp
          default: true
          address: https://vault.example.com:8200
          namespace: admin        # HCP Vault Dedicated's top-level namespace
          auth:
            method: jwt
            role: terrapod
            # mount: jwt          # the default for jwt
            # audience: vault     # the default; must be in bound_audiences
```

The chart projects a ServiceAccount token with that audience and a ten-minute
lifetime (`expirationSeconds: 600`, the shortest the kubelet issues) at
`/var/run/secrets/terrapod/vault/<instance>/token`, and tells Terrapod to read
it there. The kubelet rotates the file; Terrapod re-reads it on every login, so
rotation needs nothing from you. The projected volume is rendered only when an
instance needs it and `vault.enabled` is true.

With `namespace: admin` this is the HCP Vault Dedicated topology. The namespace
is sent on the login as well as on every read, so the JWT auth mount, the role
and the policy all live inside that namespace.

### An audience on Kubernetes auth

A Kubernetes auth role can require an audience too (`audience=` on the role).
Set the same value as `auth.audience` on a `kubernetes` instance and the chart
projects a token carrying it, instead of using the pod's standard token. Leave
it empty otherwise.

`auth.token_path` overrides where either method reads its token — for a
projection of your own, mounted through `api.extraVolumes`.

---

## A private CA in front of Vault

When Vault's certificate is signed by a CA of your own, put the CA in a Secret
in the release namespace and name it on the instance:

```sh
kubectl -n terrapod create secret generic vault-ca --from-file=ca.crt=./vault-ca.pem
```

```yaml
        - name: default
          address: https://vault.example.com:8200
          tls:
            ca_secret: vault-ca
            ca_key: ca.crt        # the default
```

The chart mounts that key at `/etc/terrapod/vault-ca/<instance>/ca.crt` and
renders it into the config as `ca_file`. TLS to **that instance** is then
verified against that CA **alone**: the default roots and `SSL_CERT_FILE` are
not consulted, so a private CA is pinned to the one Vault it fronts and trusted
for nothing else. Updating the Secret is enough to rotate it — the kubelet
refreshes the file and Terrapod reloads it when its modification time changes,
without a restart. Setting both `tls.ca_secret` and `tls_skip_verify` is refused
at startup, because the two contradict each other.

### Does the global `caBundle` already cover Vault?

**Yes, for every instance that does not set its own CA.** Checked against the
code and the image, not assumed:

- An instance without `ca_file` passes `verify=True` to httpx.
- The API image resolves **httpx 0.28.1**. For `verify=True` it builds its TLS
  context from `SSL_CERT_FILE` when that variable is set, and from certifi's
  bundle when it is not.
- `caBundle.enabled` sets `SSL_CERT_FILE` on the API pod to a bundle that merges
  the image's system roots with your CA.

So a CA you have already added through `caBundle` is trusted for Vault too, and
needs no per-instance setting. Use `tls.ca_secret` when you want the CA pinned
to one Vault rather than trusted for all of Terrapod's outbound traffic.

Two things follow. Without `caBundle`, httpx trusts **certifi's** bundle, not the
operating system's store. And the behaviour belongs to httpx, so a test
(`test_pinned_httpx_honours_ssl_cert_file_for_verify_true`) pins it: an httpx
upgrade that changed it would fail CI rather than quietly stop trusting your CA.

---

## Other auth methods and namespaces

### AppRole

For a Vault that cannot validate Kubernetes tokens by either method. `role` is
the AppRole **role_id**; the **secret_id** is a credential, so it comes from a
Secret:

```yaml
        - name: prod-vault
          address: https://vault.example.com:8200
          auth:
            method: approle
            mount: approle
            role: <role-id>        # AppRole role_id
          existingSecret: my-vault-approle
          existingSecretKey: secret_id    # defaults to "secret"
```

### A static token

For a lab, or a Vault where nothing else is available:

```yaml
        - name: lab
          address: https://vault.example.com:8200
          auth:
            method: token
          existingSecret: my-vault-token
          existingSecretKey: token
```

Terrapod uses the token as given and does not renew it. When it expires, reads
fail until the Secret is replaced.

For both, the chart injects the Secret as `TERRAPOD_VAULT_<NAME>_SECRET` (the
instance name upper-cased, dashes as underscores) via `secretKeyRef`, never
through the ConfigMap. An environment variable is fixed when the pod starts, so
after rotating the Secret, restart the API pods. `kubernetes` and `jwt` store
nothing and need none of this.

### Namespaces

Vault Enterprise and HCP namespaces work with every method. `namespace` is sent
as `X-Vault-Namespace` on the login and on every read:

```yaml
        - name: team-a
          address: https://vault.example.com:8200
          namespace: team-a
          auth:
            method: kubernetes
            role: terrapod
```

The auth mount, the role and the policy must all exist inside that namespace,
and a reference's `mount` and `path` are relative to it.

---

## What you configure in Terrapod

```yaml
api:
  config:
    vault:
      enabled: true
      instances:
        - name: default
          default: true
          address: https://vault.internal:8200
          auth:
            method: kubernetes
            mount: kubernetes      # matches `vault auth enable -path=…`
            role: terrapod         # the role created above
```

`instances` is a list from the outset, so a second Vault is a configuration
change rather than a migration.

| Key | Meaning |
|---|---|
| `name` | What a reference uses to pick this Vault. |
| `default` | Used when a reference omits `vault`. At most one instance may set it. |
| `address` | Vault's address, including scheme and port. |
| `namespace` | Vault Enterprise / HCP namespace. Omit for OSS. |
| `auth.method` | `kubernetes` (default), `jwt`, `approle`, or `token`. See [`jwt`](#vault-outside-the-cluster-jwt-auth) and [other methods](#other-auth-methods-and-namespaces). |
| `auth.mount` | The auth mount path you enabled. Defaults to `kubernetes`, or `jwt` for the `jwt` method. |
| `auth.role` | The Vault role bound to Terrapod's ServiceAccount (the role_id, for AppRole). |
| `auth.audience` | The projected token's audience. `jwt` defaults to `vault`; for `kubernetes`, set it only if the role requires one. |
| `auth.token_path` | Where the token is read on each login. Defaults to the chart's projected path, or the standard ServiceAccount token. |
| `paths` | Optional allow-list of path prefixes. See below. |
| `tls.ca_secret` / `tls.ca_key` | A Secret key holding the CA that signs this Vault's certificate. See [A private CA](#a-private-ca-in-front-of-vault). |
| `tls_skip_verify` | Lab use only. A credential broker that does not verify its peer is not one. |

### More than one Vault

Give each an entry and mark one `default: true`. A reference that omits `vault`
resolves to the default, or to the sole instance when only one is configured.
If several are configured and none is marked default, an omitted name is an
**error** rather than a guess — reading a credential from the wrong Vault is
silent, and silence is the failure worth engineering against.

---

## Writing a reference

Set the variable's **value source** to `vault` and give it coordinates. In the
UI this is a form; through the API the value is a JSON object:

```json
{ "mount": "secret", "path": "apps/netbox", "field": "apitoken" }
```

| Field | Meaning |
|---|---|
| `mount` | The secret engine's mount path (`secret`, `database`, `aws`, …). |
| `path` | The path within that mount. **No `data/` segment** — Terrapod adds it for kv-v2. |
| `field` | Which key of the secret to use as the value. |
| `vault` | Optional. Which configured instance to read from. |
| `engine` | `kv2` (default) or `dynamic`. |
| `method` | `GET` (default) or `POST`, for engines that mint on write. |
| `data` | Optional request body, when `method` is `POST`. |
| `file` | Optional. Deliver the value as a file instead — see [Delivering as a file](#delivering-as-a-file). |

### Static secrets (kv-v2)

```json
{ "mount": "secret", "path": "apps/netbox", "field": "apitoken" }
```

### Dynamic secrets

Most dynamic engines are a `vault read`, so the default `GET` is right:

```json
{ "engine": "dynamic", "mount": "database", "path": "creds/app-readonly", "field": "password" }
```

Each run mints a fresh credential. Terrapod does not renew or revoke the
lease — set a TTL on the Vault role that suits your run durations.

**Fields of one secret come from one read.** Within a run, variables whose
references name the same secret — the same instance, engine, mount, path,
method and request body — share a single Vault request, and each takes its own
`field` from that one response. So `TLS_CERT` (`field: certificate`) and
`TLS_KEY` (`field: private_key`) on `pki/issue/example` are a matching pair from
one issue, and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` on `aws/creds/deploy`
come from one lease. Reading them separately would mint two credentials, and the
halves would not belong together.

Key order inside `data` does not matter, and for a kv-v2 read or any `GET` the
`method` and `data` fields are ignored (Terrapod never sends a body with them),
so they cannot split a read either. If the shared read fails, every variable
that depends on it fails with it, and the error names them all.

Some engines mint on write (`pki/issue/…`, `aws/sts/…`), which needs `POST`:

```json
{
  "engine": "dynamic", "method": "POST",
  "mount": "pki", "path": "issue/example", "field": "certificate",
  "data": { "common_name": "app.example.internal" }
}
```

### With the Terraform provider

```hcl
resource "terrapod_variable" "netbox_token" {
  workspace_id = terrapod_workspace.app.id
  key          = "NETBOX_TOKEN"
  category     = "env"
  value_source = "vault"
  value = jsonencode({
    mount = "secret"
    path  = "apps/netbox"
    field = "apitoken"
  })
}
```

The same `value_source` works on a variable inside a **variable set**, which is
how you define a reference once and apply it to many workspaces. Note the
attribute is on `terrapod_variable_set_variable` — the variable *within* the set
— not on `terrapod_variable_set` itself:

```hcl
resource "terrapod_variable_set_variable" "netbox_token" {
  varset_id    = terrapod_variable_set.shared.id
  key          = "NETBOX_TOKEN"
  category     = "env"
  value_source = "vault"
  value = jsonencode({
    mount = "secret"
    path  = "apps/netbox"
    field = "apitoken"
  })
}
```

With an [assignment rule](api-reference.md#assignment-rules) the set can target
workspaces by label rather than one by one, so a single reference covers a whole
population. Resolution happens per run, per workspace, exactly as it does for a
workspace variable — the set is only how the reference is distributed.

---

## Delivering as a file

Some tools take a credential only from a file: a GCP service-account key, a
kubeconfig, a CA bundle, an AWS shared-credentials file. Add a `file` object to
the reference and Terrapod writes the secret to a file on the runner instead:

```json
{ "mount": "secret", "path": "apps/gcp", "field": "sa_json",
  "file": { "name": "gcp/adc.json" } }
```

**The variable's value becomes the file's absolute path.** The secret itself is
never in an environment variable or in the generated tfvars — only the path is.

- An `env` variable named `GOOGLE_APPLICATION_CREDENTIALS` with the reference
  above runs with
  `GOOGLE_APPLICATION_CREDENTIALS=/var/run/terrapod/files/gcp/adc.json`, which is
  what the Google provider and SDKs look for.
- A `terraform` variable holds the path, so read the file where you need its
  contents:

  ```hcl
  variable "sa_json" {
    type = string # the path, e.g. /var/run/terrapod/files/sa_json
  }

  provider "google" {
    credentials = file(var.sa_json)
  }
  ```

The file exists before `init`, so it is available to every phase.

A field that Vault holds as a map or list (a service-account key stored as a
JSON object rather than a string) is written as JSON.

### Where the file lands

| `file.name` | Written to |
|---|---|
| `gcp/adc.json` — a relative path | `/var/run/terrapod/files/gcp/adc.json` |
| `~/.aws/credentials` — a path in the runner's home | `/home/runner/.aws/credentials` |
| omitted (`"file": {}`) | `/var/run/terrapod/files/<variable key>` |

A name is refused (`422` when you save it, and the run errors if one is found
when the run is claimed) unless:

- every `/`-separated segment uses only `A-Z a-z 0-9 . _ -`;
- no segment is empty, `.` or `..`, and the name is not absolute;
- it is at most 255 characters.

A home path may not target anything the runner manages itself, or a directory
above one: `~/.ssh`, `~/.gitconfig`, `~/.config/terrapod-git` (private-module
git credentials), `~/.terraformrc`, `~/.terraform.rc`, `~/.terraform.d` and
`~/.pulumi` (the Pulumi CLI's plugins and workspace state).

### Permissions

The file is a key of the per-run Kubernetes Secret, mounted **read-only**. A
Secret volume is memory-backed, so the file never touches the node's disk, and
it sits outside `/workspace`, so it never enters the plan artifacts Terrapod
uploads.

Its mode is `0440` when `runners.podSecurityContext` sets an `fsGroup`, and
`0444` otherwise. Kubelet projects Secret files owned by root, and the runner is
a non-root user, so without an `fsGroup` the file has to be world-readable for
the runner to read it. Setting an `fsGroup` narrows it to owner and group.

A home path is mounted onto that single file (a `subPath` mount). Terrapod
creates its parent directories first, as the runner's own user, so the runner
can still write beside it — the AWS CLI's cache under `~/.aws`, for example. The
file itself stays read-only: a tool that rewrites that exact file fails.

### What is refused

| Combination | Result |
|---|---|
| `file` together with `structured` (or its alias `hcl`) | `422` — the value is a path, not a typed expression. |
| `file` on a variable whose value source is `static` | `422` — the reference would be delivered as the literal JSON. |
| Two variables at one path, or a file where another needs a directory (`a` and `a/b`) | The run errors, naming both variables. Checked after variable-set precedence, so a workspace variable that overrides a set variable of the **same key** is one file, not a clash. |
| A value over 256 KiB | The run errors, giving the size. |
| Any other key inside `file` | `422`. `template`, `format`, `encoding` and `mode` are reserved for a later release. |

### Older listeners

The files reach the listener in a `vault-files` attribute of the claimed run.
A listener older than this feature ignores that attribute: the variable still
carries the path, the file does not exist, and the run fails when the tool opens
it. It **fails safe** — the secret is never put anywhere else. Upgrade your
listeners before relying on file delivery.

### Clashes with operator mounts

Files are mounted at `/var/run/terrapod/files` and, for home paths, at each
file's own path under `/home/runner`. If `runners.extraVolumeMounts` mounts
something at one of those paths, Kubernetes rejects the Job as having a
duplicate mount path and the run errors with `Failed to create K8s Job`. A mount
at a directory above one of them can hide the file instead. Pick names that do
not overlap your own mounts.

Each file is stored under a Secret key named `vault-file-0`, `vault-file-1`, and
so on. An `env` variable with one of those exact names is refused when the run
launches.

---

## Who can read what

**Terrapod is a credential broker once this is enabled.** Anyone who can set a
workspace variable can ask it to read any path Terrapod's Vault role can reach.

**The Vault policy is the access boundary — not Terrapod's RBAC.** Terrapod's
workspace permissions control who can *edit variables*; they do not constrain
*which paths* those variables may name. Scope the policy in step 3 to exactly
what your workspaces need, and prefer several instances with narrow policies
over one instance with a broad one.

As a second line, an instance may declare an allow-list of path prefixes that
Terrapod will refuse to read outside. Prefixes match on **path segments**, so
`secret/apps` permits `secret/apps/netbox` but not `secret/apps-admin`, and a
reference containing `.` or `..` segments is refused outright — otherwise the
URL Terrapod checks and the one Vault receives would differ:

```yaml
        - name: default
          address: https://vault.internal:8200
          paths:
            - secret/apps
            - database/creds
```

This is belt-and-braces over a correctly scoped policy, not a replacement for
one — but "correctly scoped" does a lot of work in that sentence, and an
operator who gets it slightly wrong otherwise has no second line.

### What is stored, logged, and returned

| | |
|---|---|
| **Stored in Terrapod** | The reference (mount, path, field). Never the secret. |
| **Returned by the API** | The reference. A path is not a secret, and masking it would hide configuration while concealing nothing. |
| **In run logs** | Nothing. The value is delivered through the per-run Kubernetes Secret, never a command line or the Job spec. |
| **In the Job spec** | Secret references and, for file delivery, file paths and Secret key names. Never a value. |
| **Delivered as a file** | The secret is only in the per-run Secret and the read-only file it is mounted as. The variable's env value or tfvars entry is the file's path. |
| **In Terrapod's own logs** | Variable names, instances, coordinates and file names. Never a value. |
| **On failure** | The variable name, the instance, the coordinates and the HTTP status — never Vault's response body or a partial value. |

---

## When a reference cannot be resolved

Terrapod never proceeds with the variable missing. A missing secret is worse
than a failed run — Terraform either fails somewhere confusing, or falls back to
another identity and acts with credentials nobody chose. (This differs from
private-git-module credentials, which are dropped with a warning so one bad
credential cannot fail everything.)

What happens next depends on **which kind of failure it is**, and the difference
matters when you are diagnosing one:

| Vault's answer | What it means | What Terrapod does |
|---|---|---|
| `403`, `404`, other 4xx | A real answer: denied, or nothing at that path | **The run is errored**, naming the variable and the cause |
| Unreachable, DNS failure, TLS failure, timeout | Vault cannot be contacted | **The run returns to `queued`** and waits for the next claim |
| `503` (sealed), `501`, `429`/`473` (standby), other 5xx | Vault is up but cannot answer yet | **The run returns to `queued`** and waits |

A misconfigured reference will never resolve, so retrying it would only hide the
fault. A Vault that is sealed, restarting or briefly unreachable *will* answer in
a moment — and erroring every queued run in the estate for that turns a blip into
an incident someone has to clean up by hand.

**The waiting case is quiet, and you should know its shape.** A run held this way
shows no error: it simply sits in `queued` and is re-claimed periodically. If
Vault never comes back — a wrong `address`, a blocked egress rule, a certificate
the API pod does not trust — the run waits indefinitely rather than failing. The
signal is in the API pod log:

```
vault is unavailable; leaving the run for a later claim
```

If runs are not starting and that line is repeating, treat it as a connectivity
problem between the API pods and Vault, not as a problem with the run. See
[the runbook](runbooks.md).

For the errored case, the run carries the cause, naming the variable:

```
variable 'NETBOX_TOKEN': Vault denied 'secret/apps/netbox' on instance
'default'. The policy attached to role 'terrapod' does not grant read on
this path.
```

### Troubleshooting

| What you see | What it usually means |
|---|---|
| `Vault login failed for instance 'default' (kubernetes auth, mount 'kubernetes', role 'terrapod')` | The role does not exist, or its `bound_service_account_names` / `bound_service_account_namespaces` do not match the API pod's ServiceAccount. |
| `permission denied` on login | Vault cannot call TokenReview — the `system:auth-delegator` binding in step 2 is missing. |
| `Vault denied '<path>' … policy attached to role` | Login worked; the policy does not grant `read` on that path. Remember the `data/` segment for kv-v2 in the *policy*. |
| `Vault has no secret at '<path>'` | Wrong mount or path. Note the reference omits `data/` while the policy includes it. |
| `field '<x>' is not present at '<path>' (available: …)` | Right secret, wrong key. The message lists what is there. |
| `path '<x>' is not in the allow-list configured for vault instance` | Terrapod's own `paths` allow-list refused it before contacting Vault. |
| `could not read the ServiceAccount token` | Kubernetes auth outside a cluster. Use `approle` or `token` instead. |
| `Vault login failed … (jwt auth, mount 'jwt', role 'terrapod', audience 'vault')` | The role does not exist; its `bound_audiences` lacks the audience shown; its `bound_subject` does not match `system:serviceaccount:<namespace>:<ServiceAccount>`; or Vault cannot verify the signature (a wrong or unreachable `oidc_discovery_url`, or stale `jwt_validation_pubkeys`). |
| `could not read the projected ServiceAccount token` | The chart has not projected it — `vault.enabled` is false — or `auth.token_path` points somewhere with no file. |
| `could not read the CA file` | The `tls.ca_secret` Secret, or its `tls.ca_key` key, does not exist. |
| `not a usable PEM certificate bundle` | `tls.ca_key` names a key that does not hold a PEM certificate. |
| Runs sit in `queued` and the log shows a TLS verification failure | Vault's certificate is not trusted. Add its CA through `tls.ca_secret`, or through the global `caBundle`. |
| `references unknown vault instance '<name>'` | The reference names an instance that is not in `instances`. |
| `omits 'vault' but several instances are configured` | Mark one `default: true`, or name the instance in the reference. |

---

## Limits

- **Agent execution only.** The reference is resolved server-side when a runner
  claims the run, so a local-execution workspace has no point at which it could
  happen. Creating a Vault-sourced **workspace** variable on one is refused
  rather than silently resolving to nothing, and switching a workspace that
  holds one to local execution is refused for the same reason.

  A **variable set** is checked at the points where the pairing can actually be
  created, since a set is not bound to a workspace and there is no single write
  to test. All three are refused: assigning a workspace to a set that carries a
  Vault reference, writing a Vault reference into a set that already reaches a
  local-execution workspace, and switching a workspace to local while a set
  delivers one to it (the workspace's own variables and the set's are counted
  together).

  One gap remains by design: a set assigned by an **assignment rule** whose match
  set widens later — a label edit, or a new local workspace matching the rule —
  is not a write against either side, so there is nothing to refuse at. On such a
  workspace the reference resolves to nothing while agent-mode workspaces in the
  same set are unaffected. Prefer agent execution for any workspace a
  Vault-bearing set can reach.
- **Leases are not renewed or revoked.** A dynamic credential is minted per run
  and left to expire. Set the Vault role's TTL to suit your run durations.
- **One field per file.** A file holds one field of a secret, or the whole field
  as JSON when Vault stores it as a map. Combining several fields into one file
  (a PEM bundle, say) is not supported yet; `template`, `format`, `encoding` and
  `mode` are reserved in the `file` object for that. Files are text: there is no
  binary or base64 decoding yet.
- **`approle` and `token` auth** work but are less well trodden than
  `kubernetes` and `jwt`, which need no stored credential. See
  [Other auth methods](#other-auth-methods-and-namespaces).
