resource "aws_lb" "main" {
  name               = local.name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # The default is 60 seconds, which can drop a /chat/stream connection (server-sent events)
  # that goes quiet while the model answers; a verified answer is several sequential calls.
  # The maximum is 4000.
  idle_timeout = var.alb_idle_timeout

  # Set to true once the deployment is real: with it on, `terraform destroy` needs a manual
  # change first, which is the point.
  enable_deletion_protection = false

  tags = { Name = local.name }
}

resource "aws_lb_target_group" "api" {
  name        = local.name
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # Fargate tasks register by their awsvpc address

  # Liveness only. /health/ready also pings Postgres and Redis, so using it here would turn a
  # Redis blip into a deployment failure.
  health_check {
    enabled             = true
    path                = "/health/live"
    matcher             = "200"
    protocol            = "HTTP"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # A replaced task must stop receiving requests quickly; ingestion and generation happen in
  # the API process, so the drain window stays short.
  deregistration_delay = 20

  tags = { Name = local.name }
}

# Port 80 always exists: it forwards while no certificate is configured (first light, testing
# against the ALB DNS name) and redirects to 443 once one is.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = var.certificate_arn == "" ? "forward" : "redirect"
    target_group_arn = var.certificate_arn == "" ? aws_lb_target_group.api.arn : null

    dynamic "redirect" {
      for_each = var.certificate_arn == "" ? [] : [1]

      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.certificate_arn == "" ? 0 : 1

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# The UI can only call the API over HTTPS with a certificate the browser trusts, so a custom
# API domain is the recommended shape rather than the raw ALB DNS name.
resource "aws_route53_record" "api" {
  count = var.route53_zone_id == "" || var.domain_name == "" ? 0 : 1

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
