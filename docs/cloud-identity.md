# Per-workspace cloud identity (OIDC federation)

Terrapod can act as an **OIDC identity provider for its own runs**. A run gets a
short-lived JWT whose claims say which workspace it belongs to and which phase it
is in, and your cloud federates to Terrapod to exchange that token for its own
credentials. Two workspaces on the same agent pool can then hold different cloud
permissions without standing up a second listener.

> ## What Terrapod does, and it is all it does
>
> **Terrapod mints a short-lived RS256 JWT describing the run and writes it to a
> file at `/var/run/terrapod/oidc/token`.** That is the whole mechanism.
>
> Which cloud consumes that token, and how, is **your provider configuration**.
> There is no cloud-specific code anywhere in this feature — no role ARN, no
> tenant id, no credential-config JSON, no per-cloud environment variable.
> Terrapod does not know AWS from Azure.
>
> That is not a gap to be filled later. It is exactly why **one** mechanism
> serves AWS, Azure, GCP, OpenBao (or HashiCorp Vault) JWT auth, and anything
> else that federates to an OIDC issuer. The price is that there is **no
> zero-configuration path**: every target needs a provider block you write
> yourself, naming a role or client id Terrapod never learns.

---

## The problem this solves

The runner ServiceAccount is **global per listener**. [Cloud
credentials](cloud-credentials.md#runner-serviceaccount) gives two sources for it
— the Helm-configured `runners.serviceAccount.name`, or the namespace default —
so every run that lands on an agent pool authenticates to the cloud as that pool.
The credential boundary is the pool, not the workspace. Three consequences:

- **Workspaces on one pool are interchangeable to the cloud.** A workspace that
  should only touch one account's DNS can reach whatever the pool's role can,
  because the pool's role is what it presents.
- **Cloud audit logs name the ServiceAccount, not the workspace.** The run is
  attributable inside Terrapod and anonymous outside it — the wrong way round,
  because outside is where the permissions are.
- **Segmentation is priced at a whole Deployment.** The existing advice is a
  listener per boundary, each with its own ServiceAccount. It works, and almost
  nobody does it, so workspaces share a pool and therefore share an identity.

With federation the identity travels with the *run*, as a claim about the
workspace, so which runner executed it grants nothing.

---

## Fall-through, not replacement — permanently

**A workspace that names no audiences runs as the agent pool's ServiceAccount,
exactly as it does today.** Nothing changes for it. There is no migration, no
cliff and no deprecation: keep assigning permissions to your runner pods and runs
fall through to them.

**Partial adoption is the expected posture**, not a stage on the way to
something else. Give an identity to the few workspaces that need segmentation and
leave the pool to serve everything else. Read this feature as *"the credential
boundary is the workspace where one is set, and the pool otherwise"*.

This extends the precedence table in
[cloud-credentials.md](cloud-credentials.md#runner-serviceaccount) rather than
replacing it:

| Priority | Source | Configured via |
|---|---|---|
| 1 | **The workspace's own federated identity** | `oidc-audiences` on the workspace, plus your own provider block (this page) |
| 2 | **Global runner SA** | `runners.serviceAccount.name` in Helm values |
| 3 | **K8s default SA** | Implicit namespace default |

The runner pod keeps the pool's ServiceAccount in **every** case, including when
a workspace is federated. The federated credential is established inside the
container before `init`, so it layers over the pod's own identity rather than
replacing it — which is why an IRSA-annotated pool and a federated workspace
coexist without either having to know about the other.

---

## Turning it on

Three things, and all three are required. Two are the operator's and one is the
workspace admin's; **neither implies the other**, and a published issuer with no
workspace opted in grants nothing.

### 1. Publish the issuer

```yaml
api:
  config:
    auth:
      oidc_issuer:
        enabled: true
        # The issuer URL, exactly as your cloud will be configured with it.
        # Leave empty to derive it from webhookIngress.hostname, falling back
        # to external_url.
        public_url: "https://terrapod-webhooks.example.com"
```

Off by default, and **off means the two issuer routes are not mounted at all**
rather than mounted and refusing. A deployment that has not opted in publishes no
trust root, which is a stronger statement than a 404 on a path that exists.

`public_url` is one setting for three things that have to agree — the token's
`iss`, the discovery document's `issuer`, and its `jwks_uri`. OIDC issuer
matching is exact, so if they diverge every token is rejected at exchange time,
inside the cloud, with nothing wrong on Terrapod's side to look at.

### 2. Make the two issuer paths publicly reachable

```yaml
webhookIngress:
  enabled: true
  className: nginx
  hostname: terrapod-webhooks.example.com
  tls: true
  paths:
    - /api/terrapod/v1/vcs-events          # (chart default)
    - /api/terrapod/v1/task-stage-results  # (chart default)
    - /.well-known/openid-configuration    # add for OIDC federation
    - /.well-known/jwks.json               # add for OIDC federation
```

Both paths are **unauthenticated by necessity, not by oversight**: a cloud
fetches them anonymously, before any token exists, to decide whether to trust
one. They publish only public key material and the issuer's own URL.

The two issuer paths ship **commented out** in the chart's default `paths` list,
beside an explanation — uncomment them at the same time as
`api.config.auth.oidc_issuer.enabled`. List them **exactly**, not as a
`/.well-known` prefix: the prefix would bring the Terraform service-discovery
document along with it, which is harmless but should be a decision rather than a
side effect.

> **`webhookIngress` is misleadingly named for this purpose.** It is *the public
> surface*; webhooks are one use of it. If you poll VCS and want nothing
> webhook-shaped exposed, enabling it with **only** the two issuer paths and
> neither webhook path is a first-class configuration, not a corner:
>
> ```yaml
> webhookIngress:
>   enabled: true
>   className: nginx
>   hostname: terrapod-webhooks.example.com
>   tls: true
>   paths:
>     - /.well-known/openid-configuration
>     - /.well-known/jwks.json
> ```
>
> The issuer deliberately rides this Ingress rather than getting one of its own,
> because its `paths` allow-list is already the right granularity.

> **A tailnet-only or internal-LB-only deployment cannot use this feature.** The
> clouds fetch the discovery document and JWKS from the public internet, so there
> has to be a publicly reachable hostname. See [recipe B in
> deployment-network-isolation.md](deployment-network-isolation.md#b-internal-lb-for-everyone-no-public-surface),
> which is mutually exclusive with OIDC federation.

Verify both from outside your network:

```sh
curl -s https://terrapod-webhooks.example.com/.well-known/openid-configuration | jq .issuer
curl -s https://terrapod-webhooks.example.com/.well-known/jwks.json | jq '.keys[].kid'
```

The `issuer` value has to equal what you configure in the cloud, character for
character, including the absence of a trailing slash.

### 3. Give the workspace its audiences

The audience list **is** the opt-in. Empty means the workspace mints nothing.

```hcl
resource "terrapod_workspace" "dns" {
  name           = "prod-dns"
  oidc_audiences = ["sts.amazonaws.com"]
}
```

Or `PATCH /api/terrapod/v1/workspaces/{id}` with
`{"data": {"attributes": {"oidc-audiences": ["sts.amazonaws.com"]}}}`, or the
workspace's Configuration tab in the UI.

The audience cannot be a deployment-wide constant, because every federation
target names its own — `sts.amazonaws.com`, `api://AzureADTokenExchange`,
whatever a JWT auth role's `bound_audiences` says. At most **10** entries, each
at most **255** characters, stored byte-for-byte: an audience is an opaque string
the target chose, so Terrapod rejects what it cannot accept and normalises
nothing. A blank entry is refused rather than dropped, because a silently
dropped one means an operator who believes they granted an audience did not.

**Prefer one audience per workspace.** A token audienced for two targets is
replayable between them: either target will accept a token minted for the other.

---

## What the run actually gets

Before `init`, the runner asks the API for a token and writes it to a file, then
exports three cloud-neutral environment variables:

| | What it is | |
|---|---|---|
| `/var/run/terrapod/oidc/token` | The JWT itself, mode `0600` | **file** |
| `TERRAPOD_OIDC_TOKEN_FILE` | That same path, for a configuration that would rather not hard-code it | env |
| `TERRAPOD_RUN_PHASE` | `plan` or `apply` | env |
| `TF_VAR_terrapod_run_phase` | The same value as a Terraform variable — declare `variable "terrapod_run_phase" {}` and it arrives with no wiring | env |

One fixed path for every target, deliberately: there is nothing per-cloud about
it, so an operator can paste it.

**Terrapod sets no per-cloud environment variable.** Not
`AWS_WEB_IDENTITY_TOKEN_FILE`, not `AWS_ROLE_ARN`, not
`ARM_OIDC_TOKEN_FILE_PATH`, not `GOOGLE_APPLICATION_CREDENTIALS`. It could only
set all of them blindly or none, and the answer is none — the same decision that
lets this one token serve OpenBao/Vault as readily as a cloud.

The token's contents never reach a log line. The runner streams stdout verbatim,
and a JWT in a log is a credential in a log.

---

## Per-provider configuration

**This is the half Terrapod does not do, so it is the half you write.** Every
block below needs a value Terrapod never learns — a role ARN, a client id, an
audience — because nothing in the platform knows which target it is talking to.

| Target | Provider configuration | Audience to set on the workspace |
|---|---|---|
| **AWS** | `assume_role_with_web_identity` block: `role_arn`, `web_identity_token_file`, optional `session_name` | `sts.amazonaws.com` |
| **Azure** | `use_oidc = true` plus `oidc_token_file_path`, with `client_id` and `tenant_id` | `api://AzureADTokenExchange` |
| **GCP** | `external_credentials` block: `audience`, `service_account_email`, `identity_token` — a **value**, so read the file with `file()` | The workload identity pool provider's audience |
| **OpenBao/Vault** | A JWT auth role whose `bound_audiences` matches | Whatever that role's `bound_audiences` says |

### AWS

```hcl
provider "aws" {
  region = "eu-west-1"

  assume_role_with_web_identity {
    role_arn                = "arn:aws:iam::123456789012:role/terrapod-prod-dns"
    session_name            = "terrapod"
    web_identity_token_file = "/var/run/terrapod/oidc/token"
  }
}
```

Workspace audience: `["sts.amazonaws.com"]`.

Register the issuer as an IAM OIDC identity provider first (`aws iam
create-open-id-connect-provider`), with the issuer URL from step 1 and
`sts.amazonaws.com` as a client id. The role's trust policy then conditions on
the claims below:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/terrapod-webhooks.example.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "terrapod-webhooks.example.com:aud": "sts.amazonaws.com",
          "terrapod-webhooks.example.com:sub": "workspace:prod-dns:phase:apply"
        }
      }
    }
  ]
}
```

`AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` are the environment equivalents
of the two fields above, should you prefer to set them as workspace
`env`-category variables instead of writing the block. If you set both, the block
wins — the provider documents that *"values configured in the
`assume_role_with_web_identity` block take precedence over environment variables
for both token sources"*. Terrapod sets neither, so there is nothing of ours for
your own configuration to have to beat.

### Azure

```hcl
provider "azurerm" {
  features {}

  use_oidc             = true
  oidc_token_file_path = "/var/run/terrapod/oidc/token"

  client_id       = "00000000-0000-0000-0000-000000000000"
  tenant_id       = "11111111-1111-1111-1111-111111111111"
  subscription_id = "22222222-2222-2222-2222-222222222222"
}
```

Workspace audience: `["api://AzureADTokenExchange"]`. The environment
equivalents are `ARM_USE_OIDC`, `ARM_OIDC_TOKEN_FILE_PATH`, `ARM_CLIENT_ID` and
`ARM_TENANT_ID`.

Configure a **federated identity credential** on the app registration or managed
identity, with Terrapod's issuer URL and the `sub` value from the claim table
below.

> **The azurerm provider reads the token file exactly once.** Its documentation
> states: *"The OIDC token will only be read once from the file at
> `oidc_token_file_path`. When the Azure access token expires, the provider will
> not read the OIDC token again from this file."*
>
> This is normally invisible: the provider reads the file at init and exchanges
> it immediately, well inside the 15-minute default lifetime. What it means is
> that `token_ttl_seconds` is the window in which the **exchange** has to happen,
> not a cap on how long the run may take. If an Azure run ever reports an expired
> assertion at provider init, raising `token_ttl_seconds` is the lever; it does
> nothing for a long apply, because the Azure access token that results refreshes
> on Azure's own terms.

### GCP

```hcl
provider "google" {
  project = "my-project"

  external_credentials {
    audience              = "//iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/terrapod/providers/terrapod"
    service_account_email = "terrapod-prod-dns@my-project.iam.gserviceaccount.com"
    identity_token        = file("/var/run/terrapod/oidc/token")
  }
}
```

`identity_token` takes the token **value**, not a path — which is why it is read
with `file()`. Set the workspace's audience to the same string you put in
`audience` here; it is the audience your workload identity pool provider expects,
which `gcloud iam workload-identity-pools providers describe` reports.

Create the pool provider with Terrapod's issuer URL and map the claims:

```sh
gcloud iam workload-identity-pools providers create-oidc terrapod \
  --location=global --workload-identity-pool=terrapod \
  --issuer-uri="https://terrapod-webhooks.example.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.workspace=assertion.workspace,attribute.phase=assertion.phase"
