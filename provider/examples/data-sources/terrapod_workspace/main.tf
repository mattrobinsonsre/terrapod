data "terrapod_workspace" "existing" {
  name = "production"
}

output "workspace_id" {
  value = data.terrapod_workspace.existing.id
}
