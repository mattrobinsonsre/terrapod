# Per-workspace cloud identity (OIDC federation)

Terrapod can act as an **OIDC identity provider for its own runs**. A run gets
short-lived JWTs whose claims say which workspace it belongs to and which phase
it is in, and your cloud federates to Terrapod to exchange one for its own
credentials. Two workspaces on the same agent pool can then hold different cloud
permissions without standing up a second listener.

> ## What Terrapod does, and it is all it does
>
> **Terrapod mints a short-lived RS256 JWT per provider configuration and writes
> each one to its own file at `/var/run/terrapod/oidc/<target>/token`, mode
> `0600`.** That is the whole mechanism.
>
> Which cloud consumes a token, and how, is **your provider configuration**.
> There is no cloud-specific code anywhere in this feature — no role ARN, no
> tenant id, no credential-config JSON, no per-cloud environment variable, and
> no built-in `sts.amazonaws.com`. Terrapod does not know AWS from Azure.
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

## The trade: network isolation for fine-grained authorization

**Enabling the issuer means two paths must be reachable from the public
internet.** This is a deliberate trade, not an oversight, and it is the one
decision to make before anything else on this page:

> **Network isolation is traded here for fine-grained auth. That is the choice.**

A cloud fetches the discovery document and the signing keys **anonymously,
before any token exists** — it has to, because that fetch is how it decides
whether to trust a token at all. There is no credential to hand it and no
private path to route it over. So a deployment that keeps its management plane
private still has to expose these two paths:

```
/.well-known/openid-configuration
/.well-known/jwks.json
```

**What is exposed:** the issuer's own URL, the claim *names* it publishes, the
signing algorithm, and **public** RSA key material. Nothing else.

**What is not exposed:** no tokens, no private key material, no audiences, no
workspace names, no run data, and nothing that accepts a write. Both endpoints
are read-only `GET`s that take no parameters. The audiences in particular stay
off this surface on purpose — an audience is the value a trust policy matches
on, so the *set* of them names the roles a deployment can ask to assume.

**There is deliberately no air-gapped variant**, and there will not be one. The
major clouds cannot federate to an issuer they cannot fetch, so a
"locally-provided JWKS" mode would work for a minority of targets and fail for
the ones most people need. One generic mechanism that works everywhere beats two
that each work somewhere.

