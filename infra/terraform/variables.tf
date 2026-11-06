variable "aws_region" {
  description = "AWS region for the chat application."
  type        = string
  default     = "us-east-1"
}

variable "owner" {
  description = "Owner tag applied to all supported resources."
  type        = string
  default     = "pair-b"
}

variable "resource_suffix" {
  description = "Suffix for named resources so this Terraform deployment can coexist with CDK."
  type        = string
  default     = "tf"
}

variable "cognito_domain_prefix" {
  description = "Globally unique Cognito hosted UI domain prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.cognito_domain_prefix))
    error_message = "The Cognito domain prefix must be 1-63 lowercase letters, digits, or hyphens, and cannot start or end with a hyphen."
  }
}

variable "additional_callback_urls" {
  description = "Additional Cognito callback and sign-out URLs, such as a staging site."
  type        = list(string)
  default     = []
}

variable "frontend_dist_path" {
  description = "Frontend build directory, relative to this Terraform folder."
  type        = string
  default     = "../../frontend/dist"
}

variable "force_destroy_site_bucket" {
  description = "Allow Terraform to delete the site bucket when it still contains objects."
  type        = bool
  default     = false
}