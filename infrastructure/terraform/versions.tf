terraform {
  required_version = ">= 1.11, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.73.0"
    }
  }
  # The helper adds a private S3 backend in its ignored working directory for Actions.
  # Direct terminal use defaults to local state; never add state files to Git.
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.allowed_account_id]
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "Terraform"
      Purpose   = "SRE Academy ephemeral laboratory"
    }
  }
}
