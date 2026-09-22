terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.60" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  # Uncomment before this state is shared with a CI runner. Local state cannot be used by
  # a second operator or by .github/workflows/deploy-aws.yml, and losing it means the
  # infrastructure has to be imported by hand.
  #
  # backend "s3" {
  #   bucket         = "REPLACE-with-your-state-bucket"
  #   key            = "mansam-rag/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "REPLACE-with-your-lock-table"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

# A certificate for CloudFront must live in us-east-1 regardless of where the stack runs,
# so the frontend module needs a second provider alias when the distribution has a
# custom domain.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.tags
  }
}

locals {
  name = "${var.project}-${var.environment}"

  tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "rag-from-scratch"
    },
    var.tags,
  )
}
