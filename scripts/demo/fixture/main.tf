# checkout — production service estate, eu-west-1.
#
# ECS services behind a public ALB, an Aurora cluster, ElastiCache for
# sessions, and an encrypted receipts bucket.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "eu-west-1"
  # Credentials come from the environment, never from this block.
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
}

locals {
  env     = "prod"
  service = "checkout"
  vpc_id  = "vpc-0b4a1f3cfe0c88a8d"
  subnets = ["subnet-06936007499bd9e12", "subnet-08c220af8bcfb67c4", "subnet-07bb93bc854ef1948"]
  tags = {
    Service     = "checkout"
    Environment = "prod"
    ManagedBy   = "terrapod"
  }
}
