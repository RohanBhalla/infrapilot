terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project   = "infrapilot"
      owner     = var.owner
      lifecycle = "persistent"
    }
  }
}

locals {
  frontend_dist = abspath("${path.module}/${var.frontend_dist_path}")
  site_url      = "https://${aws_cloudfront_distribution.site.domain_name}"
  content_types = {
    css   = "text/css"
    html  = "text/html"
    ico   = "image/x-icon"
    jpg   = "image/jpeg"
    jpeg  = "image/jpeg"
    js    = "application/javascript"
    json  = "application/json"
    png   = "image/png"
    svg   = "image/svg+xml"
    txt   = "text/plain"
    webp  = "image/webp"
    woff  = "font/woff"
    woff2 = "font/woff2"
  }
}