# ── Security groups ─────────────────────────────────────────────────

resource "aws_security_group" "web" {
  name        = "${local.service}-web-sg"
  description = "Public web tier"
  vpc_id      = local.vpc_id
  tags        = local.tags

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "internal" {
  name        = "${local.service}-internal-sg"
  description = "Internal service tier"
  vpc_id      = local.vpc_id
  tags        = local.tags

  ingress {
    description = "From the web tier"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
}

resource "aws_kms_key" "data" {
  description             = "checkout data encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}
