# Authentication

Terrapod supports multiple authentication methods: local passwords, OIDC, SAML, and OAuth2 PKCE for the terraform CLI. This guide covers setup and configuration for each.

---

## Overview

Three authentication methods, evaluated in priority order:

| Type | Storage | Lifetime | Use Case |
|---|---|---|---|
| **Runner Tokens** | Stateless (HMAC-SHA256) | Short-lived (1h default, 2h max) | Runner Jobs (scoped to a single run) |
| **API Tokens** | PostgreSQL (SHA-256 hashed) | Configurable max TTL | terraform CLI, automation |
| **Sessions** | Redis | 12h sliding TTL | Web UI |

The unified auth dependency tries runner tokens first (fast HMAC verification, no I/O), then API tokens (DB lookup), then sessions (Redis lookup). All return the same `AuthenticatedUser` shape to downstream handlers.

---

![Login](images/login.png)

## Local Password Authentication

Local auth is the simplest authentication method, suitable for development and small deployments.

### Configuration

```yaml
# Helm values
api:
  config:
    auth:
      local_enabled: true
```

Or via environment variable:

```zsh
TERRAPOD_AUTH__LOCAL_ENABLED=true
```

### Bootstrap Admin User

The initial admin user is created by the bootstrap Helm hook:

```yaml
# Helm values
bootstrap:
  adminEmail: admin@example.com
  adminPassword: "a-strong-password"
```

Or reference an existing Kubernetes Secret:

```yaml
bootstrap:
  existingSecret: terrapod-admin-credentials
  emailKey: email
  passwordKey: password
```

Users can be managed from the admin panel at **Admin > Users**.

![User Management](images/admin-users.png)

### Password Requirements

