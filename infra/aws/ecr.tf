# One repository for the API, the worker and the migration task: they are the same image
# with different commands, so there is nothing to keep in sync between them.
resource "aws_ecr_repository" "api" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = local.name }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  # Every ingestion deploy pushes a new tag; without a lifecycle policy the repository
  # grows without bound.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the most recent 15 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 15
        }
        action = { type = "expire" }
      }
    ]
  })
}
