mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a"] }
  }
}

variables {
  allowed_account_id = "123456789012"
}

run "private_administration_and_ephemeral_storage" {
  command = plan
  assert {
    condition     = aws_instance.lab.associate_public_ip_address && aws_instance.lab.instance_type == "t4g.medium"
    error_message = "The lab must use an auto-assigned IP and a supported ARM instance."
  }
  assert {
    condition     = aws_instance.lab.root_block_device[0].encrypted && aws_instance.lab.root_block_device[0].delete_on_termination
    error_message = "EBS must be encrypted and removed with this ephemeral instance."
  }
  assert {
    condition     = alltrue([for r in aws_vpc_security_group_ingress_rule.web : r.from_port == 80 && r.to_port == 80])
    error_message = "Only the read-only HTTP application can be exposed."
  }
  assert {
    condition     = aws_instance.lab.metadata_options[0].http_tokens == "required" && aws_s3_bucket_public_access_block.releases.block_public_policy
    error_message = "Require IMDSv2 and private release transfers."
  }
}

run "amd64_variant" {
  command = plan
  variables { architecture = "amd64" }
  assert {
    condition     = aws_instance.lab.instance_type == "t3.medium"
    error_message = "The AMD64 image needs a matching node architecture."
  }
}