```

GCP can read arbitrary claims, so condition on the discrete `workspace` and
`phase` claims rather than parsing `sub`.

The alternative to the provider block is `GOOGLE_APPLICATION_CREDENTIALS`
pointing at an [external-account credential-config
JSON](https://cloud.google.com/iam/docs/workload-identity-federation) whose
`credential_source.file` is `/var/run/terrapod/oidc/token`. You supply that file
yourself — in a custom runner image, or written by a [`pre_init` execution
hook](execution-hooks.md) — because Terrapod generates credential-config JSON for
no target.

### OpenBao (or HashiCorp Vault)

The same token authenticates against a JWT auth role. Nothing cloud-shaped is
involved, which is the point: this is the identity half, and it works for any
OIDC-federating target.

```sh
bao auth enable jwt   # or: vault auth enable jwt

bao write auth/jwt/config \
  oidc_discovery_url="https://terrapod-webhooks.example.com"

bao write auth/jwt/role/prod-dns \
  role_type="jwt" \
  user_claim="sub" \
  bound_audiences="terrapod" \
  bound_claims_type="string" \
  bound_claims='{"workspace":"prod-dns","phase":"apply"}' \
  token_policies="prod-dns"
```

Set the workspace's audience to match `bound_audiences` — here `["terrapod"]`.
The run then logs in with the token file and gets a short-lived OpenBao/Vault
token scoped to that workspace:

```hcl
provider "vault" {
  auth_login_jwt {
    role = "prod-dns"
    jwt  = file("/var/run/terrapod/oidc/token")
  }
}
```

**This is a different mechanism from Terrapod's OpenBao/Vault [variable value
source](vault.md), and it answers a different question.** There, Terrapod reads a
secret server-side on the workspace's behalf using **one deployment-wide
identity** — which is why that page says the server's own policy, not Terrapod's
RBAC, is the access boundary, and that anyone who can set a workspace variable
can ask Terrapod to read any path that role reaches. Here the **run**
authenticates as itself, so the policy can be scoped per workspace with
`bound_claims`. Use the value source for convenience, and this where a
credential's blast radius has to be the workspace.

It also narrows the per-run credential problem described in
[cloud-credentials.md](cloud-credentials.md#per-run-per-workspace-cloud-credentials--the-three-mechanisms):
a [`pre_init` execution hook](execution-hooks.md) fetching a dynamic credential
previously had to log in with a credential shared by every workspace, so the role
it bound to could be no narrower than the pool. Now the login is itself
per-workspace.

---

## The `phase` claim, and why write permissions belong behind it

Every token claims the phase it was minted for: `plan` or `apply`.

**The phase comes from the runner's own phase-bound token, never from a field in
the request.** A plan-phase runner asking for the apply identity is the thing
this guards.

That makes one arrangement worth reaching for:

> **Put write permissions behind a trust condition on `phase: apply`, and a
> speculative pull-request plan structurally cannot assume that role** — because
> every PR-driven run in Terrapod is plan-only, so it is handed a token claiming
> `phase: plan` and nothing else.

Apply is only ever *extra* permissions, never different ones, so the simple
arrangement is one role covering both phases, and the segmented one is a second
role whose trust policy requires `phase: apply`.

This narrows the blast radius of a fork pull request at the credential layer. It
**does not replace the fork-PR gate**
([`allow-fork-pr-plans`](vcs-integration.md#pull-requests-from-forks), off by
default, [GHSA-gp5w-76rw-c452](https://github.com/mattrobinsonsre/terrapod/security/advisories/GHSA-gp5w-76rw-c452)):
a plan still runs the author's code with the workspace's `env` variables,
sensitive values and OpenBao/Vault-resolved values. What it removes is that
code's ability to **write** to your cloud.

### Switching on the phase inside HCL

A single provider block can choose its own role from the phase, using the
variable the runner delivers:

```hcl
variable "terrapod_run_phase" {
  type    = string
  default = "plan"
}

