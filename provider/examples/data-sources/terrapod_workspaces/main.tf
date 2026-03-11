data "terrapod_workspaces" "dev" {
  search = "dev-"
}

output "dev_workspaces" {
  value = data.terrapod_workspaces.dev.workspaces[*].name
}
