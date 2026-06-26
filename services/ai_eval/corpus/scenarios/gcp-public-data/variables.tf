variable "project_id" {
  type    = string
  default = "acme-analytics-prod"
}

variable "region" {
  type    = string
  default = "europe-west1"
}

variable "db_password" {
  type      = string
  sensitive = true
  default   = "change-me-in-prod"
}
