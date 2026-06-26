variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "sql_admin_password" {
  description = "SQL Server administrator password."
  type        = string
  sensitive   = true
  default     = "P@ssw0rd-change-me"
}
