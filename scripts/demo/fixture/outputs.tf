output "cluster_name" {
  value = "checkout-prod"
}

output "load_balancer_dns" {
  value = "checkout-prod-1f2e3d4c.eu-west-1.elb.amazonaws.com"
}

output "database_endpoint" {
  value = "checkout-prod.cluster-ro-c9k2mx.eu-west-1.rds.amazonaws.com"
}

output "receipts_bucket" {
  value = "checkout-prod-receipts-eu-west-1"
}
