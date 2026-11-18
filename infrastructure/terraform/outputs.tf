output "public_ip" {
  description = "Auto-assigned IPv4, refreshed after stop/start; no Elastic IP exists."
  value       = aws_instance.lab.public_ip
}

output "application_url" {
  value = try(length(aws_instance.lab.public_ip), 0) > 0 ? "http://${aws_instance.lab.public_ip}" : null
}

output "api_docs_url" {
  value = try(length(aws_instance.lab.public_ip), 0) > 0 ? "http://${aws_instance.lab.public_ip}/docs" : null
}

output "instance_id" {
  value = aws_instance.lab.id
}

output "release_bucket" {
  value = aws_s3_bucket.releases.id
}

output "architecture" {
  value = startswith(aws_instance.lab.instance_type, "t4g.") ? "arm64" : "amd64"
}
