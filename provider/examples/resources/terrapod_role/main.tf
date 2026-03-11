resource "terrapod_role" "dev_writer" {
  name                 = "dev-writer"
  description          = "Write access to dev workspaces"
  workspace_permission = "write"

  allow_labels = {
    environment = "dev"
  }
}
