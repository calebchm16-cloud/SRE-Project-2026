terraform {
  required_version = ">= 1.11, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.73.0" }
  }
}
variable "allowed_account_id" { type = string }
variable "region" { type = string }
variable "project" { type = string }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.allowed_account_id]
}
resource "aws_s3_bucket" "state" {
  bucket_prefix = "${var.project}-state-"
  tags          = { Project = var.project, ManagedBy = "Terraform", Purpose = "Private Terraform state" }
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
output "bucket" { value = aws_s3_bucket.state.id }
