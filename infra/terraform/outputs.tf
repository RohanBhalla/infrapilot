output "site_url" {
  description = "CloudFront URL for the frontend."
  value       = local.site_url
}

output "cognito_user_pool_id" {
  description = "Cognito user pool ID."
  value       = aws_cognito_user_pool.users.id
}

output "cognito_client_id" {
  description = "Public browser app client ID."
  value       = aws_cognito_user_pool_client.web.id
}

output "cognito_domain" {
  description = "Cognito hosted UI domain."
  value       = "https://${aws_cognito_user_pool_domain.users.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "site_bucket_name" {
  description = "Private S3 bucket containing the frontend build."
  value       = aws_s3_bucket.site.id
}