provider "aws" {
  assume_role_with_web_identity {
    role_arn                = var.terrapod_run_phase == "apply" ? var.apply_role_arn : var.plan_role_arn
    web_identity_token_file = "/var/run/terrapod/oidc/token"
  }
}
```

This exists because HCL otherwise cannot see which phase it is in, so the apply
increment would not be expressible at all. **One role whose trust policy
conditions on the `phase` claim is simpler and usually better** — the cloud does
the switching, and the configuration carries one role ARN. Reach for the variable
when you need two genuinely distinct roles, for instance in two different
accounts.

---

## The claim set

| Claim | Example | Notes |
|---|---|---|
| `iss` | `https://terrapod-webhooks.example.com` | Matched **exactly** by the cloud |
| `sub` | `workspace:prod-dns:phase:apply` | Composite, colon-delimited, phase last |
| `aud` | `["sts.amazonaws.com"]` | The workspace's audience list, verbatim |
| `workspace` | `prod-dns` | The workspace name |
| `workspace_id` | `0f8b…` | The id — stable across a rename, where the name is readable |
| `phase` | `apply` | `plan` or `apply` |
| `run_id` | `3c71…` | The run, for correlating a cloud audit entry back to Terrapod |
| `terrapod_organization` | `default` | Always the literal `default` — Terrapod is single-organization |
| `iat`, `nbf`, `exp`, `jti` | | Standard. `jti` is what makes two tokens for one run distinguishable in a cloud audit log |

