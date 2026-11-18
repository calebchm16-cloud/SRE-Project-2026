variable "allowed_account_id" {
  type        = string
  sensitive   = true
  description = "Only this dedicated AWS laboratory account may be used. Supply privately."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.allowed_account_id))
    error_message = "Supply the twelve-digit laboratory AWS account ID."
  }
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
    error_message = "Use a lowercase project slug of 3–26 characters."
  }
}

variable "architecture" {
  type    = string
  default = "arm64"
  validation {
    condition     = contains(["arm64", "amd64"], var.architecture)
    error_message = "Choose arm64 or amd64."
  }
}

variable "web_cidrs" {
  type        = set(string)
  default     = ["0.0.0.0/0"]
  description = "Readers allowed to reach the read-only HTTP forecast page."
  validation {
    condition     = length(var.web_cidrs) > 0 && alltrue([for c in var.web_cidrs : can(cidrnetmask(c))])
    error_message = "Supply one or more IPv4 CIDRs."
  }
}

variable "disk_gib" {
  type    = number
  default = 30
  validation {
    condition     = var.disk_gib >= 20 && var.disk_gib <= 100
    error_message = "Use 20–100 GiB for this single-node laboratory."
  }
}
