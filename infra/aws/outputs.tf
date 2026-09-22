output "region" {
  description = "Region every resource lives in (except a CloudFront certificate)."
  value       = var.aws_region
}

output "project" {
  description = "Name prefix shared by every resource."
  value       = local.name
}

output "ecr_repository_url" {
  description = "Push the image here: docker buildx build -t <url>:<tag> --push ."
  value       = aws_ecr_repository.api.repository_url
}

output "image_uri" {
  description = "Image the task definitions currently point at."
  value       = local.image
}

output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.main.name
}

output "api_service_name" {
  description = "ECS service running uvicorn."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "ECS service running Celery."
  value       = aws_ecs_service.worker.name
}

output "api_task_definition" {
  description = "API task definition family (aws ecs run-task / update-service accept it; append :revision to pin)."
  value       = aws_ecs_task_definition.api.family
}

output "worker_task_definition" {
  description = "Worker task definition family."
  value       = aws_ecs_task_definition.worker.family
}

output "migrate_task_definition" {
  description = "Task definition used for the migration and the admin bootstrap."
  value       = aws_ecs_task_definition.migrate.family
}

output "private_subnet_ids" {
  description = "Comma-separated private subnet ids for aws ecs run-task --network-configuration."
  value       = join(",", aws_subnet.private[*].id)
}

output "task_security_group_id" {
  description = "Security group attached to the ECS tasks."
  value       = aws_security_group.tasks.id
}

output "alb_dns_name" {
  description = "Load balancer DNS name; use it over http:// until a certificate and domain exist."
  value       = aws_lb.main.dns_name
}

output "api_url" {
  description = "Base URL the browser should call (the API prefix is /api/v1)."
  value       = var.domain_name == "" ? "http://${aws_lb.main.dns_name}" : "https://${var.domain_name}"
}

output "frontend_bucket" {
  description = "S3 bucket the UI bundle is synced into."
  value       = aws_s3_bucket.frontend.bucket
}

output "cloudfront_domain" {
  description = "CloudFront domain serving the UI."
  value       = aws_cloudfront_distribution.ui.domain_name
}

output "cloudfront_distribution_id" {
  description = "Distribution id for cache invalidations."
  value       = aws_cloudfront_distribution.ui.id
}

output "secret_arns" {
  description = "Secrets the tasks read; rotate by adding a new version and forcing a new deployment."
  value       = local.secret_arns
}