### Why `sub` repeats what the discrete claims already say

The redundancy is load-bearing, not an oversight:

**Azure federated identity credentials match on issuer, subject and audience
only — with no access to arbitrary claims.** So `sub` is the one place a phase
condition can be expressed on Azure, which is why the phase is embedded in it as
well as claimed discretely.

**Targets that can read arbitrary claims should condition on the discrete claims
instead**, which needs no wildcard and no string parsing. AWS, GCP and
OpenBao/Vault can all do this, and the examples above do.

Azure's "flexible federated identity credentials" would allow claim expressions,
but they are gated to a hardcoded allow-list of issuers (GitHub, GitLab and
`app.terraform.io`) and are not supported by the Terraform providers in either
direction. They are therefore **not available to a self-hosted issuer**, and
there is no configuration that makes them available.

Two things are deliberately absent from the claim set: **workspace labels**, and
anything identifying the user who queued the run. Labels are mutable by the
workspace's own owner, so a trust policy conditioned on one would not be the
boundary it appeared to be.

---

## Key management and rotation

Terrapod signs with **RS256 over a 2048-bit RSA key**. Not the listener CA's
Ed25519: at least one major cloud does not accept EdDSA for workload identity
federation, and a federation path that works on two clouds out of three is not
worth the elegance. A non-RSA key is refused by name at startup rather than
published as a JWKS the clouds silently cannot use.

