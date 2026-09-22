resource "random_password" "jwt_secret" {
  length  = 64
  special = false
}

# One secret per value. A task definition resolves each ARN to exactly one environment
# variable, so bundling these into a JSON document would only add parsing code.
resource "aws_secretsmanager_secret" "jwt_secret" {
  name                    = "${local.name}/jwt-secret"
  description             = "JWT signing secret (at least 32 characters)"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "jwt_secret" {
  secret_id     = aws_secretsmanager_secret.jwt_secret.id
  secret_string = random_password.jwt_secret.result
}

resource "aws_secretsmanager_secret" "hf_token" {
  name                    = "${local.name}/hf-token"
  description             = "Token for the hosted generation and embedding endpoints"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "hf_token" {
  secret_id     = aws_secretsmanager_secret.hf_token.id
  secret_string = var.hf_token
}

# Composed here rather than in tfvars so the password never appears in a file. The scheme is
# postgresql+asyncpg because app/database/session.py creates an async engine, and ?ssl=require
# is what RDS for PostgreSQL 15+ expects (rds.force_ssl=1 in the parameter group).
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.name}/database-url"
  description             = "SQLAlchemy async URL for the RDS instance"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+asyncpg://%s:%s@%s:5432/%s?ssl=require",
    var.db_username,
    random_password.db.result,
    aws_db_instance.main.address,
    var.db_name,
  )
}

# No AUTH token: ElastiCache only accepts one when in-transit encryption is enabled, and the
# application cannot verify the ElastiCache CA without a code change. See elasticache.tf.
resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${local.name}/redis-url"
  description             = "Celery broker, result backend and rate-limit store"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id = aws_secretsmanager_secret.redis_url.id
  secret_string = format(
    "redis://%s:6379/0",
    aws_elasticache_replication_group.main.primary_endpoint_address,
  )
}

resource "aws_secretsmanager_secret" "bootstrap_admin_email" {
  name                    = "${local.name}/bootstrap-admin-email"
  description             = "First administrator created by scripts/bootstrap_admin.py; removable afterwards"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "bootstrap_admin_email" {
  secret_id     = aws_secretsmanager_secret.bootstrap_admin_email.id
  secret_string = var.bootstrap_admin_email
}

resource "aws_secretsmanager_secret" "bootstrap_admin_password" {
  name                    = "${local.name}/bootstrap-admin-password"
  description             = "First administrator password; removable after the bootstrap task runs"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "bootstrap_admin_password" {
  secret_id     = aws_secretsmanager_secret.bootstrap_admin_password.id
  secret_string = var.bootstrap_admin_password
}

locals {
  # Consumed by the execution role policy in ecs.tf: a task can only read these secrets.
  secret_arns = [
    aws_secretsmanager_secret.jwt_secret.arn,
    aws_secretsmanager_secret.hf_token.arn,
    aws_secretsmanager_secret.database_url.arn,
    aws_secretsmanager_secret.redis_url.arn,
    aws_secretsmanager_secret.bootstrap_admin_email.arn,
    aws_secretsmanager_secret.bootstrap_admin_password.arn,
  ]
}
