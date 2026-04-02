# Getting Started with the Terrapod Provider

This guide walks through authenticating with your Terrapod instance and creating your first workspace using Terraform.

## Prerequisites

- A running Terrapod instance (e.g. `terrapod.example.com`)
- Terraform or OpenTofu CLI installed

## Step 1: Authenticate

```shell
terraform login terrapod.example.com
```

This initiates the PKCE OAuth flow, opens a browser for SSO, and stores an API token locally.

## Step 2: Configure the Provider

```hcl
terraform {
  required_providers {
    terrapod = {
      source  = "terrapod.example.com/default/terrapod"
      version = "~> 0.3"
    }
  }
}

provider "terrapod" {
  hostname = "terrapod.example.com"
  # Token is read automatically from terraform login credentials
}
```

## Step 3: Create a Workspace

```hcl
resource "terrapod_workspace" "example" {
  name              = "my-first-workspace"
  execution_mode    = "agent"
  execution_backend = "tofu"
  auto_apply        = false

  labels = {
    environment = "dev"
    team        = "platform"
  }
}
```

## Step 4: Apply

```shell
terraform init
terraform plan
terraform apply
```

## Environment Variables

| Variable | Description |
|---|---|
| `TERRAPOD_HOSTNAME` | Terrapod instance hostname |
| `TERRAPOD_TOKEN` | API token (overrides terraform login) |
| `TERRAPOD_SKIP_TLS_VERIFY` | Skip TLS verification (`true`/`1`) |
