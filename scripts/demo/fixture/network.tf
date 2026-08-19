# ── Load balancing + DNS ────────────────────────────────────────────

resource "aws_lb" "public" {
  name                       = "${local.service}-${local.env}-alb"
  internal                   = false
  load_balancer_type         = "application"
  subnets                    = local.subnets
  security_groups            = [aws_security_group.web.id]
  enable_deletion_protection = true
  tags                       = local.tags
}

resource "aws_lb_target_group" "web" {
  name        = "${local.service}-web-tg"
  port        = 8443
  protocol    = "HTTPS"
  vpc_id      = local.vpc_id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = local.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.public.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = "arn:aws:acm:eu-west-1:123456789012:certificate/8f1d4c2a-7b3e-4a19-9c58-2e6f0d7a1b34"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_route53_record" "web" {
  zone_id = "Z08184481D5XHKIS55UKU"
  name    = "checkout.example.com"
  type    = "A"

  alias {
    name                   = aws_lb.public.dns_name
    zone_id                = aws_lb.public.zone_id
    evaluate_target_health = true
  }
}
