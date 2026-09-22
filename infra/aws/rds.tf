# The password ends up inside DATABASE_URL, so it must stay URL-safe: no special characters.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = local.name }
}

resource "aws_db_parameter_group" "main" {
  name        = local.name
  family      = "postgres16"
  description = "TLS-enforcing parameters for the Mansam RAG database"

  # RDS for PostgreSQL 15+ enables this by default; setting it explicitly means a custom
  # parameter group cannot silently drop it. The application connects with ?ssl=require,
  # which asyncpg accepts without a CA bundle.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = { Name = local.name }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16" # Terraform resolves this to the current 16.x; pin the full version if the plan reports a difference after apply

  instance_class    = var.db_instance_class
  allocated_storage = var.db_allocated_storage
  # Storage autoscaling: grow automatically instead of failing an ingestion run.
  max_allocated_storage = var.db_allocated_storage * 5
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  # CREATE EXTENSION vector needs rds_superuser, so DATABASE_URL uses this master user.
  # See docs/DEPLOY-AWS.md B3 before introducing a restricted application user.
  publicly_accessible        = false
  vpc_security_group_ids     = [aws_security_group.database.id]
  db_subnet_group_name       = aws_db_subnet_group.main.name
  parameter_group_name       = aws_db_parameter_group.main.name
  multi_az                   = var.db_multi_az
  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:04:30-sun:05:30"
  auto_minor_version_upgrade = true
  allow_major_version_upgrade = false
  apply_immediately           = false

  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"
  copy_tags_to_snapshot     = true

  tags = { Name = local.name }

  lifecycle {
    # Rotating the password is a deliberate act (and a redeploy), not something an apply
    # should do behind your back; the pgvector extension is created by migration 0001.
    ignore_changes = [password]
  }
}