**By default the key is generated on first startup and persisted in the
database**, the same model as the listener CA — one source of truth, loaded on
every startup, with the check-then-create serialised across replicas by a
Postgres advisory lock. It is never generated by the Helm chart: under `helm
template`, which is what Argo CD and Flux run, a `lookup` returns nothing and a
generating branch would re-mint on every render, breaking every federated
workspace in the deployment at once.

**An operator-supplied key wins on every startup and is never stored.** It is key
material, so it arrives from a Secret by `secretKeyRef` and never touches a
ConfigMap:

```yaml
api:
  oidcSigningKey:
    existingSecret: terrapod-oidc-signing-key
    existingSecretKey: key.pem        # an RSA private key, PKCS8 PEM
```

```sh
kubectl -n terrapod create secret generic terrapod-oidc-signing-key \
  --from-file=key.pem=./oidc-signing-key.pem
```

Rotating a supplied key therefore means **replacing the Secret and restarting the
API** — Terrapod will not rotate it for you, and the rotate endpoint returns
`409` on such a deployment. Storing a copy would mean the first value ever
supplied won for ever and every later rotation was silently ignored, so a BYO
deployment never writes to the key table at all.

### Rotation adds a key rather than swapping one

A published trust root cannot be swapped atomically, because the clouds fetch the
JWKS on their own schedule and cache it. So a rotation **adds**:

