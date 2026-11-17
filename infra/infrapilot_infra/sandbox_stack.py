"""Limits for sandbox deploys (plan C13, decision 004): what an `ip-sbx-*` stack may create.

- **Service role:** CloudFormation runs every sandbox stack as this role (`--role-arn`). It can
  create only the resource types in `infrapilot_assistant.sandbox.RESOURCE_TYPES`, with `ip-sbx-*`
  names where the service has names, and it can't touch anything tagged `lifecycle=persistent`
  (all of our own stacks).
- **Permissions boundary:** every IAM role a sandbox creates must carry it; the service role can't
  create or change a role otherwise. It caps those roles at writing logs and using `ip-sbx-*`
  buckets, functions and ECR repositories.

Both are free. Nothing here runs on its own; the request workflow (C12) uses them.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack, aws_iam as iam
from constructs import Construct

PREFIX = "ip-sbx-"


class SandboxStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        a, r, p = self.account, self.region, self.partition
        sbx = f"{PREFIX}*"

        self.boundary = iam.ManagedPolicy(
            self, "Boundary",
            description="InfraPilot sandbox: the most any role created by an ip-sbx-* stack can do",
            statements=[
                iam.PolicyStatement(actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[f"arn:{p}:logs:{r}:{a}:log-group:{sbx}"]),
                iam.PolicyStatement(actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                                    resources=[f"arn:{p}:s3:::{sbx}", f"arn:{p}:s3:::{sbx}/*"]),
                iam.PolicyStatement(actions=["lambda:InvokeFunction"],
                                    resources=[f"arn:{p}:lambda:{r}:{a}:function:{sbx}"]),
                iam.PolicyStatement(actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                                             "ecr:BatchCheckLayerAvailability"],
                                    resources=[f"arn:{p}:ecr:{r}:{a}:repository/{sbx}"]),
                iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
            ],
        )
        boundary_set = {"StringEquals": {"iam:PermissionsBoundary": self.boundary.managed_policy_arn}}
        roles = f"arn:{p}:iam::{a}:role/{sbx}"

        self.service_role = iam.Role(
            self, "CloudFormationRole",
            description="InfraPilot sandbox: CloudFormation creates and deletes ip-sbx-* stacks as this role",
            assumed_by=iam.ServicePrincipal("cloudformation.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": a}}),
        )
        statements = [
            # Roles: only ip-sbx-* ones, and only while they carry the boundary.
            iam.PolicyStatement(
                actions=["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
                         "iam:DeleteRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary"],
                resources=[roles], conditions=boundary_set),
            # Tagging requests don't include the boundary, so they can't be conditioned on it.
            iam.PolicyStatement(
                actions=["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
                         "iam:ListRoleTags", "iam:TagRole", "iam:UntagRole", "iam:DeleteRole"],
                resources=[roles]),
            iam.PolicyStatement(
                actions=["iam:PassRole"], resources=[roles],
                conditions={"StringEquals": {"iam:PassedToService": [
                    "lambda.amazonaws.com", "scheduler.amazonaws.com", "ecs-tasks.amazonaws.com"]}}),
            # Named resources: ip-sbx-* only.
            iam.PolicyStatement(actions=["s3:*"], resources=[f"arn:{p}:s3:::{sbx}", f"arn:{p}:s3:::{sbx}/*"]),
            iam.PolicyStatement(actions=["lambda:*"], resources=[f"arn:{p}:lambda:{r}:{a}:function:{sbx}"]),
            iam.PolicyStatement(actions=["logs:*"], resources=[f"arn:{p}:logs:{r}:{a}:log-group:{sbx}"]),
            iam.PolicyStatement(actions=["logs:DescribeLogGroups"], resources=["*"]),
            iam.PolicyStatement(actions=["scheduler:*"],
                                resources=[f"arn:{p}:scheduler:{r}:{a}:schedule/default/{sbx}"]),
            iam.PolicyStatement(actions=["ecr:*"], resources=[f"arn:{p}:ecr:{r}:{a}:repository/{sbx}"]),
            # Services without our names (CloudFront, API Gateway): any id, but see the deny below.
            iam.PolicyStatement(
                actions=["cloudfront:*Distribution*", "cloudfront:*OriginAccessControl*",
                         "cloudfront:TagResource", "cloudfront:UntagResource", "cloudfront:ListTagsForResource"],
                resources=[f"arn:{p}:cloudfront::{a}:distribution/*",
                           f"arn:{p}:cloudfront::{a}:origin-access-control/*"]),
            iam.PolicyStatement(
                actions=["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH",
                         "apigateway:DELETE", "apigateway:TagResource", "apigateway:UntagResource"],
                resources=[f"arn:{p}:apigateway:{r}::/apis", f"arn:{p}:apigateway:{r}::/apis/*",
                           f"arn:{p}:apigateway:{r}::/tags/*"]),
            # Never touch our own stacks' resources.
            iam.PolicyStatement(effect=iam.Effect.DENY, actions=["*"], resources=["*"],
                                conditions={"StringEquals": {"aws:ResourceTag/lifecycle": "persistent"}}),
        ]
        policy = iam.ManagedPolicy(self, "CloudFormationPolicy", statements=statements,
                                   description="InfraPilot sandbox: what CloudFormation may do in an ip-sbx-* stack")
        self.service_role.add_managed_policy(policy)

        CfnOutput(self, "BoundaryArn", value=self.boundary.managed_policy_arn)
        CfnOutput(self, "CloudFormationRoleArn", value=self.service_role.role_arn)
