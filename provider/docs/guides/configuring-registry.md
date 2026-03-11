# Configuring Your Registry

Every Terrapod instance serves the Terrapod provider from its own registry. This guide explains how `terraform init` discovers and downloads the provider.

## How Discovery Works

1. Terraform reads the `source` field in `required_providers`:
   ```hcl
   source = "terrapod.example.com/default/terrapod"
   ```

2. It fetches `https://terrapod.example.com/.well-known/terraform.json` to discover the registry protocol endpoints.

3. It calls the version list endpoint to find available versions.

4. It downloads the binary for the current platform.

## How the PKCE Login Flow Works

When you run `terraform login terrapod.example.com`:

1. Terraform fetches `/.well-known/terraform.json` for OAuth configuration
2. Opens a browser to `GET /oauth/authorize` with a PKCE challenge
3. You authenticate via SSO (OIDC/SAML) or local password
4. The callback generates a one-time auth code
5. Terraform exchanges the code at `POST /oauth/token` for an API token
6. The token is stored in `~/.terraform.d/credentials.tfrc.json`

The same token is used by the provider for API calls.

## Self-Signed Certificates

For development instances with self-signed TLS certificates:

```hcl
provider "terrapod" {
  hostname        = "terrapod.local"
  skip_tls_verify = true
}
```

Or via environment variable:

```shell
export TERRAPOD_SKIP_TLS_VERIFY=true
```

For `terraform init` to trust the self-signed cert, create a CLI config:

```hcl
# ~/.terraformrc
host "terrapod.local" {
  services = {
    "providers.v1" = "https://terrapod.local/api/v2/registry/providers/"
  }
}
```
