resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }
}

# FARGATE_SPOT is registered so the worker can be moved onto it with worker_use_spot without
# changing anything else; the default strategy stays on-demand FARGATE.
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# The execution role is used by the ECS agent before the container starts: pull the image,
# write to CloudWatch Logs, resolve secrets and mount EFS.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "efs_client" {
  statement {
    sid = "MountTheUploadsFileSystem"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:ClientRootAccess",
    ]
    resources = [aws_efs_file_system.uploads.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.uploads.arn]
    }
  }
}

data "aws_iam_policy_document" "execution_extra" {
  source_policy_documents = [data.aws_iam_policy_document.efs_client.json]

  statement {
    sid       = "ReadApplicationSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns
  }
}

resource "aws_iam_role_policy" "execution_extra" {
  name   = "${local.name}-execution-extra"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_extra.json
}

# The task role is what the application itself can do at runtime. This service talks to
# Postgres, Redis and the model endpoints over the network and holds no AWS credentials, so
# the EFS statement exists only because Fargate also authorizes the mount through the task
# role when the access point is used.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy" "task_efs" {
  name   = "${local.name}-task-efs"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.efs_client.json
}

locals {
  image = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"

  # FRONTEND_ORIGINS must be the browser's origin exactly. CloudFront's domain only exists
  # after the distribution is created, which Terraform resolves for us, and the custom
  # frontend domain is added when one is configured - so CORS is correct on the first apply
  # instead of after a debugging round.
  frontend_origins = join(",", compact(concat(
    ["https://${aws_cloudfront_distribution.ui.domain_name}"],
    var.frontend_domain_name == "" ? [] : ["https://${var.frontend_domain_name}"],
  )))

  # Non-secret configuration. Everything here is visible in the task definition by design;
  # tokens and URLs with credentials live in local.common_secrets instead.
  container_environment = concat(
    [
      { name = "APP_ENV", value = "production" },
      { name = "AUTHENTICATION_ENABLED", value = "true" },
      { name = "APP_NAME", value = "Mansam RAG" },
      { name = "API_PREFIX", value = "/api/v1" },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "LOG_USER_CONTENT", value = "false" },
      { name = "PORT", value = "8000" },
      { name = "FRONTEND_ORIGINS", value = local.frontend_origins },
      { name = "PUBLIC_BASE_URL", value = var.domain_name == "" ? "http://${aws_lb.main.dns_name}" : "https://${var.domain_name}" },
      { name = "LLM_PROVIDER", value = "huggingface" },
      { name = "HF_API_MODE", value = "openai" },
      { name = "HF_INFERENCE_URL", value = var.hf_inference_url },
      { name = "HF_MODEL", value = var.hf_model },
      { name = "LLM_DRAFT_MODEL", value = var.llm_draft_model },
      { name = "LLM_FAST_MODEL", value = var.llm_fast_model },
      { name = "LLM_TIMEOUT_SECONDS", value = tostring(var.llm_timeout_seconds) },
      { name = "EMBEDDING_PROVIDER", value = "huggingface" },
      { name = "EMBEDDING_INFERENCE_URL", value = var.embedding_inference_url },
      { name = "EMBEDDING_MODEL", value = var.embedding_model },
      { name = "EMBEDDING_DIMENSION", value = tostring(var.embedding_dimension) },
      { name = "RERANK_PROVIDER", value = var.rerank_provider },
      { name = "RERANK_INFERENCE_URL", value = var.rerank_inference_url },
      { name = "RERANK_MODEL", value = var.rerank_model },
      { name = "RERANK_TOP_K", value = tostring(var.rerank_top_k) },
      { name = "STT_PROVIDER", value = "disabled" },
      { name = "TTS_PROVIDER", value = "disabled" },
      { name = "OCR_PROVIDER", value = "disabled" },
      { name = "RAG_VECTOR_TOP_K", value = "20" },
      { name = "RAG_LEXICAL_TOP_K", value = "20" },
      { name = "RAG_MIN_SCORE", value = "0.35" },
      { name = "RAG_MIN_EVIDENCE", value = "1" },
      { name = "RAG_MAX_CONTEXT_CHARS", value = "16000" },
      { name = "RAG_VERIFY_ENABLED", value = tostring(var.rag_verify_enabled) },
      { name = "PLANNER_LLM_MIN_WORDS", value = "6" },
      { name = "CHUNK_TARGET_CHARS", value = "1600" },
      { name = "CHUNK_MAX_CHARS", value = "2400" },
      { name = "CHUNK_OVERLAP_CHARS", value = "200" },
      { name = "MAX_UPLOAD_SIZE_MB", value = tostring(var.max_upload_size_mb) },
      { name = "RATE_LIMIT_PER_MINUTE", value = tostring(var.rate_limit_per_minute) },
      { name = "LONG_TERM_MEMORY_ENABLED", value = "true" },
      { name = "LONG_TERM_MEMORY_INLINE", value = "false" },
      { name = "MEMORY_TTL_SECONDS", value = "86400" },
      { name = "URL_INGESTION_ENABLED", value = "false" },
      { name = "KEEP_MODEL_WARM", value = "false" },
      { name = "INGESTION_EXCLUDED_SHEETS", value = var.ingestion_excluded_sheets },
    ],
    [for key, value in var.extra_environment : { name = key, value = value }],
  )

  common_secrets = [
    { name = "JWT_SECRET", valueFrom = aws_secretsmanager_secret.jwt_secret.arn },
    { name = "HF_TOKEN", valueFrom = aws_secretsmanager_secret.hf_token.arn },
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
  ]

  # The migration task also carries the bootstrap credentials, so the first administrator
  # can be created by overriding the command on the same task definition.
  bootstrap_secrets = concat(local.common_secrets, [
    { name = "BOOTSTRAP_ADMIN_EMAIL", valueFrom = aws_secretsmanager_secret.bootstrap_admin_email.arn },
    { name = "BOOTSTRAP_ADMIN_PASSWORD", valueFrom = aws_secretsmanager_secret.bootstrap_admin_password.arn },
  ])

  # The API writes uploads/<tenant>/<uuid>-<name> under /app/uploads and stores that absolute
  # path; the worker reads it back. Both tasks mount the same access point, which is the only
  # reason the split topology works.
  uploads_volume = [
    {
      name = "uploads"
      efs_volume_configuration = {
        file_system_id     = aws_efs_file_system.uploads.id
        transit_encryption = "ENABLED"
        authorization_config = {
          access_point_id = aws_efs_access_point.uploads.id
          iam             = "ENABLED"
        }
      }
    }
  ]

  uploads_mount = [{ sourceVolume = "uploads", containerPath = "/app/uploads", readOnly = false }]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "uploads"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.uploads.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.uploads.id
        iam             = "ENABLED"
      }
    }
  }

  # No command: the image CMD runs uvicorn on 0.0.0.0:8000 with --proxy-headers.
  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.image
      essential = true

      portMappings = [{
        name          = "api"
        containerPort = 8000
        hostPort      = 8000
        protocol      = "tcp"
        appProtocol   = "http"
      }]

      environment = local.container_environment
      secrets     = local.common_secrets
      mountPoints = local.uploads_mount

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "uploads"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.uploads.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.uploads.id
        iam             = "ENABLED"
      }
    }
  }

  # Same image, different process. No port mapping: nothing should reach this task.
  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image
      essential = true

      command = [
        "celery", "-A", "app.workers.celery_app:celery_app",
        "worker", "--loglevel=INFO", "--concurrency=2",
      ]

      environment = local.container_environment
      secrets     = local.common_secrets
      mountPoints = local.uploads_mount

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  cpu                      = var.migrate_cpu
  memory                   = var.migrate_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # No EFS mount and no port: this task only touches the database. It carries the bootstrap
  # credentials so `python scripts/bootstrap_admin.py` can be run as a command override on
  # the same revision.
  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = local.image
      essential = true

      command     = ["alembic", "-c", "alembic.ini", "upgrade", "head"]
      environment = local.container_environment
      secrets     = local.bootstrap_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.migrate.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "migrate"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name             = "${local.name}-api"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.api.arn
  desired_count    = var.api_desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  # The container needs a moment before the first health check should count against it.
  health_check_grace_period_seconds = 60

  # Replace the tasks one at a time without dropping below the desired count.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # A release whose tasks never pass /health/live rolls back on its own.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  propagate_tags = "SERVICE"

  lifecycle {
    # Autoscaling owns the task count once the service exists; without this every apply
    # would fight the scaling policy.
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name             = "${local.name}-worker"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.worker.arn
  desired_count    = var.worker_desired_count
  platform_version = "LATEST"

  # A capacity provider strategy and launch_type are mutually exclusive in the ECS API, so
  # the on-demand path also goes through the strategy (FARGATE) and not through launch_type.
  capacity_provider_strategy {
    capacity_provider = var.worker_use_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  propagate_tags = "SERVICE"

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# Only the API scales automatically. The worker's useful signal is queue depth, and an idle
# worker waiting on Redis and a worker grinding through embeddings look identical on a CPU
# metric - so a naive CPU policy would not help it.
resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_min_capacity
  max_capacity       = var.api_max_capacity
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}



