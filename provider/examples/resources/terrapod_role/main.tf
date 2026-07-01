resource "terrapod_role" "dev_writer" {
  name                 = "dev-writer"
  description          = "Write access to dev workspaces"
  workspace_permission = "write"

  allow_labels = {
    environment = "dev"
  }
}

# Capability-based role (#585): author the grant directly as explicit
# "resource:verb" tokens instead of the preset permission levels. When
# `capabilities` is set it is the source of truth; the *_permission level
# fields are then server-derived as a summary (a preset name, or the literal
# "custom" when the capabilities match no preset) and must not be set here —
# they are computed. Omit `capabilities` to author by level instead.
resource "terrapod_role" "custom_scoped" {
  name        = "custom-scoped"
  description = "Fine-grained grant authored via capabilities"

  capabilities = [
    "workspace:read",
    "run:read",
    "run:plan",
  ]

  allow_labels = {
    environment = "staging"
  }
}
