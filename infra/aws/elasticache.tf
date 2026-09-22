resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = local.name
  description          = "Celery broker, rate-limit counters and short-term conversation memory"

  engine         = "redis"
  engine_version = var.redis_engine_version
  node_type      = var.redis_node_type
  port           = 6379

  num_cache_clusters         = 1
  automatic_failover_enabled = false # requires two nodes
  multi_az_enabled           = false
  parameter_group_name       = "default.redis7"

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true

  # In-transit encryption is off, and therefore no AUTH token is set: ElastiCache only
  # accepts an auth token when transit encryption is enabled, and redis-py 5 defaults
  # ssl_cert_reqs to "required" — so a rediss:// URL fails certificate verification
  # against the ElastiCache CA unless the application is changed to pass ssl_ca_certs
  # (app/dependencies.py and the Celery broker URL). What protects the connection today is
  # the private subnet group plus the security group that only the ECS tasks can reach.
  # See docs/DEPLOY-AWS.md B4.
  transit_encryption_enabled = false

  apply_immediately = true

  tags = { Name = local.name }
}
