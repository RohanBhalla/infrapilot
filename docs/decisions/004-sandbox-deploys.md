# 004: How sandbox deploys work

- **Status:** accepted
- **Date:** 2026-11-10

## Decision

1. **Account:** an approved plan is deployed into the **team account** as a temporary demo
   sandbox, not into the user's own account. Each sandbox is one CloudFormation stack named
   `ip-sbx-<request id>` (10 lowercase letters and digits), tagged `lifecycle=ephemeral`.
2. **Code:** sandboxes run the placeholder code and sample pages our generators already make
   (a sample `index.html` for static sites, the inline handler for Lambda), not the user's code.
3. **Who:** any signed-in user can deploy static sites and Lambda functions. ECS and EKS will
   be team-only when they are added.
4. **Limits:** one active sandbox per user; every sandbox is deleted after **4 hours**.
5. **No CodeBuild for CloudFormation targets.** The generators already output CloudFormation, so
   there is nothing to build or synthesize. Step Functions calls CloudFormation directly
   (`CreateStack`, then polls `DescribeStacks`), and a small Lambda uploads the sample page and
   empties buckets before teardown. The live log in the chat is the stack's events ("SiteBucket:
   CREATE_COMPLETE"). CodeBuild is added only for the ECS target, which has to build an image.
6. **Two layers of limits:**
   - CloudFormation runs sandbox stacks with a **service role** (`--role-arn`) that may only
     create the resource types our generators use, only with `ip-sbx-*` names where the service
     has names, and never touch resources tagged `lifecycle=persistent` (all of our own stacks).
   - Every IAM role a sandbox creates must carry the **sandbox permissions boundary**, which caps
     it at logs, `ip-sbx-*` buckets, `ip-sbx-*` functions and `ip-sbx-*` ECR repositories. The
     service role can't create a role without it.
7. **Templates are prepared, not edited by hand.** `infrapilot_assistant.sandbox.prepare` turns a
   validated plan into a sandbox bundle: the template as JSON with the boundary on every role, the
   Lambda upload bucket renamed into the `ip-sbx-` prefix, and a check that every resource type is
   on the allowlist. Plans that need something we can't supply (a custom domain's certificate, a
   workflow-only plan) are refused with a reason.

## Why

- Users can't send their own templates: a sandbox only ever deploys our generators' output from
  a plan that passed validation. The limits above are a second line of defence, not the only one.
- Step Functions + CloudFormation costs almost nothing per deploy (a few hundred state
  transitions, inside the free tier), while CodeBuild bills every minute it waits for CloudFront.
- Deploying into users' own accounts would need cross-account roles and onboarding; out of scope
  for the course.

## Cost

Static site and Lambda sandboxes: about $0 for 4 hours. Step Functions Standard: 4,000 free
transitions a month, then $0.025 per 1,000. The service role, boundary and table are free.
