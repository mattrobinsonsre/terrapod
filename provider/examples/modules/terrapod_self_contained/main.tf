terraform {
  required_providers {
    terrapod = {
      source = "terrapod.local/default/terrapod"
    }
  }
}

variable "terrapod_hostname" {
  description = "Runner-reachable Terrapod web BFF URL."
  type        = string
}

variable "terrapod_token" {
  description = "API token authorized to manage the test workspace."
  type        = string
  sensitive   = true
}

variable "managed_workspace_name" {
  description = "Unique name for the separate workspace managed by this catalog instance."
  type        = string
}

provider "terrapod" {
  hostname = var.terrapod_hostname
  token    = var.terrapod_token
}

resource "terrapod_workspace" "probe" {
  name              = var.managed_workspace_name
  execution_mode    = "local"
  execution_backend = "tofu"
  auto_apply        = false
}

output "managed_workspace_id" {
  value = terrapod_workspace.probe.id
}
