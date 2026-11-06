variable "aws_region" {
  description = "AWS region in which to create the test bucket."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name used only for this connection test."
  type        = string

  validation {
    condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63 && can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be 3-63 characters, use lowercase letters, numbers, dots, or hyphens, and start and end with a letter or number."
  }
}