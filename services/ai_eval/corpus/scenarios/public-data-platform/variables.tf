variable "region" {
  description = "AWS region for the data platform."
  type        = string
  default     = "eu-west-1"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR block for the platform VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "db_password" {
  description = "Master password for the analytics database."
  type        = string
  sensitive   = true
  default     = "change-me-in-prod"
}
