output "aws_account_id" {
  description = "AWS account selected by the active credentials."
  value       = data.aws_caller_identity.current.account_id
}

output "bucket_name" {
  description = "Name of the test bucket created by this configuration."
  value       = aws_s3_bucket.connection_test.id
}

output "bucket_arn" {
  description = "ARN of the test bucket."
  value       = aws_s3_bucket.connection_test.arn
}