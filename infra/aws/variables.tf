# ---------------------------------------------------------------------------------------
# Naming and location
# ---------------------------------------------------------------------------------------
variable "aws_region" {
  description = "Region for every resource except a CloudFront certificate (which is us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "mansam-rag"
}

variable "environment" {
  description = "Environment suffix (prod, staging)."
  type        = string
  default     = "prod"
}

variable "tags" {
  description = "Extra tags merged into the default tags."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------------------
variable "vpc_cidr" {
  description = "CIDR block of the VPC that holds the ALB, the tasks and both data services."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = "Availability zones to spread the subnets across (2 is the minimum for RDS/ElastiCache subnet groups)."
  type        = number
  default     = 2
}

# ---------------------------------------------------------------------------------------
# Container image and task sizing
# ---------------------------------------------------------------------------------------
variable "image_tag" {
  description = "Tag of the image in the ECR repository. Set this to the commit SHA of the release."
  type        = string
  default     = "latest"
}

variable "api_cpu" {
  description = "CPU units for the API task (1024 = 1 vCPU)."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Memory (MiB) for the API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Baseline number of API tasks. Autoscaling moves between api_min_capacity and api_max_capacity."
  type        = number
  default     = 1
}

variable "api_min_capacity" {
  description = "Lower bound for API autoscaling."
  type        = number
  default     = 1
}

variable "api_max_capacity" {
  description = "Upper bound for API autoscaling."
  type        = number
  default     = 3
}

variable "worker_cpu" {
  description = "CPU units for the Celery worker task."
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Memory (MiB) for the Celery worker task. PDF/XLSX extraction is the spike."
  type        = number
  default     = 1024
}

variable "worker_desired_count" {
  description = "Number of worker tasks. Keep at 1 until a queue-depth metric drives scaling."
  type        = number
  default     = 1
}

variable "worker_use_spot" {
  description = "Run the worker on FARGATE_SPOT. Ingestion retries through Celery acks_late, so an interrupted task is re-run."
  type        = bool
  default     = false
}

variable "migrate_cpu" {
  description = "CPU units for the one-off migration/bootstrap task."
  type        = number
  default     = 256
}

variable "migrate_memory" {
  description = "Memory (MiB) for the one-off migration/bootstrap task."
  type        = number
  default     = 512
}

# ---------------------------------------------------------------------------------------
# Data services
# ---------------------------------------------------------------------------------------
variable "db_name" {
  description = "Database name created inside the RDS instance."
  type        = string
  default     = "rag"
}

variable "db_username" {
  description = "RDS master user. Must be the master: CREATE EXTENSION vector needs rds_superuser at migration time."
  type        = string
  default     = "rag"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS storage in GiB."
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. 0 disables backups."
  type        = number
  default     = 7
}

variable "db_multi_az" {
  description = "Run a standby RDS instance in a second AZ (roughly doubles the database cost)."
  type        = bool
  default     = false
}

variable "db_deletion_protection" {
  description = "Block accidental deletion. Must be disabled explicitly before terraform destroy succeeds."
  type        = bool
  default     = true
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_engine_version" {
  description = "Redis engine version for the replication group."
  type        = string
  default     = "7.1"
}

variable "efs_posix_uid" {
  description = "UID the EFS access point performs file operations as. 0 with 0777 works for the image's non-root app user; see docs/DEPLOY-AWS.md B5."
  type        = number
  default     = 0
}

variable "efs_posix_gid" {
  description = "GID the EFS access point performs file operations as."
  type        = number
  default     = 0
}

variable "efs_root_permissions" {
  description = "Permissions of the EFS access point root directory."
  type        = string
  default     = "0777"
}

# ---------------------------------------------------------------------------------------
# Application configuration (non-secret)
# ---------------------------------------------------------------------------------------
variable "hf_token" {
  description = "Hugging Face (or other OpenAI-compatible provider) token. Required outside development."
  type        = string
  sensitive   = true
}

variable "hf_inference_url" {
  description = "Base URL of the OpenAI-compatible chat endpoint, without /v1: the client appends /v1/chat/completions."
  type        = string
  default     = "https://router.huggingface.co"
}

variable "hf_model" {
  description = "Model id used for the answer draft."
  type        = string
}

variable "llm_draft_model" {
  description = "Optional model for the answer draft when it should differ from hf_model."
  type        = string
  default     = ""
}

variable "llm_fast_model" {
  description = "Optional smaller model for the planner, verifier and memory extraction."
  type        = string
  default     = ""
}

variable "llm_timeout_seconds" {
  description = "Per-call timeout for generation."
  type        = number
  default     = 60
}

variable "embedding_inference_url" {
  description = "Embedding endpoint: a TEI /embed URL (native payload) or any OpenAI-compatible /v1/embeddings."
  type        = string
}

variable "embedding_model" {
  description = "Embedding model id."
  type        = string
  default     = "intfloat/multilingual-e5-large"
}

variable "embedding_dimension" {
  description = "Embedding dimension; must match the model and must not change after data is indexed."
  type        = number
  default     = 1024
}

variable "rerank_provider" {
  description = "Reranking provider; 'disabled' runs retrieval on reciprocal rank fusion alone."
  type        = string
  default     = "disabled"
}

variable "rerank_inference_url" {
  description = "Hosted reranker URL (a URL ending in /rerank is treated as Text Embeddings Inference). Empty disables the stage."
  type        = string
  default     = ""
}

variable "rerank_model" {
  description = "Reranker model id."
  type        = string
  default     = "BAAI/bge-reranker-v2-m3"
}

variable "rerank_top_k" {
  description = "Chunks kept after reranking. 8 is the measured floor for this corpus."
  type        = number
  default     = 8
}

variable "rag_verify_enabled" {
  description = "Run the independent verification generation (best grounding, about double the latency)."
  type        = bool
  default     = true
}

variable "rate_limit_per_minute" {
  description = "Per-client requests per minute enforced in Redis."
  type        = number
  default     = 30
}

variable "max_upload_size_mb" {
  description = "Maximum upload size."
  type        = number
  default     = 25
}

variable "ingestion_excluded_sheets" {
  description = "Comma-separated spreadsheet sheets kept out of the index."
  type        = string
  default     = ""
}

variable "extra_environment" {
  description = "Additional non-secret container environment variables (name = value)."
  type        = map(string)
  default     = {}
}

variable "bootstrap_admin_email" {
  description = "Email of the first administrator created by scripts/bootstrap_admin.py."
  type        = string
}

variable "bootstrap_admin_password" {
  description = "Password of the first administrator."
  type        = string
  sensitive   = true
}

# ---------------------------------------------------------------------------------------
# Edge, DNS and observability
# ---------------------------------------------------------------------------------------
variable "alb_idle_timeout" {
  description = "ALB idle timeout in seconds. 300 keeps a /chat/stream connection alive between events."
  type        = number
  default     = 300
}

variable "certificate_arn" {
  description = "ACM certificate ARN in aws_region for the API domain. Empty serves the ALB over plain HTTP (testing only)."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "API hostname, e.g. api.example.com. Empty creates no DNS record."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Hosted zone that holds domain_name and frontend_domain_name."
  type        = string
  default     = ""
}

variable "frontend_domain_name" {
  description = "UI hostname served by CloudFront, e.g. rag.example.com."
  type        = string
  default     = ""
}

variable "frontend_certificate_arn" {
  description = "ACM certificate ARN for frontend_domain_name. CloudFront requires a certificate in us-east-1."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the API, worker and migration log groups."
  type        = number
  default     = 30
}

variable "enable_container_insights" {
  description = "ECS Container Insights (per-task metrics with an extra CloudWatch cost)."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Email subscribed to the CloudWatch alarm topic. Empty creates no topic."
  type        = string
  default     = ""
}


