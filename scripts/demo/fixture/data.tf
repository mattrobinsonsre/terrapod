# ── Data tier ───────────────────────────────────────────────────────
# The Aurora cluster is UPDATED in place (backup retention lengthens), which
# gives the plan a third change kind alongside create/replace/destroy.

resource "aws_rds_cluster" "main" {
  cluster_identifier      = "${local.service}-${local.env}"
  engine                  = "aurora-postgresql"
  engine_version          = "16.4"
  database_name           = "checkout"
  master_username         = "checkout_app"
  storage_encrypted       = true
  backup_retention_period = 30
  preferred_backup_window = "02:00-03:00"
  skip_final_snapshot     = false
  tags                    = local.tags
}

resource "aws_rds_cluster_instance" "writer" {
  identifier          = "${local.service}-${local.env}-writer"
  cluster_identifier  = aws_rds_cluster.main.id
  instance_class      = "db.r6g.xlarge"
  engine              = aws_rds_cluster.main.engine
  publicly_accessible = false
  tags                = local.tags
}

resource "aws_rds_cluster_instance" "reader" {
  identifier          = "${local.service}-${local.env}-reader"
  cluster_identifier  = aws_rds_cluster.main.id
  instance_class      = "db.r6g.xlarge"
  engine              = aws_rds_cluster.main.engine
  publicly_accessible = false
  tags                = local.tags
}

resource "aws_elasticache_replication_group" "sessions" {
  replication_group_id       = "${local.service}-sessions"
  description                = "checkout session cache"
  engine                     = "redis"
  node_type                  = "cache.r7g.large"
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  tags                       = local.tags
}

resource "aws_s3_bucket" "receipts" {
  bucket = "${local.service}-${local.env}-receipts"
  tags   = local.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
  }
}
