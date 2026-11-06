resource "aws_s3_bucket" "site" {
  force_destroy = var.force_destroy_site_bucket
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${aws_s3_bucket.site.id}-oac"
  description                       = "Private access to the InfraPilot frontend bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "site-s3-origin"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "site-s3-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  ordered_cache_behavior {
    path_pattern           = "/config.json"
    target_origin_id       = "site-s3-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

data "aws_iam_policy_document" "site_read" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.site.arn, "${aws_s3_bucket.site.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid     = "AllowCloudFrontRead"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.site.arn}/*"
    ]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.site_read.json
}

resource "aws_cognito_user_pool" "users" {
  name                     = "infrapilot-users-${var.resource_suffix}"
  alias_attributes         = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "users" {
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.users.id
}

resource "aws_cognito_user_pool_client" "web" {
  name                                 = "infrapilot-web-${var.resource_suffix}"
  user_pool_id                         = aws_cognito_user_pool.users.id
  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls = concat(
    [local.site_url, "${local.site_url}/", "http://localhost:5173", "http://localhost:5173/"],
    var.additional_callback_urls,
  )
  logout_urls = concat(
    [local.site_url, "${local.site_url}/", "http://localhost:5173", "http://localhost:5173/"],
    var.additional_callback_urls,
  )
}

resource "aws_s3_object" "frontend" {
  for_each = fileset(local.frontend_dist, "**")

  bucket        = aws_s3_bucket.site.id
  key           = each.value
  source        = "${local.frontend_dist}/${each.value}"
  source_hash   = filemd5("${local.frontend_dist}/${each.value}")
  content_type  = lookup(local.content_types, try(element(reverse(split(".", each.value)), 0), ""), "application/octet-stream")
  cache_control = each.value == "index.html" ? "no-cache" : "public, max-age=31536000, immutable"
}

resource "aws_s3_object" "frontend_config" {
  bucket = aws_s3_bucket.site.id
  key    = "config.json"
  content = jsonencode({
    region      = var.aws_region
    userPoolId  = aws_cognito_user_pool.users.id
    clientId    = aws_cognito_user_pool_client.web.id
    domain      = "https://${aws_cognito_user_pool_domain.users.domain}.auth.${var.aws_region}.amazoncognito.com"
    redirectUrl = local.site_url
    signOutUrl  = local.site_url
  })
  content_type  = "application/json"
  cache_control = "no-store"
}