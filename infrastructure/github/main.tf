terraform {
  required_version = ">= 1.11, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.73.0" }
  }
}

variable "allowed_account_id" {
  type      = string
  sensitive = true
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "project" {
  type    = string
  default = "cahuita-lab"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,25}$", var.project))
    error_message = "Use the same lowercase project slug as the laboratory."
  }
}
variable "github_subject" {
  type        = string
  description = "Exact GitHub OIDC sub claim for the repository's laboratory environment."
  validation {
    condition     = can(regex("^repo:[A-Za-z0-9_.@-]+/[A-Za-z0-9_.@-]+:environment:laboratory$", var.github_subject))
    error_message = "Supply an exact repository/environment subject without wildcards."
  }
}
variable "existing_oidc_provider_arn" {
  type        = string
  default     = null
  description = "Reuse the account's GitHub provider when it already exists."
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.allowed_account_id]
}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.existing_oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  account = data.aws_caller_identity.current.account_id
  prefix  = "arn:${data.aws_partition.current.partition}"
  oidc    = var.existing_oidc_provider_arn != null ? var.existing_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
}

resource "aws_iam_role" "github" {
  name_prefix = "cahuita-github-"
  description = "Dedicated laboratory deployment role for one GitHub environment"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.oidc }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = var.github_subject
      } }
    }]
  })
  tags = { Project = var.project, ManagedBy = "Terraform" }
}

resource "aws_iam_role_policy" "deployment" {
  role = aws_iam_role.github.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DedicatedLabCompute", Effect = "Allow", Action = ["ec2:*"], Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      },
      {
        Sid      = "ProjectBuckets", Effect = "Allow", Action = ["s3:*"]
        Resource = ["${local.prefix}:s3:::${var.project}-*", "${local.prefix}:s3:::${var.project}-*/*"]
      },
      {
        Sid = "InstanceRoles", Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
          "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
          "iam:ListAttachedRolePolicies", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
          "iam:ListInstanceProfilesForRole"
        ]
        Resource = "${local.prefix}:iam::${local.account}:role/${var.project}-instance-*"
      },
      {
        Sid = "InstanceProfiles", Effect = "Allow"
        Action = [
          "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
          "iam:TagInstanceProfile", "iam:UntagInstanceProfile", "iam:AddRoleToInstanceProfile",
          "iam:RemoveRoleFromInstanceProfile"
        ]
        Resource = "${local.prefix}:iam::${local.account}:instance-profile/${var.project}-*"
      },
      {
        Sid       = "PassOnlyLabInstanceRoles", Effect = "Allow", Action = "iam:PassRole"
        Resource  = "${local.prefix}:iam::${local.account}:role/${var.project}-instance-*"
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Sid       = "LabCommands", Effect = "Allow", Action = "ssm:SendCommand"
        Resource  = "${local.prefix}:ec2:${var.region}:${local.account}:instance/*"
        Condition = { StringEquals = { "ssm:resourceTag/Project" = var.project } }
      },
      {
        Sid      = "CommandDocument", Effect = "Allow", Action = "ssm:SendCommand"
        Resource = "${local.prefix}:ssm:${var.region}::document/AWS-RunShellScript"
      },
      {
        Sid      = "ReadCommandProgress", Effect = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:ListCommands", "ssm:ListCommandInvocations", "ssm:DescribeInstanceInformation"]
        Resource = "*"
      }
    ]
  })
}

output "role_arn" {
  value     = aws_iam_role.github.arn
  sensitive = true
}