**A deployment that will not expose those two paths is not broken — it simply
does not use this feature.** The [fall-through](#fall-through-not-replacement--permanently)
is permanent and supported, so such a deployment keeps pool-level ServiceAccount
identity exactly as it has it today. See [recipe B in
deployment-network-isolation.md](deployment-network-isolation.md#b-internal-lb-for-everyone-no-public-surface),
which is mutually exclusive with OIDC federation; [recipe
A](deployment-network-isolation.md) keeps the management plane private and
exposes only `webhookIngress`, which is where these two paths land.

---

## Fall-through, not replacement — permanently

**A workspace whose resolved audience map is empty runs as the agent pool's
ServiceAccount, exactly as it does today.** Nothing changes for it. There is no
migration, no cliff and no deprecation: keep assigning permissions to your runner
pods and runs fall through to them.

**Partial adoption is the expected posture**, not a stage on the way to
something else. Give an identity to the few workspaces that need segmentation and
leave the pool to serve everything else. Read this feature as *"the credential
boundary is the workspace where one is set, and the pool otherwise"*.

This extends the precedence table in
[cloud-credentials.md](cloud-credentials.md#runner-serviceaccount) rather than
replacing it:

| Priority | Source | Configured via |
|---|---|---|
| 1 | **The workspace's own federated identity** | the resolved `oidc-audiences` map, plus your own provider block (this page) |
| 2 | **Global runner SA** | `runners.serviceAccount.name` in Helm values |
| 3 | **K8s default SA** | Implicit namespace default |

The runner pod keeps the pool's ServiceAccount in **every** case, including when
a workspace is federated. **Terrapod sets no cloud credential environment
variable at all** — not `AWS_WEB_IDENTITY_TOKEN_FILE`, not `AWS_ROLE_ARN`, not
`ARM_OIDC_TOKEN_FILE_PATH`, not `GOOGLE_APPLICATION_CREDENTIALS` — so an
IRSA-annotated or Workload-Identity-annotated pool and a federated workspace
coexist without either having to know about the other. Coexistence is achieved by
**absence**: there is nothing of ours for the pod's own identity to have to beat.

---

## Turning it on

Three things. Two are the operator's and one is the workspace admin's;
**neither implies the other**, and a published issuer with no workspace opted in
grants nothing.

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

**The two issuer paths are added to `webhookIngress` automatically** when
`oidc_issuer.enabled` is true. You do not list them yourself:

```yaml
webhookIngress:
  enabled: true
  className: nginx
  hostname: terrapod-webhooks.example.com
  tls: true
  # paths: the chart's own defaults, plus /.well-known/openid-configuration
  # and /.well-known/jwks.json while the issuer is enabled.
```

Publishing a trust root the clouds cannot fetch is not a degraded
configuration but a broken one, and the failure surfaces at the cloud's token
exchange rather than anywhere on the Terrapod side — so the chart adds them
rather than leaving an uncomment-this step. Listing them by hand as well is
harmless; the template de-duplicates. They are added **exactly**, never as a
`/.well-known` prefix, so the Terraform service-discovery document is not
exposed as a side effect.

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
> List them explicitly here: the chart **refuses an empty `paths`** (an Ingress
> that accepts nothing is almost certainly a mistake), and because the automatic
> addition de-duplicates, naming them yourself yields exactly these two and no
> webhook path.
>
> The issuer deliberately rides this Ingress rather than getting one of its own,
> because its `paths` allow-list is already the right granularity.
>
> If you have overridden `webhookIngress.paths` in your own values, you still
> inherit the automatic addition — it is applied to whatever list you supply.

Verify both from outside your network:

```sh
curl -s https://terrapod-webhooks.example.com/.well-known/openid-configuration | jq .issuer
curl -s https://terrapod-webhooks.example.com/.well-known/jwks.json | jq '.keys[].kid'
```

The `issuer` value has to equal what you configure in the cloud, character for
character, including the absence of a trailing slash. An in-cluster fetch proves
nothing about what a cloud can reach, so run these from outside.

### 2. Set the deployment's audience catalogue

The catalogue maps **a provider configuration to the audiences a token minted for
it should carry**. It is the deployment-wide default, and most installs will set
it here once and never touch it per workspace:

```yaml
api:
  config:
    auth:
      oidc_issuer:
        audiences:
          aws:     ["sts.amazonaws.com"]
          azurerm: ["api://AzureADTokenExchange"]
          vault:   ["https://vault.example.com"]
```

**Terrapod attaches no meaning to any of these strings.** There is no built-in
`sts.amazonaws.com`: any provider may be federated to any audience, because the
only gate is the cloud's own trust policy. Terrapod holds no per-cloud knowledge
and will not second-guess a mapping — a wrong one fails at the cloud, which is
where the trust decision actually lives.

### 3. Give a workspace its override, if it needs one

A workspace's `oidc-audiences` is **merged over** the catalogue, **per key**:

```hcl
resource "terrapod_workspace" "dns" {
  name = "prod-dns"

  # Inherits `aws` and `azurerm` from the catalogue; points `vault` at a
  # different instance for this workspace only.
  oidc_audiences = {
    vault = ["https://vault-eu.example.com"]
  }
}
```

Or `PATCH /api/terrapod/v1/workspaces/{id}`:

```json
{"data": {"attributes": {"oidc-audiences": {"vault": ["https://vault-eu.example.com"]}}}}
```

**An empty override is the common case and does not mean "mints nothing"** — it
means "take the catalogue as it stands". A workspace mints nothing only when the
**resolved merge** is empty.

See [the audience map](#the-audience-map) for the keys, the merge rules and the
limits.

---

## The audience map

### Keys are provider configurations

A key is the **bare provider type exactly as a `provider` block writes it** —
`aws`, `azurerm`, `google`, `vault` — optionally with an alias, where the alias
is part of the key:

```hcl
provider "aws" { }                      # key: aws
provider "aws" { alias = "west" }       # key: aws.west
provider "vault" { alias = "eu" }       # key: vault.eu
```

The bare type, because that is what you write in a configuration and therefore
the only name you can be expected to use as a key. Two registry namespaces
publishing the same type is not a concern: a single configuration cannot use
both, so within one workspace the type is unambiguous.

### Lookup is specific, then general

`vault.eu` is answered by an entry for `vault.eu` if there is one, and by `vault`
otherwise. So aliased configurations inherit their type's audiences unless you
deliberately give one its own, and you never have to enumerate every alias.

### The value is always a list, even for one entry

```yaml
aws: ["sts.amazonaws.com"]        # correct
aws: "sts.amazonaws.com"          # rejected
```

**Keep it to one entry unless you genuinely mean "these are
interchangeable".** A multi-valued `aud` is replayable between the targets it
names — anything that can read the token file can present it to any of them —
and **AWS refuses a multi-valued `aud` outright**. That refusal lands at the
cloud's token exchange, not at configuration time, so Terrapod cannot warn you
about it. A list within one entry is safe for an OpenBao/Vault-like consumer
whose `bound_audiences` intersects, and unsafe for AWS; Terrapod cannot tell
which a given target is.

### Merge semantics

| | |
|---|---|
| Key in **both** | the workspace's list replaces the catalogue's **whole** list for that key |
| Key only in the **catalogue** | inherited |
| Key only in the **workspace** | added — a workspace may name a target the catalogue does not |
| Key **removed** from the workspace | falls back to the catalogue's value |

Replacement rather than append, because appending would make it impossible to
*narrow* a target — which is the main reason a workspace would override one.

**Removing the key is the defined way to stop overriding.** An explicitly empty
list for a key is therefore **refused** (`oidc-audiences["vault"] cannot be an
empty list — remove the key instead`): it is neither an override nor a removal,
and accepting it would let an operator believe they had suppressed a target when
they had not. An empty *map* still means something different again — it drops
every override — and is valid.

> **The API returns the workspace's own override, not the merged result.** The
> merge happens when a run is created (snapshotted onto the run) and again at
> mint time. So reading `oidc-audiences` off a workspace tells you what that
> workspace overrides, and the catalogue tells you what it inherits.

### Seeing what you would inherit

The two-level merge is otherwise hard to observe, so the catalogue is readable
through the API by **any authenticated user** — not just a platform admin,
because the person who needs it is the workspace owner deciding whether to
override a key:

```sh
curl -s -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  https://terrapod.example.com/api/terrapod/v1/oidc/audience-defaults | jq .
```

```json
{
  "data": {
    "type": "oidc-audience-defaults",
    "id": "default",
    "attributes": {
      "audiences": {"aws": ["sts.amazonaws.com"], "vault": ["https://vault.example.com"]},
      "issuer-enabled": true
    }
  }
}
```

`issuer-enabled` is reported separately on purpose: **an empty catalogue and a
disabled issuer are different states with the same symptom** ("my workspace
minted nothing"), and only one of them is fixed by adding audiences. An empty
`audiences` map is the default and is not an error.

Knowing an audience grants nothing on its own — the federation target's own
trust policy is the gate, and minting needs a phase-bound runner token scoped to
a run on that workspace. Note the deliberate asymmetry with the runner-facing
targets endpoint, which returns target **names only**: a runner writes a file
and the engine reads it, so it has no use for the values, whereas the set of
audiences names the roles this deployment can ask to assume.

### Limits and validation

| | |
|---|---|
| Keys per map | **10** |
| Audiences per key | **10** |
| Audience length | **255** characters |
| Key length | **128** characters |
| Key shape | no whitespace; at most one `.`; no leading or trailing `.` |
| A blank audience | **refused**, not dropped |
| A duplicate audience under one key | **refused** |
| An empty list for a key | **refused** — remove the key |

Stored **byte-for-byte** after rejecting the unacceptable, and never normalised.
An audience is an opaque string the federation target chose, so there is nothing
to canonicalise — and normalising would make a Terraform plan disagree with its
own apply.

Deliberately **not** validated: whether an audience suits the provider it is
keyed under, and whether the same audience appears under two keys. The first is
the cloud's trust policy to decide; the second may legitimately be two OpenBao
instances that share a `bound_audiences`.

---

## What the run actually gets

After `init`, the runner discovers which provider configurations the root module
uses, mints one token per matching target, and writes each to its own path:

| | What it is | |
|---|---|---|
| `/var/run/terrapod/oidc/<target>/token` | **One JWT per provider configuration**, mode `0600`. `<target>` is the map key — `aws`, or `aws.west` | **file** |
| `TERRAPOD_OIDC_TOKEN_DIR` | `/var/run/terrapod/oidc` — the **directory**, for a configuration that would rather not hard-code it | env |
| `TF_VAR_terrapod_oidc_token_dir` | The same value as a Terraform variable — declare `variable "terrapod_oidc_token_dir" {}` and it arrives with no wiring | env |
| `TERRAPOD_RUN_PHASE` | `plan` or `apply` | env |
| `TF_VAR_terrapod_run_phase` | The same value as a Terraform variable | env |

A provider block builds its own path from the directory:

```hcl
variable "terrapod_oidc_token_dir" {
  type    = string
  default = "/var/run/terrapod/oidc"
}

# -> /var/run/terrapod/oidc/aws/token
web_identity_token_file = "${var.terrapod_oidc_token_dir}/aws/token"
```

**Give the variable a default.** The environment variables are exported only
once at least one token has been delivered, so a workspace whose resolved map is
empty — or a `tofu plan` run outside Terrapod — leaves them unset. A declared
variable with no default would then fail to resolve.

**There is deliberately no per-target environment variable.** A target name may
carry a dot (`aws.west`) and there is no sane environment variable name for that,
so one documented convention — the directory plus the target name — beats a
mangling rule nobody can predict.

### Why one token per target, rather than one shared token

Two independent reasons, and the second is the one that will bite you:

1. **A multi-audience token is replayable between its targets.** Anything that
   can read the file can present it to any of them, so a single token audienced
   for AWS and your secret store makes each of them accept a credential minted
   for the other.
2. **AWS refuses a multi-valued `aud` outright.** It fails at
   `AssumeRoleWithWebIdentity`, inside the cloud, not at configuration time — so
   nothing on the Terrapod side reports a problem.

So there is no combined token and no shared file. Each provider block names the
one token it needs.

The token contents never reach a log line. The runner streams stdout verbatim,
and a JWT in a log is a credential in a log — so the run log records which
*target* a token was delivered for, and its audiences, and never the token.

---

## How the runner knows which tokens to fetch

Three steps, all after `init`:

1. **Ask the API which targets this run mints for.** `GET
   /api/terrapod/v1/runs/{run_id}/cloud-identity-targets` returns the target
   *names* from the run's snapshot. `204` means this run mints nothing, and the
   runner stops here without invoking the engine at all — which is why the
   feature adds no cost and no new failure mode to a run that does not use it.
   The response carries **names only, never audiences**.
2. **Ask the engine which provider configurations the root module uses**, by
   running its own graph command (`tofu graph`, or `terraform graph`, whichever
   binary the run uses). The engine prunes provider configurations nothing
   references, so a `provider "aws" { alias = "unused" }` nobody points at
   yields no token, correctly. The graph build is allowed **180 seconds**.
3. **Mint the intersection.** `POST
   /api/terrapod/v1/runs/{run_id}/cloud-identity-token?target=<target>`, once per
   target that is both configured and used, writing each answer to its own path.

A configured target the root module never uses is not an error — the mapping is
per workspace and a configuration need not use every provider in it. A used
provider that nothing maps to is the common case for most providers in most
workspaces.

### Why after `init`, and what it costs

**Discovery asks the engine, and the engine cannot answer until the providers are
installed.** The graph command needs `.terraform/` populated. With Terragrunt
there is a second reason: the working directory moves after `init`, so the
post-`init` directory is the only one that holds the configuration the run will
actually execute.

> **A `pre_init` execution hook can no longer see the tokens.** They do not exist
> yet at that point. **A hook that talks to a cloud belongs at `pre_plan` or
> `pre_apply`**, both of which run after the credential step. `post_plan` and
> `post_apply` are after it too.

It also runs *after* the backend backstop, deliberately: a run that is about to
be failed for a non-local backend should not mint credentials first.

### What happens when something goes wrong

| Situation | What happens |
|---|---|
| The run maps no targets (`204`) | The runner takes no action and does not invoke the engine. The run uses the pool's identity. **Normal, and permanent** |
| The issuer is not enabled deployment-wide (`204`) | The same outcome — an operator who has not published an issuer has not opted this deployment in |
| The API does not serve the targets endpoint (`404`) | Read as "nothing to do". An API older than this runner image, and in agent mode the control plane and a runner in another cluster upgrade independently |
| Nothing maps to one requested target (`204`) | That target is skipped. The others are still delivered |
| The run maps targets, but the root module uses none of them | No token is written, and no environment variable is exported. The run uses the pool's identity — a configured target a configuration never uses is not an error |
| The graph command fails, or names no provider at all | **The run fails**, naming the engine's own error. A configuration that reaches a cloud with no provider configuration does not exist, so an empty graph means our parser or the engine's output has moved — and failing is what stops that becoming a silent fall-through |
| The run maps targets and **anything else fails** | **The run fails.** Credentials were asked for and could not be had |
| The runner image predates the feature | Nothing is asked for. The run uses the pool's identity and reports success. **See [the honest limitation](#the-honest-limitation-a-runner-image-that-predates-this-feature)** |

The failure case is deliberate and is not a warn-and-continue. Falling through
would not mean *no* credentials — it would mean **the pool's**, which are broader
than the ones the workspace was deliberately moved off. A run that quietly
succeeds against real infrastructure under permissions nobody chose is worse than
a run that fails.

---

## Per-provider configuration

**This is the half Terrapod does not do, so it is the half you write.** Every
block below needs a value Terrapod never learns — a role ARN, a client id, a
pool provider audience — because nothing in the platform knows which target it is
talking to.

Each block reads **its own** token file, named after its map key.

| Target | Provider configuration | Typical audience |
|---|---|---|
| **AWS** | `assume_role_with_web_identity` block: `role_arn`, `web_identity_token_file`, optional `session_name` | `sts.amazonaws.com` |
| **Azure** | `use_oidc = true` plus `oidc_token_file_path`, with `client_id` and `tenant_id` | `api://AzureADTokenExchange` |
| **GCP** | `external_credentials` block: `audience`, `service_account_email`, `identity_token` — a **value**, so read the file with `file()` | The workload identity pool provider's audience |
| **OpenBao/Vault** | A JWT auth role whose `bound_audiences` matches | Whatever that role's `bound_audiences` says |

These are the audiences those targets conventionally expect, not values Terrapod
knows or imposes. You choose them in the catalogue.

### AWS

```hcl
provider "aws" {
  region = "eu-west-1"

  assume_role_with_web_identity {
    role_arn                = "arn:aws:iam::123456789012:role/terrapod-prod-dns"
    session_name            = "terrapod"
    web_identity_token_file = "${var.terrapod_oidc_token_dir}/aws/token"
  }
}
```

Catalogue entry: `aws: ["sts.amazonaws.com"]`.

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

> **The AWS condition key named `aud` is not the `aud` claim.** AWS maps the
> `aud` condition key to the token's `azp` claim when one is present, and falls
> back to `aud` only when it is absent; the real `aud` claim is exposed as the
> **`oaud`** condition key. Terrapod mints no `azp`, so `…:aud` reads the `aud`
> claim here — but if you ever see a condition evaluate against something you did
> not expect, that precedence is why.

An aliased configuration reads its own file:

```hcl
provider "aws" {
  alias  = "west"
  region = "us-west-2"

  assume_role_with_web_identity {
    role_arn                = "arn:aws:iam::123456789012:role/terrapod-prod-dns-west"
    web_identity_token_file = "${var.terrapod_oidc_token_dir}/aws.west/token"
  }
}
```

`aws.west` inherits the `aws` audiences unless the catalogue or the workspace
gives it its own entry. Both configurations can share one audience *and* still
get separate tokens, because the token is per provider configuration.

`AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` are the environment equivalents
of the two fields above, should you prefer to set them as workspace
`env`-category variables instead of writing the block — though they are
deployment-wide per run, so they suit a single-provider configuration only. If
you set both, the block wins: the provider documents that *"values configured in
the `assume_role_with_web_identity` block take precedence over environment
variables for both token sources"*. Terrapod sets neither, so there is nothing of
ours for your own configuration to have to beat.

### Azure

```hcl
provider "azurerm" {
  features {}

  use_oidc             = true
  oidc_token_file_path = "${var.terrapod_oidc_token_dir}/azurerm/token"

  client_id       = "00000000-0000-0000-0000-000000000000"
  tenant_id       = "11111111-1111-1111-1111-111111111111"
  subscription_id = "22222222-2222-2222-2222-222222222222"
}
```

Catalogue entry: `azurerm: ["api://AzureADTokenExchange"]`. The environment
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
    identity_token        = file("${var.terrapod_oidc_token_dir}/google/token")
  }
}
```

`identity_token` takes the token **value**, not a path — which is why it is read
with `file()`. Set the catalogue's `google` entry to the same string you put in
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
`credential_source.file` is `/var/run/terrapod/oidc/google/token`. You supply
that file yourself — in a custom runner image, or written by a `pre_plan`
[execution hook](execution-hooks.md) — because Terrapod generates
credential-config JSON for no target. **Not a `pre_init` hook**: the tokens do
not exist yet at that point.

### OpenBao (or HashiCorp Vault)

The same mechanism authenticates against a JWT auth role. Nothing cloud-shaped is
involved, which is the point: this is the identity half, and it works for any
OIDC-federating target.

```sh
bao auth enable jwt   # or: vault auth enable jwt

bao write auth/jwt/config \
  oidc_discovery_url="https://terrapod-webhooks.example.com"

bao write auth/jwt/role/prod-dns \
  role_type="jwt" \
  user_claim="sub" \
  bound_audiences="https://vault.example.com" \
  bound_claims_type="string" \
  bound_claims='{"workspace":"prod-dns","phase":"apply"}' \
  token_policies="prod-dns"
```

Set the `vault` catalogue entry to match `bound_audiences` — here
`vault: ["https://vault.example.com"]`. The run then logs in with its own token
file and gets a short-lived OpenBao/Vault token scoped to that workspace:

```hcl
provider "vault" {
  auth_login_jwt {
    role = "prod-dns"
    jwt  = file("${var.terrapod_oidc_token_dir}/vault/token")
  }
}
```

**OpenBao/Vault is also the case where several audiences under one key are
genuinely safe**, because `bound_audiences` is an intersection: a role accepts a
token whose `aud` contains any of its bound values. So a set of instances that
really do share a bound audience can sit under one key. Several *instances* with
*different* addresses each want their own key — `vault`, `vault.eu` — which is
what gives each its own token and keeps one instance's token useless at another.

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
an execution hook fetching a dynamic credential previously had to log in with a
credential shared by every workspace, so the role it bound to could be no
narrower than the pool. Now the login is itself per-workspace — from `pre_plan`
or later, where the token file exists.

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
    web_identity_token_file = "${var.terrapod_oidc_token_dir}/aws/token"
  }
}
```

The `default` matters for the same reason as above: the variable is only
exported on a run that delivered at least one token.

This exists because HCL otherwise cannot see which phase it is in, so the apply
increment would not be expressible at all. **One role whose trust policy
conditions on the `phase` claim is simpler and usually better** — the cloud does
the switching, and the configuration carries one role ARN. Reach for the variable
when you need two genuinely distinct roles, for instance in two different
accounts.

---

## The claim set

Every token for every target carries the same claims; only `aud` differs.

| Claim | Example | Notes |
|---|---|---|
| `iss` | `https://terrapod-webhooks.example.com` | Matched **exactly** by the cloud |
| `sub` | `workspace:prod-dns:phase:apply` | Composite, colon-delimited, phase last |
| `aud` | `["sts.amazonaws.com"]` | **This target's** audiences, verbatim and in order |
| `workspace` | `prod-dns` | The workspace name |
| `workspace_id` | `0f8b…` | The id — stable across a rename, where the name is readable |
| `phase` | `apply` | `plan` or `apply`. **Absent** when the runner token made no phase claim |
| `run_id` | `3c71…` | The run, for correlating a cloud audit entry back to Terrapod |
| `terrapod_organization` | `default` | Always the literal `default` — Terrapod is single-organization |
| `iat`, `nbf`, `exp`, `jti` | | Standard. `jti` is what makes two tokens for one run distinguishable in a cloud audit log |

The token header carries `kid`, an RFC 7638 thumbprint of the signing key.

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

## When the configuration moves between plan and apply

A run **snapshots its resolved audience map at creation** — the record of what
its plan was reviewed under — and the mint endpoint **re-resolves** the requested
target from live configuration and compares the two. If they disagree the mint
returns **409** and the run fails:

```
The cloud identity configuration for 'aws' has changed since this run was
created, so the identity this run would present is no longer the one its plan
was reviewed under. Queue a new run to pick up the current configuration.
```

Minting from the snapshot alone would hand an apply a token matching the reviewed
plan while the cloud had moved on, and the rejection would then land at the
cloud's token exchange, deep inside the engine and possibly after a partial
apply. Refusing before anything executes is the same shape as a saved plan
refused because the state serial moved.

The comparison is order-sensitive, not just membership: the entries are what goes
into `aud`, and a cloud matching on the first value would see a different token.
A target that previously resolved and now resolves to nothing, or the reverse, is
a change too.

The same comparison also runs **at confirm time**, beside the existing
state-drift and plan-expiry guards, so an apply is refused before a Job is ever
scheduled and the reason names what moved:

```
cloud identity configuration changed since plan (aws, vault.eu)
```

That check is scoped to **what the run actually minted for**, not what it was
configured for. The snapshot is the merged map, so it carries catalogue entries a
workspace may never use; checking against that whole set would mean one edit to
the deployment catalogue refusing every pending apply in the fleet, including
runs whose own identity had not moved at all.

A target the catalogue has *gained* since the plan is deliberately **not** a
staleness cause. The mint reads the run's own snapshot, so a new target yields no
token at apply exactly as it yielded none at plan — the identity the apply
presents is unchanged.

**The operator's action is always the same: queue a new run.** This is expected
after a catalogue edit, and it only affects runs that were already planned.

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
    existingSecretKey: oidc_signing_key   # an RSA private key, PKCS8 PEM
```

```sh
kubectl -n terrapod create secret generic terrapod-oidc-signing-key \
  --from-file=oidc_signing_key=./oidc-signing-key.pem
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

### The issuer documents are cacheable, and the JWKS lifetime is derived

Both public documents carry a `Cache-Control` header, and that is both a
correctness requirement and the mitigation for their being necessarily
unauthenticated.

| Document | `max-age` |
|---|---|
| `/.well-known/openid-configuration` | **300** seconds. It changes only when the issuer URL or the claim set does, and a stale copy carries no key material — but modest, so a corrected issuer URL takes effect in minutes |
| `/.well-known/jwks.json` | **half of `key_propagation_seconds`**, with a floor of 60 seconds. 300 seconds at the default |

**The JWKS lifetime is derived rather than configured**, because the two numbers
describe the same commitment from opposite ends. `key_propagation_seconds` exists
*because* the clouds cache this document, so advertising a cache lifetime longer
than the wait would mean a cloud still holding the old key set at the moment we
begin signing with the new one. Half rather than all of it, because a cache
expiring exactly at the boundary is a clock-skew race.

A cloud may apply its own caching policy and ignore the header entirely, so the
propagation window remains the correctness mechanism. The header reduces load and
aligns compliant caches and intermediaries.

**Both documents also have their own rate-limit bucket**, at
`authenticated_requests_per_minute` rather than the anonymous IP limit. They are
anonymous by necessity and shared by every cloud, so a `429` on the JWKS would
fail token verification for every federated run at once — including runs whose
own traffic had nothing to do with filling the bucket. Their own bucket isolates
them in both directions: unrelated traffic cannot starve the trust root, and a
cloud retry storm cannot starve anything else.

### Configuration reference

| Key (under `api.config.auth.oidc_issuer`) | Default | What it does |
|---|---|---|
| `enabled` | `false` | Publish the discovery document and JWKS. Off means the routes are not mounted |
| `public_url` | `""` | The issuer URL, exactly as the cloud is configured with it. Empty derives it from `webhookIngress.hostname`, falling back to `external_url` |
| `audiences` | `{}` | The deployment's audience catalogue: provider configuration → its audiences. A workspace's `oidc-audiences` is merged over this per key |
| `token_ttl_seconds` | `900` | Token lifetime, 60–43200. Short because the target exchanges it immediately |
| `key_propagation_seconds` | `600` | How long a rotated-in key is published before it starts signing. The retired key signs across this window, and the JWKS `max-age` is half of it |
| `retired_key_grace_seconds` | `3600` | How long a retired key stays published. Must exceed both `token_ttl_seconds` and `key_propagation_seconds` |

The signing key is not among these, because it is key material: it is supplied as
`api.oidcSigningKey.existingSecret` / `existingSecretKey` and injected by
`secretKeyRef`, never rendered into a ConfigMap.

---

## The honest limitation: a runner image that predates this feature

**A runner image older than this feature never asks for a token.** A workspace
whose resolved map names targets then runs as the agent pool's identity anyway —
silently, because from the API's side nothing was asked for and nothing was
refused.

**Terrapod cannot detect this.** No runner-image version is reported to the API,
so there is no server-side check to add. It is a documented, managed degradation:
**upgrade your runner images** when you adopt this, and treat a federated
workspace on a stale pool as mis-scoped until you have.

It fails in the safe direction in one sense — the run authenticates as the pool,
which is what it did before — and in the dangerous direction in another: a
workspace you deliberately moved off the pool's broad permissions is still using
them.

A runner image that knows about the feature but **not** about per-target minting
asks for a token without naming a target. That request answers `204` rather than
`400`, deliberately: `400` would make every run on such a runner fail, when the
designed behaviour is the same permanent fall-through. A request we cannot serve
must look like "nothing here" rather than like a fault.

A token minted before the phase claim existed carries no phase, which is read as
"makes no claim": the JWT then carries no `phase` either, so a trust policy
conditioning on it simply does not match. That refuses the credential rather than
quietly widening it.

---

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /.well-known/openid-configuration` | **None** | Discovery document. Mounted only when enabled. `Cache-Control: max-age=300` |
| `GET /.well-known/jwks.json` | **None** | The published signing keys. `max-age` is half `key_propagation_seconds` |
| `GET /api/terrapod/v1/runs/{run_id}/cloud-identity-targets` | Runner token, scoped to that run | Which provider configurations this run mints for — **names only, never audiences**. `204` when it mints nothing |
| `POST /api/terrapod/v1/runs/{run_id}/cloud-identity-token?target=<target>` | Runner token, scoped to that run | Mint one target's token. `204` when nothing maps to it; `409` when the configuration moved since the run was created |
| `GET /api/terrapod/v1/oidc/audience-defaults` | Any authenticated user | The deployment's audience catalogue a workspace's map merges over, plus `issuer-enabled` |
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
- [Execution Hooks](execution-hooks.md) — the hook points, and why a cloud-talking hook belongs at `pre_plan` or later
- [Network Isolation](deployment-network-isolation.md) — the trade this feature makes, and which recipes it is compatible with
- [VCS Integration → Pull requests from forks](vcs-integration.md#pull-requests-from-forks) — the gate the `phase` claim complements but does not replace
- [Runbooks → Rotating the OIDC issuer signing key](runbooks.md#rotating-the-oidc-issuer-signing-key)
- [API Reference](api-reference.md#per-workspace-cloud-identity-oidc-federation) — the `oidc-audiences` attribute and the endpoints above
