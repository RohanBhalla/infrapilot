# InfraPilot frontend Terraform deployment

This standalone Terraform configuration deploys the static frontend to a private S3 bucket behind
CloudFront and provisions Cognito sign-in. It deliberately does not deploy or connect a chat API,
Lambda, DynamoDB, or Bedrock. Chat functionality remains unavailable until a backend is added.

## Prerequisites

- Terraform 1.10 or newer and AWS credentials for the target account.
- AWS provider access to create S3, CloudFront, and Cognito resources.
- Node.js/npm to build the frontend.

## AWS Access and GitHub Actions

The workflow at `.github/workflows/deploy-terraform.yml` uses GitHub OIDC, not long-lived AWS
access keys. An AWS administrator must create an IAM OIDC provider for
`https://token.actions.githubusercontent.com` and a deployment role trusted only for this GitHub
repository's `main` branch. The trust policy should restrict the audience to `sts.amazonaws.com`
and the subject to `repo:RohanBhalla/infrapilot:ref:refs/heads/main`. Give that role the permissions
needed to manage this Terraform root and access the state bucket; avoid broad account-admin access.
An example trust policy (replace the account ID if creating this in another account) is:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::YOUR_ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:RohanBhalla/infrapilot:ref:refs/heads/main"
      }
    }
  }]
}
```

Create a dedicated, versioned, private S3 bucket for Terraform state before the first workflow run.
For example, after authenticating the AWS CLI and choosing a globally unique name:

```bash
aws s3api create-bucket --bucket YOUR_UNIQUE_TF_STATE_BUCKET --region us-east-1
aws s3api put-public-access-block --bucket YOUR_UNIQUE_TF_STATE_BUCKET \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket YOUR_UNIQUE_TF_STATE_BUCKET \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket YOUR_UNIQUE_TF_STATE_BUCKET \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

In GitHub repository **Settings → Secrets and variables → Actions**, add the secret
`AWS_DEPLOY_ROLE_ARN` and these variables: `TF_STATE_BUCKET`, `COGNITO_DOMAIN_PREFIX`, and
optionally `AWS_REGION` (defaults to `us-east-1`). The deployment role needs access to the state
bucket, including the `.tflock` object, and permission to manage the static-site and Cognito
resources in this Terraform root.

To deploy, open **Actions → Deploy InfraPilot with Terraform → Run workflow** on `main`. The
workflow builds the frontend, validates, and plans. Leave **Apply the Terraform plan** unchecked to
plan only; check it to apply the generated plan after inspecting the plan output.

## Build and deploy

```bash
cd frontend
npm ci
npm run build
cd ../infra/terraform
terraform init \
  -backend-config="bucket=YOUR_UNIQUE_TF_STATE_BUCKET" \
  -backend-config="key=infrapilot/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
terraform fmt -recursive
terraform validate
terraform plan \
  -var='cognito_domain_prefix=YOUR_GLOBALLY_UNIQUE_PREFIX'
terraform apply \
  -var='cognito_domain_prefix=YOUR_GLOBALLY_UNIQUE_PREFIX'
```

If you already have local Terraform state, add `-migrate-state` to `terraform init` when configuring
the backend and confirm the migration prompt. The GitHub role needs `s3:ListBucket` on the state
bucket, `s3:GetObject` and `s3:PutObject` on the state file, and `s3:GetObject`, `s3:PutObject`, and
`s3:DeleteObject` on its `.tflock` object.

The frontend is uploaded from `frontend/dist`; Terraform also writes `/config.json` with the
Cognito settings and no API URL. `terraform output` prints the site URL and Cognito IDs. Create
user accounts through Cognito's admin flow; public self-registration is disabled.
Use a `.tfvars` file or environment variables for repeated inputs rather than copying the example
values. Terraform state contains infrastructure metadata and should be stored in a secured,
shared backend before team use.

To remove the deployment, run `terraform destroy` with the same variables. S3 bucket deletion is
blocked if it is not empty by default; set `force_destroy_site_bucket=true` only when intentionally
deleting the site bucket and its contents.

## Notes

- This folder has its own Terraform state and does not import, modify, or destroy the CDK stacks.
- `additional_callback_urls` can be set for approved staging or custom-domain URLs.