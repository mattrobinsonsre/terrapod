# OpenBao (or HashiCorp Vault) as a variable value source

A workspace variable can hold a **reference** to a secret in
[OpenBao](https://openbao.org/) (or HashiCorp Vault) instead of a literal value.
Terrapod reads the secret at run time and delivers it through the same per-run
Kubernetes Secret every other variable uses, so:

- **The server stays the source of truth.** Nothing is copied into Terrapod's
  database — only the path is stored.
- **Dynamic secrets work.** A `database/creds/…` or `aws/creds/…` reference
  mints a fresh, short-lived credential on every run, which is the case that
  actually replaces an agent sidecar.
- **It inherits the variable model.** Variable sets, workspace scoping,
  precedence and the encrypted-at-rest pipeline all apply unchanged, because an
  OpenBao/Vault-backed variable is an ordinary `env` or `terraform` variable that
  happens to resolve its value elsewhere.

This replaces the pattern of running an agent injector to land secrets as
files on runner pods. That works, but the wiring lives in Kubernetes pod
annotations rather than in Terrapod — invisible to workspaces, variable sets
and RBAC, and scoped per agent pool rather than per workspace.

> **Read the security note before you configure this.** Terrapod becomes a
> credential broker, and the OpenBao/Vault policy — not Terrapod's RBAC — is the
> access boundary. See [Who can read what](#who-can-read-what).

## OpenBao first, HashiCorp Vault too

Terrapod recommends OpenBao, the open-source (MPL-2.0) fork of HashiCorp Vault,
maintained under the Linux Foundation, in the same way it recommends OpenTofu
and supports Terraform. HashiCorp Vault is supported just as fully, including
Vault Enterprise and HCP Vault Dedicated. Nothing in this feature is gated on
one server or the other.

Both servers speak the same HTTP API for everything this feature uses, so the
value source, its settings and its API attributes are all named `vault`, after
that API. They keep the name
whichever server you run, as `terraform.tfvars` and the `TF_*` variables do
under OpenTofu.

The commands on this page use OpenBao's `bao` CLI. With Vault, run the same
command with `vault` in place of `bao`. Where the two servers differ, the
section says so: namespaces and HCP Vault Dedicated are the main cases.

---

<a id="what-you-need-to-do-in-vault"></a>

## What you need to do in OpenBao/Vault

Terrapod authenticates as its own Kubernetes ServiceAccount by default, so
there is no credential to store anywhere. The server validates the
ServiceAccount token by calling the Kubernetes TokenReview API.

That means the server has to reach the cluster. If it cannot — a managed
service such as HCP Vault Dedicated, or a server in another network — use
[`jwt` auth](#vault-outside-the-cluster-jwt-auth) instead, which needs no
reach-back. The policy (step 3) is the same either way.

Everything below runs against your server with a token that can manage auth
methods and policies.

### 1. Enable the Kubernetes auth method

```sh
bao auth enable kubernetes
```

<a id="2-let-vault-validate-serviceaccount-tokens"></a>

### 2. Let the server validate ServiceAccount tokens

The server needs to reach the Kubernetes API and be allowed to call TokenReview.
Run this **from a pod in the cluster** (the OpenBao or Vault pod itself, if it
runs there) so the projected ServiceAccount files are present:

```sh
bao write auth/kubernetes/config \
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
  name: openbao-token-reviewer
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
  - kind: ServiceAccount
    name: openbao         # the ServiceAccount OpenBao (or Vault) runs as
    namespace: openbao
```

**This is the step that most often goes wrong.** Without the binding, the
server cannot verify the token Terrapod presents and every login fails with
`permission denied` — from OpenBao/Vault, not from Kubernetes, which makes it look like
a policy problem when it is not.

If the server runs **outside** the cluster, supply `kubernetes_host` and
`kubernetes_ca_cert` for your API server and a `token_reviewer_jwt` minted for
a ServiceAccount with the same binding.

### 3. Write a policy — narrowly

This policy is the real limit on what Terrapod can read. Grant the least it
needs:

```sh
bao policy write terrapod - <<'EOF'
# Static secrets this Terrapod may read.
path "secret/data/apps/*" {
  capabilities = ["read"]
}

# A dynamic engine: each read mints a fresh credential.
path "database/creds/app-readonly" {
  capabilities = ["read"]
}

# Only with `revoke_leases: true` on the instance — see "Revoking leases".
path "sys/leases/revoke" {
  capabilities = ["update"]
}
EOF
```

Note the `data/` segment for kv-v2 paths in the *policy* — it is part of the
API path even though `bao kv get secret/apps/x` hides it. Terrapod's
reference does not include it; see [Writing a reference](#writing-a-reference).

### 4. Bind a role to Terrapod's ServiceAccount

```sh
bao write auth/kubernetes/role/terrapod \
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

<a id="vault-outside-the-cluster-jwt-auth"></a>

## A server outside the cluster: `jwt` auth

Kubernetes auth makes the server call the cluster's TokenReview API to check
each login. A server outside the cluster often cannot reach that API. `jwt`
auth removes the call: Terrapod presents a ServiceAccount token projected with
an audience of its own, and the server checks the token's signature against
the cluster's published signing keys. Nothing is stored on either side.

<a id="in-vault"></a>

### In OpenBao/Vault

Find the cluster's issuer — the URL its ServiceAccount tokens are signed as:

```sh
kubectl get --raw /.well-known/openid-configuration | jq -r .issuer
```

Enable JWT auth and point it at that issuer. When the server can reach the
issuer's discovery document (managed Kubernetes services publish it at a public URL):

```sh
bao auth enable jwt
bao write auth/jwt/config \
  oidc_discovery_url="https://<issuer>" \
  bound_issuer="https://<issuer>"
```

Add `oidc_discovery_ca_pem=@issuer-ca.pem` if the issuer's certificate is not
publicly trusted. When the server cannot reach the issuer at all, give it the
cluster's ServiceAccount signing public key instead —
`jwt_validation_pubkeys=@sa.pub` (PEM), with `bound_issuer` as above. That key
only changes when the cluster rotates it, and the server's config must be
updated when it does.

Write the policy as in [step 3](#3-write-a-policy--narrowly), then bind a role
to Terrapod's ServiceAccount:

```sh
bao write auth/jwt/role/terrapod \
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
        - name: external
          default: true
          address: https://openbao.example.com:8200
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

For HCP Vault Dedicated, which is HashiCorp's managed Vault service, add
`namespace: admin`, its top-level namespace. The namespace is sent on the login
as well as on every read, so the JWT auth mount, the role and the policy all
live inside that namespace.

### An audience on Kubernetes auth

A Kubernetes auth role can require an audience too (`audience=` on the role).
Set the same value as `auth.audience` on a `kubernetes` instance and the chart
projects a token carrying it, instead of using the pod's standard token. Leave
it empty otherwise.

`auth.token_path` overrides where either method reads its token — for a
projection of your own, mounted through `api.extraVolumes`.

---

<a id="a-private-ca-in-front-of-vault"></a>

## A private CA in front of the server

When the server's certificate is signed by a CA of your own, put the CA in a Secret
in the release namespace and name it on the instance:

```sh
kubectl -n terrapod create secret generic openbao-ca --from-file=ca.crt=./openbao-ca.pem
```

```yaml
        - name: default
          address: https://openbao.example.com:8200
          tls:
            ca_secret: openbao-ca
            ca_key: ca.crt        # the default
```

The chart mounts that key at `/etc/terrapod/vault-ca/<instance>/ca.crt` and
renders it into the config as `ca_file`. TLS to **that instance** is then
verified against that CA **alone**: the default roots and `SSL_CERT_FILE` are
not consulted, so a private CA is pinned to the one server it fronts and trusted
for nothing else. Updating the Secret is enough to rotate it — the kubelet
refreshes the file and Terrapod reloads it when its modification time changes,
without a restart. Setting both `tls.ca_secret` and `tls_skip_verify` is refused
at startup, because the two contradict each other.

<a id="does-the-global-cabundle-already-cover-vault"></a>

### Does the global `caBundle` already cover the server?

**Yes, for every instance that does not set its own CA.** Checked against the
code and the image, not assumed:

- An instance without `ca_file` passes `verify=True` to httpx.
- The API image resolves **httpx 0.28.1**. For `verify=True` it builds its TLS
  context from `SSL_CERT_FILE` when that variable is set, and from certifi's
  bundle when it is not.
- `caBundle.enabled` sets `SSL_CERT_FILE` on the API pod to a bundle that merges
  the image's system roots with your CA.

So a CA you have already added through `caBundle` is trusted for OpenBao/Vault
too, and needs no per-instance setting. Use `tls.ca_secret` when you want the CA
pinned to one server rather than trusted for all of Terrapod's outbound traffic.

Two things follow. Without `caBundle`, httpx trusts **certifi's** bundle, not the
operating system's store. And the behaviour belongs to httpx, so a test
(`test_pinned_httpx_honours_ssl_cert_file_for_verify_true`) pins it: an httpx
upgrade that changed it would fail CI rather than quietly stop trusting your CA.

---

## Other auth methods and namespaces

### AppRole

For a server that cannot validate Kubernetes tokens by either method. `role` is
the AppRole **role_id**; the **secret_id** is a credential, so it comes from a
Secret:

```yaml
        - name: prod
          address: https://openbao.example.com:8200
          auth:
            method: approle
            mount: approle
            role: <role-id>        # AppRole role_id
          existingSecret: my-openbao-approle
          existingSecretKey: secret_id    # defaults to "secret"
```

### A static token

For a lab, or a server where nothing else is available:

```yaml
        - name: lab
          address: https://openbao.example.com:8200
          auth:
            method: token
          existingSecret: my-openbao-token
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

OpenBao has namespaces in its open-source release; this was checked against
OpenBao 2.6.2. In HashiCorp's product line they are a Vault Enterprise and
HCP Vault Dedicated feature. They work with every auth method: `namespace` is
sent as the `X-Vault-Namespace` header on the login and on every read, and
OpenBao and Vault both read that header:

```yaml
        - name: team-a
          address: https://openbao.example.com:8200
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
          address: https://openbao.internal:8200
          auth:
            method: kubernetes
            mount: kubernetes      # matches `bao auth enable -path=…`
            role: terrapod         # the role created above
```

`instances` is a list from the outset, so a second server is a configuration
change rather than a migration.

| Key | Meaning |
|---|---|
| `name` | What a reference uses to pick this instance. |
| `default` | Used when a reference omits `vault`. At most one instance may set it. |
| `address` | The server's address, including scheme and port. |
| `namespace` | The namespace to work in, on a server that has namespaces (OpenBao, Vault Enterprise or HCP Vault Dedicated). Omit otherwise. |
| `auth.method` | `kubernetes` (default), `jwt`, `approle`, or `token`. See [`jwt`](#vault-outside-the-cluster-jwt-auth) and [other methods](#other-auth-methods-and-namespaces). |
| `auth.mount` | The auth mount path you enabled. Defaults to `kubernetes`, or `jwt` for the `jwt` method. |
| `auth.role` | The role bound to Terrapod's ServiceAccount (the role_id, for AppRole). |
| `auth.audience` | The projected token's audience. `jwt` defaults to `vault`; for `kubernetes`, set it only if the role requires one. |
| `auth.token_path` | Where the token is read on each login. Defaults to the chart's projected path, or the standard ServiceAccount token. |
| `paths` | Optional allow-list of path prefixes. See below. |
| `tls.ca_secret` / `tls.ca_key` | A Secret key holding the CA that signs this server's certificate. See [A private CA](#a-private-ca-in-front-of-vault). |
| `tls_skip_verify` | Lab use only. A credential broker that does not verify its peer is not one. |
| `revoke_leases` | Revoke each dynamic secret's lease once the run phase's Job has ended. Off by default. See [Revoking leases](#revoking-leases). |

<a id="more-than-one-vault"></a>

### More than one server

Give each an entry and mark one `default: true`. A reference that omits `vault`
resolves to the default, or to the sole instance when only one is configured.
If several are configured and none is marked default, an omitted name is an
**error** rather than a guess — reading a credential from the wrong server is
silent, and silence is the failure worth engineering against.

---

## Writing a reference

Set the variable's **value source** to `vault` (the name for both servers) and
give it coordinates. In the
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

Most dynamic engines are a `bao read` (`vault read`), so the default `GET` is right:

```json
{ "engine": "dynamic", "mount": "database", "path": "creds/app-readonly", "field": "password" }
```

Each run mints a fresh credential — and so does **each phase**. A run's plan
and its apply are separate claims by a runner, and every claim resolves the
run's OpenBao/Vault variables again, so plan and apply never share a credential: the
apply gets its own, minted when the apply starts. Terrapod does not renew the
lease. With [`revoke_leases`](#revoking-leases) on it revokes the lease when
the phase's Job ends; otherwise the lease is left to expire. Either way, set
the role's TTL to cover one phase. For how that
interacts with Terraform variables, see
[Env, file or Terraform variable?](#env-file-or-terraform-variable).

**Fields of one secret come from one read.** Within a run, variables whose
references name the same secret — the same instance, engine, mount, path,
method and request body — share a single request, and each takes its own
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

### Env, file or Terraform variable?

**Deliver credentials as `env` variables or as files, not as `terraform`
variables.** The difference is what Terraform itself does with an input
variable:

- **Terraform stores input-variable values in the saved plan.** A
  `terraform`-category variable sourced from OpenBao/Vault is therefore written into the
  plan file, and Terrapod keeps that plan as a run artifact in its object
  storage. The secret is persisted at rest there for as long as the run's
  artifacts are kept, even though Terrapod's database only ever held the
  reference.
- **Apply reuses the plan-time value.** Terrapod applies the saved plan
  (`terraform apply tfplan`), and a saved plan carries its own variable values.
  The fresh credential the apply phase mints is delivered but not used; the one
  read at plan time is. A short-lived dynamic credential may have expired by the
  time a plan is confirmed.

`env` and file delivery avoid both:

- A provider reads an `env` variable (`AWS_ACCESS_KEY_ID`, `VAULT_TOKEN`,
  `GOOGLE_APPLICATION_CREDENTIALS`, …) from its environment in each phase, so it
  is never an input variable and never in the plan, and apply uses the
  credential minted for apply.
- A [file-delivered](#delivering-as-a-file) variable's value is the file's
  **path**, so a `terraform`-category file variable puts only the path in the
  plan. What your configuration then does with the file's contents follows
  Terraform's usual rules: a value you copy into a resource attribute is in the
  plan and the state like any other.

Keep `terraform`-category OpenBao/Vault variables for values that are not
secret-at-rest sensitive — a hostname, an account id, or other configuration
that happens to live in OpenBao/Vault — and still want to come from one place.

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

A field that the server holds as a map or list (a service-account key stored as a
JSON object rather than a string) is written as JSON.

A file can also be built from **several fields of the one read**, or from the
whole secret — see [Templates, formats and encoding](#templates-formats-and-encoding).

### Templates, formats and encoding

Real credential files need several fields together: an AWS credentials file
needs a key id *and* its secret, and a TLS bundle needs a certificate *and* the
key issued with it. Taking them from two variables would work for a static
secret, but a dynamic engine mints a new credential on every read. So the file
itself can say how to assemble its content from the single read that every
variable on that secret shares.

The content of a file is **exactly one** of these, and combining them is a
`422`:

| In the reference | The file holds |
|---|---|
| `field` | That one field (the default, as above). |
| `field` + `"file": {"encoding": "base64"}` | That field, base64-decoded. |
| `"file": {"template": "…"}`, no `field` | A template rendered against the whole secret. |
| `"file": {"format": "json"}` or `"env"`, no `field` | The whole secret, or the `"fields": [...]` subset. |

All of it is validated when you save the variable, except what depends on the
secret itself (whether a named field exists, whether a value is valid base64).
Those fail the run, naming the variable and the field — never a value.

#### Templates

A template is text with `{{ … }}` placeholders, at most 16 KiB:

```
{{ name }}                 a field of the secret
{{ creds.key }}            a key inside a map field (dots reach into maps)
{{ name | filter | … }}    filters, applied left to right
```

| Filter | Does |
|---|---|
| `json` | Writes the value as JSON (a string gets quotes and escapes). |
| `base64decode` | Decodes a base64 string; the result must be UTF-8 text. |
| `trim` | Strips leading and trailing whitespace. |
| `lines` | Joins a list with newlines — for a certificate chain. |
| `indent N` | Indents every line **after the first** by `N` spaces (0–64), so a multi-line value lines up under a placeholder that is already indented, as in a YAML block. |

A string field is written as it is; any other value (a number, a boolean, a map,
a list) is written as JSON.

When the response carries a lease — a dynamic engine's does, kv-v2's never does —
a template can also read `{{ _lease.ttl }}` (seconds), `{{ _lease.renewable }}`
and `{{ _lease.expires_at }}` (RFC 3339, UTC). The lease id is never offered.

It is **logic-less on purpose**: no loops, conditionals, functions, environment,
file or network access. It is a single pass and substituted values are never
re-scanned, so a secret that happens to contain `{{` is written literally and
cannot pull in anything else. Every `{{` opens a placeholder, so one without a
closing `}}` is refused rather than written as text. A name or filter that does
not exist fails the run naming it; an unknown filter or a malformed placeholder
is caught when you save.

`{{ }}` is not Terraform interpolation (that is `${ }`), so a template needs no
escaping inside `jsonencode` in the Terraform provider.

**An AWS credentials file from one `aws/creds` read.** The key id and the secret
come from the same lease:

```json
{ "engine": "dynamic", "mount": "aws", "path": "creds/deploy",
  "file": { "name": "~/.aws/credentials",
            "template": "[default]\naws_access_key_id = {{ access_key }}\naws_secret_access_key = {{ secret_key }}\n" } }
```

```ini
[default]
aws_access_key_id = AKIA…
aws_secret_access_key = …
```

The AWS CLI and SDKs read `~/.aws/credentials` by default, so nothing else is
needed. Put the reference on a variable such as `AWS_SHARED_CREDENTIALS_FILE`
and it also tells a tool where the file is.

**STS credentials, with the session token and the expiry.** `aws/sts/<role>` is
a write, so it needs `POST`. Check your server's response for the token's field
name (`bao write aws/sts/deploy ttl=1h`); recent versions return
`security_token`:

```json
{ "engine": "dynamic", "method": "POST", "mount": "aws", "path": "sts/deploy",
  "data": { "ttl": "1h" },
  "file": { "name": "~/.aws/credentials",
            "template": "[default]\naws_access_key_id = {{ access_key }}\naws_secret_access_key = {{ secret_key }}\naws_session_token = {{ security_token }}\n# expires {{ _lease.expires_at }}\n" } }
```

**A kubeconfig** from a kv-v2 secret holding `server`, `ca_data` (the CA,
already base64-encoded, as kubeconfig expects) and `token`:

```json
{ "mount": "secret", "path": "clusters/staging",
  "file": { "name": "~/.kube/config",
            "template": "apiVersion: v1\nkind: Config\nclusters:\n  - name: target\n    cluster:\n      server: {{ server }}\n      certificate-authority-data: {{ ca_data }}\nusers:\n  - name: terrapod\n    user:\n      token: {{ token | trim }}\ncontexts:\n  - name: target\n    context: {cluster: target, user: terrapod}\ncurrent-context: target\n" } }
```

**A PEM bundle whose key matches its certificate**, from one `pki/issue`:

```json
{ "engine": "dynamic", "method": "POST", "mount": "pki", "path": "issue/web",
  "data": { "common_name": "app.example.internal" },
  "file": { "name": "tls/bundle.pem",
            "template": "{{certificate}}\n{{private_key}}\n{{ca_chain|lines}}\n" } }
```

`ca_chain` is a list, so `lines` puts each certificate on its own lines. Other
variables on the same `pki/issue` reference — `TLS_CERT` with
`field: certificate`, say — read from the same issue, so they match the bundle.

#### Formats

`format` writes the whole secret, or the keys listed in `fields`, without a
template:

| `format` | Writes |
|---|---|
| `json` | The data as a JSON object, indented two spaces, with a trailing newline. Keys keep the server's order, or the order of `fields`. |
| `env` | One `KEY="value"` line per key, each ending in a newline. |

For kv-v2 "the secret" is the secret's own data — what `bao kv get` shows —
not the metadata envelope. For a dynamic engine it is the response's `data`.

```json
{ "mount": "secret", "path": "apps/db",
  "file": { "name": "db.env", "format": "env", "fields": ["DB_USER", "DB_PASS"] } }
```

`env` is **POSIX-shell syntax**: sourcing the file (`set -a; . ./db.env; set +a`)
gives each variable its exact value. Inside the double quotes, the four
characters a shell treats specially there — `\`, `"`, `$` and a backtick — are
each escaped with a backslash, and nothing else is: a newline stays a real
newline inside the quotes, so a PEM key round-trips. Every key must be a valid
environment name (`[A-Za-z_][A-Za-z0-9_]*`) and no value may contain a NUL byte;
otherwise the run fails naming the key. Non-string values are written as JSON.

Dotenv libraries do not all follow shell quoting — some leave `\$` as two
characters, or expand `${…}`. If a tool reads the file with such a library
rather than a shell, prefer a template that writes exactly the syntax it
expects.

#### Encoding

`"encoding": "base64"` decodes the reference's `field` before writing it. This
is for engines that return a file base64-encoded — GCP's dynamic service-account
keys, for example, whose `private_key_data` is the key JSON in base64:

```json
{ "engine": "dynamic", "mount": "gcp", "path": "key/deploy", "field": "private_key_data",
  "file": { "name": "gcp/adc.json", "encoding": "base64" } }
```

Wrapped (multi-line) base64 is accepted. The decoded bytes must be UTF-8 text:
**binary files are not supported yet** and are refused with a clear error. Inside
a template, use the `base64decode` filter instead; `encoding` with a template or
a format is a `422`.

#### Size

A rendered file is capped at 256 KiB, measured after the template, format or
decoding has produced it. All the OpenBao/Vault files in one run together are capped at
768 KiB, because they share the per-run Kubernetes Secret (capped at 1 MiB) with
every other variable. Going over either fails the run, naming the variable that
tipped it over and the size.

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
git credentials), `~/.terraformrc`, `~/.terraform.rc` and `~/.terraform.d`.

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
| `file` together with `hcl` | `422` — the value is a path, not an HCL expression. |
| `file` on a variable whose value source is `static` | `422` — the reference would be delivered as the literal JSON. |
| Two variables at one path, or a file where another needs a directory (`a` and `a/b`) | The run errors, naming both variables. Checked after variable-set precedence, so a workspace variable that overrides a set variable of the **same key** is one file, not a clash. |
| More than one of `field`, `file.template` and `file.format` | `422`. |
| A template syntax error or unknown filter; `fields` without `format`; `encoding` with a template or format | `422`. |
| A template name the secret does not have; invalid base64; decoded bytes that are not UTF-8 | The run errors, naming the variable and the field, never a value. |
| A file over 256 KiB after rendering, or OpenBao/Vault files over 768 KiB in one run | The run errors, giving the size. |
| Any other key inside `file` | `422`. `mode` is reserved for a later release. |

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

## Revoking leases

A dynamic secret comes with a lease, and without revocation the credential
stays valid for the role's whole TTL, even when the plan that used it
finished in a minute. Set `revoke_leases: true` on an instance and Terrapod
revokes the leases a run phase read from it once that phase is over:

```yaml
      instances:
        - name: default
          address: https://vault.internal:8200
          revoke_leases: true
```

The policy needs one more grant (shown in
[Write a policy](#3-write-a-policy--narrowly)):

```hcl
path "sys/leases/revoke" {
  capabilities = ["update"]
}
```

**One credential per phase.** Plan and apply each mint their own credential,
and each phase's leases are revoked when that phase ends. Sharing one
credential between them would mean keeping it alive between phases, which can
be days apart while a plan waits for confirmation.

**When it happens.** Once the phase's runner **Job has ended**: it succeeded,
failed, or was deleted by a cancel, a discard, or the reconciler giving up on
it. A Job whose pod failed and is being retried by Kubernetes has not ended,
and neither has a Job whose runner has only posted its plan result, since the
pod is still running then. Revocation happens within a few reconcile cycles of
the end, in a background task. It never holds up a run.

**Only for leases a Job received.** Terrapod records a phase's leases when a
runner claims the phase successfully. When a claim fails (a later read was
denied, or the server went away and the run went back to the queue), no Job
receives its credentials. Those leases are not recorded, and they expire at
their TTL as before.

**Best effort, and safe when it cannot happen.** Terrapod keeps the lease ids
in Redis while the phase runs, for up to the longest lease's TTL plus an hour.
If that record is lost, or the server cannot be reached for the revoke (the call is
retried a bounded number of times), the lease **expires at its TTL**,
exactly as it does with the option off. A revoke of a lease the server no
longer holds counts as done. None of this can fail or delay a run. With the option
off, Terrapod records nothing and makes no extra call to Redis or the server.

**Renewal is not implemented.** A lease that expires mid-phase is not
extended, so keep the role's TTL at or above your longest phase (the runner's
timeout is the upper bound).

Lease ids are never written to a log, the audit trail or the API. Each
revocation logs the run, phase and counts only.

---

## Who can read what

**Terrapod is a credential broker once this is enabled.** Anyone who can set a
workspace variable can ask it to read any path Terrapod's role can reach.

**The OpenBao/Vault policy is the access boundary — not Terrapod's RBAC.** Terrapod's
workspace permissions control who can *edit variables*; they do not constrain
*which paths* those variables may name. Scope the policy in step 3 to exactly
what your workspaces need, and prefer several instances with narrow policies
over one instance with a broad one.

As a second line, an instance may declare an allow-list of path prefixes that
Terrapod will refuse to read outside. Prefixes match on **path segments**, so
`secret/apps` permits `secret/apps/netbox` but not `secret/apps-admin`, and a
reference containing `.` or `..` segments is refused outright — otherwise the
URL Terrapod checks and the one the server receives would differ:

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
| **In the audit log** | One `vault.read` row per read — the variables, instance, mount, path, engine, phase and outcome. Never a value. See [The audit trail](#the-audit-trail). |
| **In the saved plan** | For a `terraform`-category variable, the resolved value, because Terraform stores input-variable values in its plan. See [Env, file or Terraform variable?](#env-file-or-terraform-variable). |
| **On failure** | The variable name, the instance, the coordinates and the HTTP status — never the server's response body or a partial value. |

### The audit trail

Every OpenBao/Vault read Terrapod makes writes one row to the
[audit log](api-reference.md#audit-log), whether it succeeded or not:

| Column | Value |
|---|---|
| `action` | `vault.read` |
| `origin` / `actor_type` | `system` |
| `resource_type` / `resource_id` | `runs` / `run-<id>` |
| `status_code` | `200` ok, `403` denied, `404` missing, `503` transient, `500` error |
| `detail` | JSON: `keys` (every variable the read served), `instance`, `mount`, `path`, `engine`, `phase` (`plan` or `apply`), `outcome` |

Variables that share a read are one row naming all of them, so the row count is
the number of requests the server saw — and, for a dynamic engine, the number of
credentials minted. The outcome comes from the server's answer: `denied` is a `403`,
a refused login, or Terrapod's own `paths` allow-list; `missing` is a `404`;
`transient` is the server unreachable or not answering yet (the run went back to the
queue); `error` is anything else, such as a reference refused before the
request. A read that succeeded is `ok` even if the run then failed on a field
that was not in the answer. A claim that fails before reading anything — two
variables at one file path, say — writes no row, because the server was never asked.

The rows are written in the same transaction as the claim that made the reads,
so they commit together. List them with
`GET /api/terrapod/v1/admin/audit-log?filter[action]=vault.read`.

---

## When a reference cannot be resolved

Terrapod never proceeds with the variable missing. A missing secret is worse
than a failed run — Terraform either fails somewhere confusing, or falls back to
another identity and acts with credentials nobody chose. (This differs from
private-git-module credentials, which are dropped with a warning so one bad
credential cannot fail everything.)

What happens next depends on **which kind of failure it is**, and the difference
matters when you are diagnosing one:

| The server's answer | What it means | What Terrapod does |
|---|---|---|
| `403`, `404`, other 4xx | A real answer: denied, or nothing at that path | **The run is errored**, naming the variable and the cause |
| Unreachable, DNS failure, TLS failure, timeout | The server cannot be contacted | **The run returns to `queued`** and waits for the next claim |
| `503` (sealed), `501`, `429`/`473` (standby), other 5xx | The server is up but cannot answer yet | **The run returns to `queued`** and waits |

A misconfigured reference will never resolve, so retrying it would only hide the
fault. A server that is sealed, restarting or briefly unreachable *will* answer in
a moment — and erroring every queued run in the estate for that turns a blip into
an incident someone has to clean up by hand.

**The waiting case is quiet, and you should know its shape.** A run held this way
shows no error: it simply sits in `queued` and is re-claimed periodically. If
the server never comes back — a wrong `address`, a blocked egress rule, a certificate
the API pod does not trust — the run waits indefinitely rather than failing. The
signal is in the API pod log:

```
vault is unavailable; leaving the run for a later claim
```

If runs are not starting and that line is repeating, treat it as a connectivity
problem between the API pods and the server, not as a problem with the run. See
[the runbook](runbooks.md).

For the errored case, the run carries the cause, naming the variable:

```
variable 'NETBOX_TOKEN': OpenBao/Vault denied 'secret/apps/netbox' on instance
'default'. The policy attached to role 'terrapod' does not grant read on
this path.
```

### Troubleshooting

| What you see | What it usually means |
|---|---|
| `OpenBao/Vault login failed for instance 'default' (kubernetes auth, mount 'kubernetes', role 'terrapod')` | The role does not exist, or its `bound_service_account_names` / `bound_service_account_namespaces` do not match the API pod's ServiceAccount. |
| `permission denied` on login | The server cannot call TokenReview — the `system:auth-delegator` binding in step 2 is missing. |
| `OpenBao/Vault denied '<path>' … policy attached to role` | Login worked; the policy does not grant `read` on that path. Remember the `data/` segment for kv-v2 in the *policy*. |
| `OpenBao/Vault has no secret at '<path>'` | Wrong mount or path. Note the reference omits `data/` while the policy includes it. |
| `field '<x>' is not present at '<path>' (available: …)` | Right secret, wrong key. The message lists what is there. |
| `path '<x>' is not in the allow-list configured for vault instance` | Terrapod's own `paths` allow-list refused it before contacting the server. |
| `could not read the ServiceAccount token` | Kubernetes auth outside a cluster. Use `approle` or `token` instead. |
| `OpenBao/Vault login failed … (jwt auth, mount 'jwt', role 'terrapod', audience 'vault')` | The role does not exist; its `bound_audiences` lacks the audience shown; its `bound_subject` does not match `system:serviceaccount:<namespace>:<ServiceAccount>`; or the server cannot verify the signature (a wrong or unreachable `oidc_discovery_url`, or stale `jwt_validation_pubkeys`). |
| `could not read the projected ServiceAccount token` | The chart has not projected it — `vault.enabled` is false — or `auth.token_path` points somewhere with no file. |
| `could not read the CA file` | The `tls.ca_secret` Secret, or its `tls.ca_key` key, does not exist. |
| `not a usable PEM certificate bundle` | `tls.ca_key` names a key that does not hold a PEM certificate. |
| Runs sit in `queued` and the log shows a TLS verification failure | The server's certificate is not trusted. Add its CA through `tls.ca_secret`, or through the global `caBundle`. |
| `references unknown vault instance '<name>'` | The reference names an instance that is not in `instances`. |
| `omits 'vault' but several instances are configured` | Mark one `default: true`, or name the instance in the reference. |

---

## Diagnostics

Two tools answer "why won't my OpenBao/Vault variable resolve?" without queueing a run,
and without ever minting a credential or returning a value.

### Instance status

`GET /api/terrapod/v1/admin/vault` reports on each configured instance. You can
also read it on the **OpenBao/Vault status** page (`/admin/vault`, in the Admin menu),
or through the MCP tool `terrapod_vault_status`. It needs the `admin` or
`audit` role.

| Field | Meaning |
|---|---|
| `reachable` | Terrapod got an HTTP answer from `sys/health`, so the address, DNS, network path and TLS all work |
| `initialized`, `sealed`, `standby`, `version` | What `sys/health` says. A sealed server answers every read with 503, and runs wait in `queued` while it stays sealed |
| `login-ok`, `login-error` | Whether Terrapod can log in with the instance's configured method: `kubernetes`, `jwt`, `approle` or `token` |
| `ttl-seconds` | The remaining TTL of Terrapod's token, read from `auth/token/lookup-self` |
| `tls-trust` | `instance-ca` (verified against `tls.ca_secret` alone), `global-bundle` (the chart's `caBundle`), `default` (the system store), or `skip-verify` |
| `last-error` | The last failed resolution against this instance, from any run: `{class, message, at}`, with names and causes only. `VaultUnavailable` means runs are waiting, `VaultDenied` a refused login or policy, `VaultNotFound` a wrong path |
| `checked-at` | When this instance was last sampled |

**Unknown is `null`, never `false`.** An instance that has not been sampled yet
shows every probe field as null. So does a login that was not attempted, because
a sealed server is never logged in to.

The status is **sampled, not live.** A scheduler task, `vault_status`, runs
every 60 seconds on one API replica and writes the result to Redis, and every
replica answers the endpoint from that sample. Opening the page never contacts
the server. The task is registered only when `vault.enabled` is true. With the
value source off the endpoint returns an empty list with `meta.vault.enabled: false`.

For `kubernetes`, `jwt` and `approle`, the login call itself proves the login
works. If `lookup-self` is then refused, only the TTL is lost; that happens when
a role's policy omits the server's built-in `default` policy. For a static `token` there is no
login call, so a refused `lookup-self` is reported as a failed login.

### Checking a reference

`POST /api/terrapod/v1/workspaces/{id}/vault-reference-checks` checks a
reference without resolving it. So does the **Check** button on the OpenBao/Vault
reference form, and the MCP tool `terrapod_vault_reference_check`. The body
carries either a reference or a stored variable:

```json
{"data": {"type": "vault-reference-checks",
  "attributes": {"reference": {"mount": "secret", "path": "apps/netbox", "field": "apitoken"}}}}
```

```json
{"data": {"type": "vault-reference-checks", "attributes": {"variable-id": "var-…"}}}
```

It runs these checks in order and stops at the first failure:

| Check | What it does |
|---|---|
| `parses` | The reference and its `file` block pass the same validation a variable write applies |
| `instance` | The named instance exists; or, when `vault` is omitted, a default can be chosen |
| `path-allowed` | The path is inside the instance's `paths` allow-list, and has no traversal |
| `readable` | Asks the server with `sys/capabilities-self` whether Terrapod's token has `read` on the policy path (`update` or `create` for a dynamic `POST`). This reads nothing at the path. For kv-v2 the path checked, `read-path`, includes the `data/` segment the reference leaves out, because that is the path the policy has to grant |
| `fields-present` | **kv-v2 only.** Reads the secret to list its key **names** in `keys`, and names each field the reference needs but is missing in `missing-fields`. Those fields are `field`, every tag in a `template`, and a format's `fields` |

A check's `status` is `pass`, `fail`, `skipped` or `unknown`. `unknown` means
the server could not answer (it is sealed or unreachable), not that the check
failed.

**A dynamic engine is never read.** Each read of `database/creds`,
`aws/creds`, `pki/issue` and the like mints a credential, so a check that read
one would be a resolution under another name. For those, `readable` is the
strongest answer available: `keys` stays null and the note `dynamic-not-read`
says so.

Other notes a check can carry:

| Note | Meaning |
|---|---|
| `keys-need-plan-permission` | The caller has `var:write` but not `run:plan` on the workspace, so key names are withheld |
| `local-execution` | The workspace runs locally, where an OpenBao/Vault reference never resolves |
| `vault-disabled` | The value source is off |

**Who may check.** A workspace check needs `var:write` on the workspace, the
permission it takes to create the variable. Listing a kv-v2 secret's key names
also needs `run:plan`: someone who can create the variable and run a plan with
it could have the secret delivered to a run anyway, so the names give them
nothing more. A variable-set check
(`POST /api/terrapod/v1/varsets/{id}/vault-reference-checks`) needs platform
admin, as writing a variable-set variable does. Checks are limited to 20 a
minute per user, answered with `429` and `Retry-After` beyond that. The request
itself is recorded in the audit log like any other API call.

---

## Limits

- **Agent execution only.** The reference is resolved server-side when a runner
  claims the run, so a local-execution workspace has no point at which it could
  happen. Creating an OpenBao/Vault-sourced **workspace** variable on one is refused
  rather than silently resolving to nothing, and switching a workspace that
  holds one to local execution is refused for the same reason.

  A **variable set** is checked at the points where the pairing can actually be
  created, since a set is not bound to a workspace and there is no single write
  to test. All three are refused: assigning a workspace to a set that carries an
  OpenBao/Vault reference, writing such a reference into a set that already reaches a
  local-execution workspace, and switching a workspace to local while a set
  delivers one to it (the workspace's own variables and the set's are counted
  together).

  One gap remains by design: a set assigned by an **assignment rule** whose match
  set widens later — a label edit, or a new local workspace matching the rule —
  is not a write against either side, so there is nothing to refuse at. On such a
  workspace the reference resolves to nothing while agent-mode workspaces in the
  same set are unaffected. Prefer agent execution for any workspace an
  OpenBao/Vault-bearing set can reach.
- **Leases are not renewed.** A dynamic credential is minted per phase. It is
  revoked when the phase's Job ends only with
  [`revoke_leases`](#revoking-leases) on; otherwise it is left to expire. Set
  the role's TTL to cover your longest phase either way.
- **Files are text.** A file's content, after any template, format or base64
  decoding, must be UTF-8 text; binary files need a wire change and are not
  supported yet. A file's permissions are fixed (see
  [Permissions](#permissions)); `mode` is reserved in the `file` object for that.
- **`approle` and `token` auth** work but are less well trodden than
  `kubernetes` and `jwt`, which need no stored credential. See
  [Other auth methods](#other-auth-methods-and-namespaces).
