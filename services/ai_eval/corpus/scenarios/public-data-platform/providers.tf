# Plan-only fixture generation: skip all credential/network validation so a
# `tofu plan` of resource creates never reaches AWS. No real infrastructure is
# created — the output is fed to the AI-analysis eval harness.
provider "aws" {
  region = var.region

  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true

  default_tags {
    tags = {
      Project   = "data-platform"
      ManagedBy = "opentofu"
    }
  }
}