```sh
curl -sX POST -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  https://terrapod.example.com/api/terrapod/v1/oidc/signing-keys/actions/rotate
```

Both windows then overlap, and between them nothing is ever signed with a key a
cloud has not had a chance to fetch:

| | |
|---|---|
| The new key is **published immediately** | and starts signing only after `key_propagation_seconds` (default 600). **Until then the retired key keeps signing** — it is still in the published JWKS, so its tokens verify, whereas the incoming key is precisely the one the clouds lack |
| The old key is **retired immediately** | but stays published for `retired_key_grace_seconds` (default 3600), because the tokens it already signed are still inside their own lifetime |

`retired_key_grace_seconds` has to exceed `token_ttl_seconds`, or a token signed
moments before a rotation stops verifying while still inside its own lifetime. It
also has to exceed `key_propagation_seconds`, since the retired key is what
carries the signing load across that window.

Both are configuration because only you know how long your clouds cache. A
rotation therefore needs no downtime and no coordination with the clouds: run it,
and the handover happens on its own.

Inspect the set at any time:

```sh
curl -s -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  https://terrapod.example.com/api/terrapod/v1/oidc/signing-keys | jq .
```

`kid` is an RFC 7638 JWK thumbprint, derived from the key rather than assigned —
so it is stable across restarts, cannot collide, and is the same value a cloud
sees in a token header.

