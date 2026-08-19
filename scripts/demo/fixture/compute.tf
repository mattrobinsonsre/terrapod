# ── Compute tier ────────────────────────────────────────────────────
# The ECS service is REPLACED by the plan (its task definition and launch
# type change), which is the change a reviewer most wants an approval gate on.

resource "aws_ecs_cluster" "main" {
  name = "${local.service}-${local.env}"
  tags = local.tags

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_service" "web" {
  name            = "${local.service}-web"
  cluster         = aws_ecs_cluster.main.arn
  task_definition = "arn:aws:ecs:eu-west-1:123456789012:task-definition/checkout-web:41"
  desired_count   = 6
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = local.subnets
    security_groups  = [aws_security_group.web.id]
    assign_public_ip = false
  }

  tags = local.tags
}

resource "aws_ecs_service" "worker" {
  name            = "${local.service}-worker"
  cluster         = aws_ecs_cluster.main.arn
  task_definition = "arn:aws:ecs:eu-west-1:123456789012:task-definition/checkout-worker:17"
  desired_count   = 3
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = local.subnets
    security_groups  = [aws_security_group.internal.id]
    assign_public_ip = false
  }

  tags = local.tags
}

resource "aws_appautoscaling_target" "web" {
  max_capacity       = 20
  min_capacity       = 4
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.web.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${local.service}-web"
  retention_in_days = 30
  tags              = local.tags
}
