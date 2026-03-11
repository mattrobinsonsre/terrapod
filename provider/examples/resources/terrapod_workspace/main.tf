resource "terrapod_workspace" "example" {
  name              = "my-workspace"
  execution_mode    = "remote"
  execution_backend = "tofu"
  auto_apply        = false

  labels = {
    environment = "dev"
    team        = "platform"
  }
}
