resource "terrapod_workspace" "example" {
  name              = "my-workspace"
  execution_mode    = "agent"
  execution_backend = "tofu"
  auto_apply_mode   = "never"

  labels = {
    environment = "dev"
    team        = "platform"
  }
}
