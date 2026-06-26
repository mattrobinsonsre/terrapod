resource "aws_db_subnet_group" "analytics" {
  name       = "${var.environment}-analytics"
  subnet_ids = [for s in aws_subnet.public : s.id]

  tags = {
    Name = "${var.environment}-analytics"
  }
}

resource "aws_db_instance" "analytics" {
  identifier     = "${var.environment}-analytics"
  engine         = "postgres"
  engine_version = "16.3"
  instance_class = "db.r6g.large"

  allocated_storage = 200
  storage_type      = "gp3"

  db_name  = "analytics"
  username = "dbadmin"
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.analytics.name
  vpc_security_group_ids = [aws_security_group.database.id]

  # Risk surface for review: reachable from the internet, unencrypted at rest,
  # and no deletion protection / final snapshot.
  publicly_accessible = true
  storage_encrypted   = false
  deletion_protection = false
  skip_final_snapshot = true

  tags = {
    Name        = "${var.environment}-analytics"
    Environment = var.environment
  }
}
