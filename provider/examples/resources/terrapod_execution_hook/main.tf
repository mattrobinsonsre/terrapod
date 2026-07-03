# An execution hook: a custom shell step run inside the runner Job at a fixed
# point. Define it once, then associate it with the workspaces that need it.
resource "terrapod_execution_hook" "hosts_entry" {
  name        = "internal-hosts-entry"
  description = "Add an internal registry host before init"
  hook_point  = "pre_init" # pre_init | pre_plan | post_plan | pre_apply | post_apply
  script      = "echo '10.0.0.5 registry.internal' >> /etc/hosts"
  priority    = 0
  enabled     = true
}

# Associate the hook with a workspace. Repeat (or use for_each) for a fleet.
resource "terrapod_execution_hook_workspace" "hosts_entry_prod" {
  hook_id      = terrapod_execution_hook.hosts_entry.id
  workspace_id = terrapod_workspace.example.id
}
