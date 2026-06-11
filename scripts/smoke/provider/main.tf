# Provider smoke fixture — exercises every terrapod_* resource that
# went through the go-terrapod migration in #347. Run via
# `scripts/smoke/smoke-provider.sh`; expects the Tilt-managed
# Terrapod stack at https://terrapod.local.
#
# This fixture is deliberately minimal-but-end-to-end: every resource
# gets created, read, and destroyed. The point is to catch any
# round-trip damage from the SDK rewrite, not to model a realistic
# Terrapod deployment.

terraform {
  required_providers {
    terrapod = {
      source = "mattrobinsonsre/terrapod"
      # No version constraint: the smoke runs against a locally-
      # built binary wired via dev_overrides; the registry version
      # is irrelevant.
    }
  }
}

provider "terrapod" {
  hostname        = "terrapod.local"
  skip_tls_verify = true
  # Token is supplied via TERRAPOD_TOKEN env var; no inline token
  # so the fixture is safe to check in.
}

variable "smoke_id" {
  type        = string
  description = "Suffix appended to every resource name so the smoke can run repeatedly without name collisions."
  default     = "smoke"
}

# ── Workspace ────────────────────────────────────────────────────────
resource "terrapod_workspace" "main" {
  name              = "provider-${var.smoke_id}"
  execution_mode    = "local"
  terraform_version = "1.12"
  labels = {
    test  = "true"
    smoke = var.smoke_id
  }
}

# ── Variable on the workspace (non-sensitive + sensitive) ────────────
resource "terrapod_variable" "region" {
  workspace_id = terrapod_workspace.main.id
  key          = "region"
  value        = "eu-west-1"
  category     = "terraform"
}

resource "terrapod_variable" "secret" {
  workspace_id = terrapod_workspace.main.id
  key          = "db_password"
  value        = "smoke-secret"
  category     = "terraform"
  sensitive    = true
}

# ── Variable set with a variable assigned to the workspace ───────────
resource "terrapod_variable_set" "shared" {
  name        = "shared-${var.smoke_id}"
  description = "Shared vars for the provider smoke"
  global      = false
  priority    = false
}

resource "terrapod_variable_set_variable" "shared_var" {
  varset_id = terrapod_variable_set.shared.id
  key       = "TF_LOG"
  value     = "INFO"
  category  = "env"
}

resource "terrapod_variable_set_workspace" "shared_to_main" {
  varset_id    = terrapod_variable_set.shared.id
  workspace_id = terrapod_workspace.main.id
}

# ── Agent pool + token ───────────────────────────────────────────────
resource "terrapod_agent_pool" "main" {
  name        = "provider-${var.smoke_id}"
  description = "Provider smoke pool"
  owner_email = var.smoke_email
  labels = {
    smoke = var.smoke_id
  }
}

resource "terrapod_agent_pool_token" "main" {
  pool_id     = terrapod_agent_pool.main.id
  description = "smoke"
  max_uses    = 10
}

# ── Custom role + assignment ─────────────────────────────────────────
resource "terrapod_role" "smoke" {
  name                 = "smoke-${var.smoke_id}"
  description          = "Provider smoke role"
  workspace_permission = "read"
  pool_permission      = "read"
  allow_labels = {
    smoke = var.smoke_id
  }
}

# Optional: a role assignment that references the smoke role.
# The provider-smoke uses the migrating-user's email by default so
# the smoke doesn't need a second user to exist.
variable "smoke_email" {
  type    = string
  default = "admin@example.com"
}

resource "terrapod_role_assignment" "smoke" {
  provider_name = "local"
  email         = var.smoke_email
  role_name     = terrapod_role.smoke.name
}

# ── Notification configuration ───────────────────────────────────────
resource "terrapod_notification_configuration" "main" {
  workspace_id     = terrapod_workspace.main.id
  name             = "smoke-${var.smoke_id}"
  destination_type = "generic"
  url              = "https://example.invalid/webhook"
  enabled          = false
  triggers         = ["run:completed", "run:errored"]
}

# ── GPG key ──────────────────────────────────────────────────────────
# A short test public key so the GPG-key resource has something to
# bite on. Source: the project's own test signing key (not used
# in production).
resource "terrapod_gpg_key" "smoke" {
  ascii_armor = file("${path.module}/test-gpg-key.asc")
  namespace   = "default"
  source      = "smoke"
}

# ── Outputs ──────────────────────────────────────────────────────────
output "workspace_id" {
  value = terrapod_workspace.main.id
}

output "agent_pool_id" {
  value = terrapod_agent_pool.main.id
}

output "agent_pool_token" {
  value     = terrapod_agent_pool_token.main.token
  sensitive = true
}
