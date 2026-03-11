terraform {
  required_providers {
    terrapod = {
      source  = "terrapod.example.com/default/terrapod"
      version = "~> 0.3"
    }
  }
}

provider "terrapod" {
  hostname = "terrapod.example.com"
  # Token from terraform login or TERRAPOD_TOKEN env var
}
