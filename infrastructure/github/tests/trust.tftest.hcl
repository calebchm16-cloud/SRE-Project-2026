mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}
variables {
  allowed_account_id         = "123456789012"
  github_subject             = "repo:example@100/cahuita-conditions@200:environment:laboratory"
  existing_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
}
run "exact_environment_trust" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role.github.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == var.github_subject
    error_message = "Trust must bind to the exact repository and laboratory environment."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.deployment.policy).Statement[4].Condition.StringEquals["iam:PassedToService"] == "ec2.amazonaws.com"
    error_message = "Only EC2 may receive the lab instance role."
  }
}
run "reject_wildcard_repository" {
  command = plan
  variables { github_subject = "repo:example/*:environment:laboratory" }
  expect_failures = [var.github_subject]
}
