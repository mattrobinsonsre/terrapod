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
| `namespace` | Vault Enterprise namespace. Omit for OSS. |
| `auth.method` | `kubernetes` (default), `approle`, or `token`. |
| `auth.mount` | The auth mount path you enabled. |
| `auth.role` | The Vault role bound to Terrapod's ServiceAccount. |
| `paths` | Optional allow-list of path prefixes. See below. |
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

Put it in a `terrapod_variable_set` instead to define it once and apply it to
many workspaces — with an [assignment rule](api-reference.md#assignment-rules)
it can target them by label rather than one by one.

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
| **On failure** | The variable name, the instance, the coordinates and the HTTP status — never Vault's response body or a partial value. |

---

## When a reference cannot be resolved

**The run fails.** It does not proceed with the variable missing.

This is deliberate, and differs from how private-git-module credentials behave:
those are dropped with a warning so one bad credential cannot fail everything.
A missing secret is worse than a failed run — Terraform either fails somewhere
confusing, or falls back to another identity and acts with credentials nobody
chose.

The run is errored with the cause, naming the variable:

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
| `references unknown vault instance '<name>'` | The reference names an instance that is not in `instances`. |
| `omits 'vault' but several instances are configured` | Mark one `default: true`, or name the instance in the reference. |

---

## Limits

- **Agent execution only.** The reference is resolved server-side when a runner
  claims the run, so a local-execution workspace has no point at which it could
  happen. Creating a Vault-sourced variable on one is refused rather than
  silently resolving to nothing.
- **Leases are not renewed or revoked.** A dynamic credential is minted per run
  and left to expire. Set the Vault role's TTL to suit your run durations.
- **No file materialization yet.** Values are delivered as environment or
  Terraform variables. A provider that insists on reading a credential from a
  file path still needs the sidecar.
- **`approle` and `token` auth** work but are less well trodden than
  `kubernetes`, which needs no stored credential. Supply the secret_id or token
  with `existingSecret` on the instance:

  ```yaml
        - name: prod-vault
          address: https://vault.internal:8200
          auth:
            method: approle
            mount: approle
            role: <role-id>        # AppRole role_id
          existingSecret: my-vault-approle
          existingSecretKey: secret_id    # defaults to "secret"
  ```

  The chart injects it as `TERRAPOD_VAULT_<NAME>_SECRET` via `secretKeyRef`,
  never through the ConfigMap. Kubernetes auth ignores all of this.