Passwords are hashed with PBKDF2-SHA256 and validated with [zxcvbn](https://github.com/dropbox/zxcvbn) for strength. Weak passwords are rejected at creation time.

### Login Flow

```
POST /api/v1/auth/local/authorize
  email=admin@example.com
  password=xxx
    |
    v
Verify PBKDF2-SHA256 hash
    |
    v
Create session in Redis (tp:session:{token}, 12h sliding TTL)
    |
    v
Return session token + redirect URL
```

---

> **Register BOTH callback URLs.** Terrapod serves its API at `/api/v1` and, for
> the deprecation window, at `/api/terrapod/v1`. Which one it *sends* your IdP as
> the `redirect_uri` is set by `api.config.auth.legacy_callback_url`, which
> **defaults to `true`** (the `/api/terrapod/v1` form) so that upgrading cannot
> break an existing deployment.
>
> Your IdP validates that value against its own allow-list, so Terrapod serving
> both prefixes does not help — an unregistered `redirect_uri` is refused **at the
> IdP**, before the request reaches Terrapod, and every login fails with nothing
> in Terrapod's logs. Registering both URLs now costs nothing and makes the 2.0
> default flip a no-op. See [deprecations.md](deprecations.md).

## OIDC Authentication

Terrapod uses [authlib](https://authlib.org/) for OIDC integration. Any standards-compliant OIDC provider works.

Terrapod sends **S256 PKCE** ([RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)) on the upstream authorization request and token exchange, in addition to the client secret. No configuration is required — it is always on, and providers that don't enforce PKCE simply ignore the extra parameters. This makes Terrapod compatible with IdPs that require PKCE on the authorization-code flow even when a client secret is configured (e.g. Pinniped Supervisor).

### Auth0 Example

```yaml
api:
  config:
    auth:
      callback_base_url: "https://terrapod.example.com"
      sso:
        default_provider: auth0
        oidc:
          - name: auth0
            display_name: "Auth0 SSO"
            issuer_url: "https://your-tenant.auth0.com/"
            client_id: "your-client-id"
            scopes: ["openid", "profile", "email"]
            groups_claim: "https://your-tenant.auth0.com/groups"
            role_prefixes: ["terrapod:", "terrapod-"]
            claims_to_roles:
              - claim: "https://your-tenant.auth0.com/groups"
                value: "platform-admins"
                roles: ["admin"]
```

Inject the client secret via environment variable:

```zsh
TERRAPOD_AUTH0_CLIENT_SECRET="your-client-secret"
```

The environment variable name follows the pattern `TERRAPOD_{UPPERCASE_NAME}_CLIENT_SECRET`.

**Auth0 Application Settings:**

| Setting | Value |
|---|---|
| Application Type | Regular Web Application |
| Allowed Callback URLs | `https://terrapod.example.com/api/terrapod/v1/auth/callback` (and `/api/v1/auth/callback` — register both, see the note below) |
| Allowed Logout URLs | `https://terrapod.example.com` |

### Okta Example

```yaml
api:
  config:
    auth:
      callback_base_url: "https://terrapod.example.com"
      sso:
        default_provider: okta
        oidc:
          - name: okta
            display_name: "Okta SSO"
            issuer_url: "https://your-org.okta.com/oauth2/default"
            client_id: "your-client-id"
            scopes: ["openid", "profile", "email", "groups"]
            groups_claim: "groups"
            role_prefixes: ["terrapod:"]
            claims_to_roles:
              - claim: groups
                value: "TerrapodAdmins"
                roles: ["admin"]
```

```zsh
TERRAPOD_OKTA_CLIENT_SECRET="your-client-secret"
```

**Okta Application Settings:**

| Setting | Value |
|---|---|
| Sign-in method | OIDC - OpenID Connect |
| Application type | Web Application |
| Sign-in redirect URI | `https://terrapod.example.com/api/terrapod/v1/auth/callback` (and `/api/v1/auth/callback` — register both, see the note below) |
| Assignments | Assign to users/groups as needed |

### Azure AD (Entra ID) Example

```yaml
api:
  config:
    auth:
      callback_base_url: "https://terrapod.example.com"
      sso:
        default_provider: azure-ad
        oidc:
          - name: azure-ad
            display_name: "Microsoft SSO"
            issuer_url: "https://login.microsoftonline.com/{tenant-id}/v2.0"
            client_id: "your-application-id"
            scopes: ["openid", "profile", "email"]
            groups_claim: "groups"
            role_prefixes: ["terrapod:"]
```

```zsh
TERRAPOD_AZURE_AD_CLIENT_SECRET="your-client-secret"
```

**Azure AD App Registration:**

| Setting | Value |
|---|---|
| Redirect URI | `https://terrapod.example.com/api/terrapod/v1/auth/callback` (and `/api/v1/auth/callback` — register both, see the note below) (Web platform) |
| Token configuration | Add optional claim: `groups` |
| API permissions | `openid`, `profile`, `email` |

### Role Resolution from OIDC

When a user logs in via OIDC, roles are resolved from three sources (merged and deduplicated):

1. **IDP groups** -- group names from the `groups_claim`, with `role_prefixes` stripped. For example, if the IDP returns `terrapod:developer` and the prefix is `terrapod:`, the role `developer` is assigned.

   > **Read this before pointing Terrapod at a directory you do not fully control.**
   >
   > **Every group becomes a role name. There is no allow-list.** A group the IDP
   > returns is a role name Terrapod will look for — so a group named literally
   > `admin` grants the built-in **platform admin** role, and one named `audit`
   > grants read access to every workspace. Neither needs any Terrapod-side
   > configuration, and neither leaves a role assignment behind to notice.
   >
   > **`role_prefixes` strips; it does not filter.** The name reads like a scope
   > and is not one. A group that matches a prefix has it removed; a group that
   > matches **no** prefix is passed through **unchanged**. So configuring
   > `role_prefixes: ["terrapod-"]` does not confine role-granting to
   > `terrapod-*` groups — `admin` still arrives as `admin`. The default is
   > `["terrapod:", "terrapod-"]`, so this applies to every deployment that has
   > not changed it.
   >
   > **SAML does not apply `role_prefixes` at all.** The setting exists on a SAML
   > provider and nothing reads it, so a SAML group arrives at role resolution
   > with its prefix intact: `terrapod-admin` is looked up as the role
   > `terrapod-admin`, which matches no built-in role and usually nothing at all.
   > The practical effect is that a prefixed SAML group grants nothing while an
   > unprefixed one named `admin` grants everything.
   >
   > **What to do today.** Treat the IDP group list as a grant list: if your
   > directory contains a group named `admin` or `audit` for any other purpose,
   > whoever is in it becomes a Terrapod platform admin or auditor at their next
   > login. Either rename those groups, or stop returning them in the
   > `groups_claim` — most IDPs can scope which groups are released per
   > application, and that scoping is the only real filter available.

2. **Claims-to-roles mapping** -- explicit rules in the config. Each rule matches a claim name + value and assigns specific roles.

3. **Internal role assignments** -- roles assigned via the `role_assignments` table (managed through the admin API or UI).

### Multiple OIDC Providers

You can configure multiple OIDC providers simultaneously:

```yaml
sso:
  default_provider: okta
  oidc:
    - name: okta
      issuer_url: "https://your-org.okta.com/oauth2/default"
      client_id: "..."
    - name: auth0
      issuer_url: "https://your-tenant.auth0.com/"
      client_id: "..."
```

The login page shows buttons for each configured provider.

---

## SAML Authentication

Terrapod uses [python3-saml](https://github.com/SAML-Toolkits/python3-saml) for SAML 2.0 integration.

### Azure AD SAML Example

```yaml
api:
  config:
    auth:
      callback_base_url: "https://terrapod.example.com"
      sso:
        saml:
          - name: azure-ad-saml
            display_name: "Azure AD (SAML)"
            metadata_url: "https://login.microsoftonline.com/{tenant-id}/federationmetadata/2007-06/federationmetadata.xml?appid={app-id}"
            entity_id: "https://terrapod.example.com"
            acs_url: "https://terrapod.example.com/api/terrapod/v1/auth/saml/acs"
            role_prefixes: ["terrapod:"]
            claims_to_roles:
              - claim: "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
                value: "{group-object-id}"
                roles: ["admin"]
```

**Azure AD Enterprise Application:**

| Setting | Value |
|---|---|
| Identifier (Entity ID) | `https://terrapod.example.com` |
| Reply URL (ACS URL) | `https://terrapod.example.com/api/terrapod/v1/auth/saml/acs` (and `/api/v1/auth/saml/acs` — register both, see the note below) |
| Sign on URL | `https://terrapod.example.com/login` |
| Claims | Name ID (email), groups |

Note: The API Docker image includes `xmlsec1` which is required for SAML signature verification.

### Assertion validation

Five checks decide whether an assertion the IDP posted is one Terrapod should
act on. Each has its own switch, per provider, because identity providers get
different things wrong and relaxing one should never cost you the others.

| Key | What it requires | Turn it off when |
|---|---|---|
| `validate_destination` | The assertion's `Destination` and `Recipient` name **this** deployment's ACS URL | Your IDP sends a `Destination` that genuinely differs from the URL you registered |
| `validate_in_response_to` | The assertion answers the authentication request this login sent | Your IDP does not echo `InResponseTo` on the `Response` element |
| `reject_replayed_assertions` | Each assertion is used once; the id is remembered in Redis for the rest of its validity window | Never, in practice — an IDP does not issue the same assertion twice |
| `want_assertions_signed` | The signature is on the assertion itself, not only on the enclosing message | Your IDP signs the message only |
| `reject_deprecated_algorithm` | No SHA-1 signature or digest (`RSA-SHA1`, `DSA-SHA1`, `SHA1`) | Your IDP cannot yet be moved off SHA-1 |

**The defaults differ by release line.** On the 2.x development line every one of
them is `true`. On the 1.x release lines every one is `false`, preserving the
behaviour an operator already has — a patch release must never lock someone out
of their own deployment. The implementation is identical on both; only the
default differs, so the setting you choose means the same thing on either.

```yaml
api:
  config:
    auth:
      sso:
        saml:
          - name: azure-ad-saml
            metadata_url: "https://login.microsoftonline.com/{tenant-id}/federationmetadata/2007-06/federationmetadata.xml"
            entity_id: "https://terrapod.example.com"
            # Explicit on a 1.x release, where the defaults are false:
            validate_destination: true
            validate_in_response_to: true
            reject_replayed_assertions: true
            want_assertions_signed: true
            reject_deprecated_algorithm: true
```

**What `Destination` is checked against.** The ACS URL, resolved in this order:
the provider's own `acs_url`; otherwise `auth.callback_base_url` plus the SAML
ACS path; otherwise `external_url` plus that path. `callback_base_url` comes
first because it is what the ACS URL registered with your IDP was built from,
and the IDP mirrors that URL back as `Destination` and `Recipient` — checking
against a different base is how this turns from a security control into a failed
login. If a proxy rewrites the path between your IDP and Terrapod, set `acs_url`
to the address the IDP actually posts to.

A SAML provider has always needed an absolute ACS URL — python3-saml refuses to
start without one — so turning `validate_destination` on asks for no
configuration a working SAML setup does not already have.

**Diagnosing a refusal.** Each check fails with its own message in the API log
and in the `401` body, naming the provider:

| Message contains | Check | Usual cause |
|---|---|---|
| `The response was received at … instead of …` | `validate_destination` | The IDP's reply URL is not the one Terrapod believes it serves |
| `carries no InResponseTo` | `validate_in_response_to` | IDP-initiated sign-on, or an IDP that omits the attribute |
| `answers a different authentication request` | `validate_in_response_to` | A stale browser tab, or a replayed assertion |
| `already been used` | `reject_replayed_assertions` | A replayed assertion, or a user double-submitting the IDP's form |
| `not signed and the SP require it` | `want_assertions_signed` | The IDP signs the message only |
| `Deprecated signature algorithm` | `reject_deprecated_algorithm` | The IDP still signs with SHA-1 |

Relax the one check the message names rather than all five: each failure is a
different problem, and the other four keep protecting you.

---

## Terraform Login Flow (OAuth2 PKCE)

The `terraform login` command uses OAuth2 Authorization Code with PKCE to obtain an API token.

### How It Works

1. Run `terraform login terrapod.local` (or `tofu login terrapod.local`)
2. Terraform fetches `/.well-known/terraform.json` for service discovery
3. A browser window opens to `/oauth/authorize` with a PKCE challenge
4. The user authenticates with their configured identity provider
5. After successful auth, the API generates a one-time authorization code
6. Terraform exchanges the code for an API token via `POST /oauth/token`
7. The token is stored in `~/.terraform.d/credentials.tfrc.json`

The token minted by `terraform login` is **short-lived** — its lifespan is `auth.login_token_ttl_hours` (default **12 hours**), so it expires at the end of a working session rather than living for the full `api_token_max_ttl_hours` cap. Re-run `terraform login` to get a fresh one. For long-lived automation, create a dedicated token (a [service token](#token-kinds--personal-vs-service-tokens) for scoped/M2M use) rather than relying on a login token.

### Prerequisites

The `callback_base_url` must be set to the externally-reachable URL of the Terrapod instance:

```yaml
api:
  config:
    auth:
      callback_base_url: "https://terrapod.example.com"
```

At least one SSO provider must be configured (OIDC or SAML), or local auth must be enabled.

### Usage

```zsh
# Login
terraform login terrapod.local

# Verify
terraform providers
# or
curl -s https://terrapod.local/api/tfe/v2/account/details \
  -H "Authorization: Bearer $(jq -r '.credentials["terrapod.local"].token' ~/.terraform.d/credentials.tfrc.json)"
```

### OpenTofu Compatibility

`tofu login` works identically:

```zsh
tofu login terrapod.local
```

Credentials are stored in `~/.terraform.d/credentials.tfrc.json` (shared location).

---

## API Tokens

API tokens are long-lived credentials for automation, CI/CD pipelines, and the terraform CLI.

### Token Format

```
{random_id}.tpod.{random_secret}
```

Example: `abc123def456.tpod.ghijklmnopqrstuvwxyz0123456789`

### Security Properties

- SHA-256 hashed at rest in the `api_tokens` PostgreSQL table
- The raw token value is returned only once at creation time
- Max lifetime enforced via `auth.api_token_max_ttl_hours` config

> **A non-positive `lifespan_hours` makes an interactive token never expire.**
> `lifespan_hours` takes precedence over `api_token_max_ttl_hours`, and a value of
> `0` — or any negative number — is read as "no expiry" rather than as "unset", so
> an interactive token created with `{"lifespan_hours": 0}` is exempt from the cap
> for the rest of its life. The cap uses `0` to mean *no limit*, and that meaning
> is applied to the per-token field as well, where it is almost never what the
> caller intended.
>
> **Service tokens are not affected** — they fall back to
> `auth.service_token_max_ttl_hours` whenever their resolved lifespan is
> non-positive, so they always carry an expiry.
>
> Until this is addressed, treat a non-positive `lifespan_hours` as a value to
> reject at your own boundary: omit the field to get the cap, or pass a positive
> number of hours. `GET /api/terrapod/v1/users/{user_id}/authentication-tokens`
> reports `expires-at`, and it is `null` for every token that has no expiry —
> which is these, plus every interactive token when `api_token_max_ttl_hours` is
> itself `0`. So a `null` is evidence of this only when the cap is set.
>
> **In 2.0 a non-positive `lifespan_hours` is treated as unset**, so the cap
> applies and the token expires. A deployment that is relying on `0` to mint a
> non-expiring interactive token will find those tokens expiring after
> `api_token_max_ttl_hours` once upgraded; mint them with an explicit positive
> lifespan, or set the cap to `0`, before you upgrade.
- Changing the max TTL retroactively affects all existing tokens

### Token Kinds — Personal vs Service Tokens

Every token has a **kind** that determines how its permissions are resolved:

| Kind | Who can create | Effective permissions | Bound to | Best for |
|---|---|---|---|---|
| **`interactive`** (default) | anyone | the owner's full live roles | the owner | a person's CLI / `terraform login` token |
| **`service_bound`** | anyone | the **intersection** of the token's pinned roles and the owner's live roles, resolved per resource | the owner | scoped automation that should never outlive the person who made it |
| **`service_detached`** | **admins only** | the token's pinned roles as an **absolute** scope | nobody (unbound) | critical machine-to-machine automation that must survive any one person leaving |

The intersection for `service_bound` is the key safety property: you can pin a token to a subset of your roles, but it can never grant more than you currently have.

That property is enforced on token management too, which is the non-obvious half: a request authenticated **with** a scoped token cannot create a token of kind `interactive` (which would carry its owner's full live roles), convert any token to `interactive`, or pin roles outside its own scope. Without that, a pinned credential could simply mint an unpinned one for the same person and step around its own scope without needing a single extra role. Narrowing is unaffected, and an effectively-admin service token is exempt because it already holds the maximum scope. Manage tokens from an interactive session or token. Pick the pinned roles from your own roles in the create form; the UI filters to exactly that set.

`service_detached` tokens are the supported path for long-lived, business-critical automation. Because they are unbound and admin-managed, they don't break when an individual is offboarded — but they also don't inherit anyone's live permissions, so their pinned scope is the whole story. Keep it minimal.

#### Offboarding safety — the idle-login guard

A **`service_bound`** (or `interactive`) token is **rejected if its owner hasn't successfully logged in within `auth.bound_token_idle_days`** (default 7). Terrapod records the last successful login per user in Redis (`tp:user_seen:{email}`); once that window lapses, every token bound to the user stops authenticating until they log in again.

This means a user who is cut off from SSO (account disabled at the IdP) automatically loses their bound tokens within a week — without any cleanup action — closing the "ex-employee's CI token still works weeks later" gap. For an **immediate** cut-off, use the [revoke-all offboarding runbook](runbooks.md). Critical M2M automation should use **`service_detached`** tokens (admin-managed, exempt from the idle guard) so it isn't affected by any individual's login activity.

```yaml
api:
  config:
    auth:
      bound_token_idle_days: 7            # reject bound tokens after this idle window (0 = disabled)
      service_token_max_ttl_hours: 8760   # hard cap on service-token lifespan (always expires)
      token_expiry_warning_days: 14       # in-app expiry banner lead time
```

### Rotating a Service Token

Rotate a token to swap its secret without re-wiring its identity or scope:

```zsh
curl -X POST https://terrapod.example.com/api/v1/authentication-tokens/{token-id}/actions/rotate \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

The response carries the new secret in `attributes.token` (shown once); the old secret stops working immediately and the expiry clock resets. In the UI this is the **Rotate** action on each service token.

### Creating Tokens via API

```zsh
curl -X POST https://terrapod.example.com/api/v1/users/{user_id}/authentication-tokens \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "authentication-tokens",
      "attributes": {
        "description": "CI/CD pipeline token"
      }
    }
  }'
```

The response includes the raw token value in `attributes.token`. Store it securely -- it cannot be retrieved again.

To create a **detached** service token (admin only) scoped to specific roles:

```zsh
curl -X POST https://terrapod.example.com/api/v1/users/{admin_user}/authentication-tokens \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "authentication-tokens",
      "attributes": {
        "description": "prod deploy pipeline",
        "kind": "service_detached",
        "pinned_roles": ["prod-deployer"]
      }
    }
  }'
```

The token comes back with `"bound-to": null` and the pinned roles as its absolute scope. For a `service_bound` token, set `"kind": "service_bound"` and pick `pinned_roles` from your own roles (the effective scope is the intersection with your live access).

### Creating Tokens via Web UI

1. Navigate to **Settings > API Tokens**
2. Click **Create Token**
3. Enter a description
4. Copy the token value immediately

![API Tokens](images/api-tokens.png)

### Listing Tokens

List your own tokens:

```zsh
curl https://terrapod.example.com/api/v1/users/{user_id}/authentication-tokens \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

Admins can list all tokens across users (optionally filtered by `?kind=`):

```zsh
curl https://terrapod.example.com/api/v1/admin/authentication-tokens \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Deleting Tokens

```zsh
curl -X DELETE https://terrapod.example.com/api/v1/authentication-tokens/{token-id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Token Lifespan Configuration

```yaml
api:
  config:
    auth:
      api_token_max_ttl_hours: 8760  # upper bound on ANY token. 1 year default; 0 = no limit
      login_token_ttl_hours: 12      # lifespan of `terraform login` tokens; 0 = fall back to the cap
```

- **`api_token_max_ttl_hours`** is the hard *cap* — the maximum lifetime any token may have. It's computed at validation time as `(rotated_at or created_at) + max_ttl`, so changing it retroactively re-dates every existing token. `0` removes the cap.
- **`login_token_ttl_hours`** is the *actual* lifespan handed to the short-lived token that `terraform login` mints (default 12h). It's clamped to the cap. Set `0` to give login tokens no explicit lifespan (they then fall back to the cap — the old behaviour).

A token created with an explicit `lifespan_hours` (e.g. via the API or the create form) expires at `created_at + min(lifespan_hours, cap)`; one created without (a bare `terraform login` before this setting existed) expires at the cap.

---

## Session Management

### Session Properties

| Property | Value |
|---|---|
| Storage | Redis (`tp:session:{token}`) |
| TTL | 12 hours (sliding -- refreshed on activity, rate-limited to once per 5 minutes) |
| Scope | Web UI only |

### Configuration

```yaml
api:
  config:
    auth:
      session_ttl_hours: 12
```

### Viewing Active Sessions

Via the web UI: **Settings > Sessions**

![Active Sessions](images/sessions.png)

Via the API:

```zsh
curl https://terrapod.example.com/api/v1/auth/sessions \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Logging Out

```zsh
curl -X POST https://terrapod.example.com/api/v1/auth/logout \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

This deletes the session from Redis immediately.

---

## Requiring External SSO for Specific Roles

You can require that certain roles can only be assigned via external SSO providers (not local auth):

```yaml
api:
  config:
    auth:
      require_external_sso_for_roles:
        - admin
```

This prevents the `admin` role from being granted to users who authenticate via local password.

---

## Redis Key Reference

| Key Pattern | Purpose | TTL |
|---|---|---|
| `tp:session:{token}` | Session data (user info, roles) | 12h sliding |
| `tp:user_sessions:{email}` | Set of session tokens per user | 12h |
| `tp:auth_state:{state}` | OAuth2/SAML auth state (authorize to callback) | 5 min |
| `tp:auth_code:{code}` | One-time auth code (callback to token exchange) | 60 sec |
| `tp:recent_user:{provider}:{email}` | Recent user tracking for admin UX | 7 days |
| `tp:token_roles:{email}` | Cached roles for API token auth | 60 sec |
