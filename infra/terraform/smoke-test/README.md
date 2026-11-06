# Terraform AWS connection smoke test

This separate Terraform root creates one private, encrypted S3 bucket. It has its own state and
does not touch the InfraPilot application resources. The bucket name must be globally unique.

## 1. Configure and verify AWS credentials

Use the authentication method for your AWS account. With the current AWS CLI, browser login works
for AWS CLI commands:

```bash
aws login
aws sts get-caller-identity --region us-east-1
```

Terraform may not automatically read the browser-login cache. Export the active CLI credentials
into this shell before running Terraform:

```bash
eval "$(aws configure export-credentials --format env)"
```

For AWS IAM Identity Center (SSO), configure and log into a named profile, then export credentials
from that profile:

```bash
aws configure sso --profile infrapilot
aws sso login --profile infrapilot
eval "$(aws configure export-credentials --profile infrapilot --format env)"
```

Run the export in the same terminal session as Terraform. Check the returned account ID before
creating anything. The credentials need permission to create an S3 bucket and configure its
public-access blocking and encryption. Deletion is protected in Terraform unless you intentionally
remove the guard described below.

## 2. Plan and create the test bucket

Install Terraform 1.6 or newer, then run these commands from this directory. Replace the example
name with a globally unique lowercase name:

```bash
terraform init
terraform fmt -check
terraform validate
terraform plan -var='bucket_name=yourname-infrapilot-connection-test-20261103'
terraform apply -var='bucket_name=yourname-infrapilot-connection-test-20261103'
```

Review the plan before typing `yes` at the apply prompt. A successful apply confirms Terraform can
authenticate and your account can create and configure the bucket. The account ID and bucket ARN
are available with `terraform output`.

## 3. Bucket deletion protection

The bucket has `prevent_destroy = true`, so `terraform destroy` from this directory will stop with
an error instead of deleting it. This protects the bucket if it is later reused for application
Terraform state. Do not remove the protection while the bucket contains any Terraform state.

If you created this bucket only for a temporary test and are certain nothing else uses it, remove
the `prevent_destroy` lifecycle block from `main.tf`, review `terraform plan -destroy`, and only then
run `terraform destroy` with the same bucket name. The bucket must be empty because
`force_destroy` is disabled.

Keep this directory's local state file until cleanup is complete; it records the smoke-test bucket.