The step-by-step procedure is in [runbooks.md → Rotating the OIDC issuer signing
key](runbooks.md#rotating-the-oidc-issuer-signing-key).

### Configuration reference

| Key (under `api.config.auth.oidc_issuer`) | Default | What it does |
|---|---|---|
| `enabled` | `false` | Publish the discovery document and JWKS. Off means the routes are not mounted |
| `public_url` | `""` | The issuer URL, exactly as the cloud is configured with it. Empty derives it from `webhookIngress.hostname`, falling back to `external_url` |
| `token_ttl_seconds` | `900` | Token lifetime, 60–43200. Short because the target exchanges it immediately |
| `key_propagation_seconds` | `600` | How long a rotated-in key is published before it starts signing. The retired key signs across this window |
| `retired_key_grace_seconds` | `3600` | How long a retired key stays published. Must exceed both `token_ttl_seconds` and `key_propagation_seconds` |

The signing key is not among these, because it is key material: it is supplied as
`api.oidcSigningKey.existingSecret` / `existingSecretKey` and injected by
`secretKeyRef`, never rendered into a ConfigMap.

---

## The honest limitation: a runner image that predates this feature

**A runner image older than this feature never asks for a token.** A workspace
that names audiences then runs as the agent pool's identity anyway — silently,
because from the API's side nothing was asked for and nothing was refused.

**Terrapod cannot detect this.** No runner-image version is reported to the API,
so there is no server-side check to add. It is a documented, managed degradation:
**upgrade your runner images** when you adopt this, and treat a federated
workspace on a stale pool as mis-scoped until you have.

It fails in the safe direction in one sense — the run authenticates as the pool,
which is what it did before — and in the dangerous direction in another: a
workspace you deliberately moved off the pool's broad permissions is still using
them.

Distinguish it from the case where the runner **does** know about the feature:

| Situation | What happens |
|---|---|
| Workspace names no audiences | The mint returns `204`. The runner takes no action and the run uses the pool's identity. **Normal, and permanent** |
| Issuer not enabled deployment-wide | Also `204`, and the same outcome — an operator who has not published an issuer has not opted this deployment in |
| Workspace names audiences, mint **fails** | **The run fails.** Credentials were asked for and could not be had, and continuing would mean silently running under broader permissions than you chose |
| Runner image predates the feature | Nothing is asked for. The run uses the pool's identity and reports success. **This is the gap above** |

A token minted before the phase claim existed carries no phase, which is read as
"makes no claim": the JWT then carries no `phase` either, so a trust policy
conditioning on it simply does not match. That refuses the credential rather than
quietly widening it.

---

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /.well-known/openid-configuration` | **None** | Discovery document. Mounted only when enabled |
| `GET /.well-known/jwks.json` | **None** | The published signing keys |
| `POST /api/terrapod/v1/runs/{run_id}/cloud-identity-token` | Runner token, scoped to that run | Mint this run's token. `204` when the workspace mints nothing |
| `GET /api/terrapod/v1/oidc/signing-keys` | Platform admin | What is published, and which key signs. Public key material only |
| `POST /api/terrapod/v1/oidc/signing-keys/actions/rotate` | Platform admin | Add a key, retire the current one |

The discovery document is deliberately minimal. Terrapod is not an OAuth
authorization server for these tokens: there is no authorization endpoint, no
token endpoint and no client registration, because the only consumer is a target
validating a token Terrapod already minted.

---

## See Also

- [Cloud Credentials](cloud-credentials.md) — workload identity for the pool and the platform itself; the fall-through this layers over
- [OpenBao/Vault](vault.md) — the variable value source, and why its access boundary is deployment-wide
- [Execution Hooks](execution-hooks.md) — the `pre_init` slot, for a credential the run fetches itself
- [Network Isolation](deployment-network-isolation.md) — why an internal-only deployment cannot publish an issuer
- [VCS Integration → Pull requests from forks](vcs-integration.md#pull-requests-from-forks) — the gate the `phase` claim complements but does not replace
- [Runbooks → Rotating the OIDC issuer signing key](runbooks.md#rotating-the-oidc-issuer-signing-key)
- [API Reference](api-reference.md#per-workspace-cloud-identity-oidc-federation) — the `oidc-audiences` attribute and the endpoints above
