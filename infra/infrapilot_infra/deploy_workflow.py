"""Sandbox deploy and teardown workflows (plan C12, C13; decisions 004 and 005).

Approving a request starts `Deploy`; the Tear down button starts `Teardown`. Both are Step
Functions Standard workflows that call CloudFormation directly (no CodeBuild) and use the sandbox
ops Lambda (api/infrapilot_api/sandbox_handler.py) for status checks and to record outcomes:

    Deploy:   CreateStack (sandbox role, OnFailure=DELETE) → Started → [Wait 15 s → Status]* →
              CREATE_COMPLETE: Deployed (upload sample page, record outputs) | *_IN_PROGRESS: wait | else: Failed
    Teardown: Empty buckets → DeleteStack → [Wait 15 s → Status]* → GONE: TornDown | else: TeardownFailed
    Janitor:  every hour, tear down sandboxes past their 4 hours (api/infrapilot_api/janitor.py)

Cost: a deploy takes ~15–40 state transitions; 4,000 a month are free, then $0.025 per 1,000.
The janitor is ~730 Lambda runs a month and an EventBridge rule, both inside the free tier.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack, aws_events as events, aws_events_targets as targets
from aws_cdk import aws_iam as iam, aws_lambda as lambda_, aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn, aws_stepfunctions_tasks as tasks
from constructs import Construct

from .sandbox_stack import PREFIX, service_role_arn

POLL = Duration.seconds(15)


class DeployWorkflow(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, code: lambda_.Code, requests, artifacts) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        a, r, p = stack.account, stack.region, stack.partition
        stacks_arn = f"arn:{p}:cloudformation:{r}:{a}:stack/{PREFIX}*/*"
        role_arn = service_role_arn(stack)

        self.ops = lambda_.Function(
            self, "OpsFunction",
            description="InfraPilot sandbox ops: checks ip-sbx-* stack status, uploads the sample page or the "
                        "user's checked site, empties "
                        "sandbox buckets before delete and records each deploy's outcome.",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="infrapilot_api.sandbox_handler.handler",
            code=code,
            memory_size=512,  # a user's site: up to 20 MB zipped, 50 MB unzipped (decision 006)
            timeout=Duration.minutes(2),
            environment={"REQUESTS_TABLE": requests.table_name, "ARTIFACTS_BUCKET": artifacts.bucket_name,
                         "POWERTOOLS_SERVICE_NAME": "infrapilot-sandbox"},
            log_group=logs.LogGroup(self, "OpsLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
        )
        requests.grant(self.ops, "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")
        artifacts.grant_read(self.ops, "requests/*")
        artifacts.grant_read(self.ops, "uploads/*")  # users' site zips
        self.ops.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
                     "cloudformation:ListStackResources"], resources=[stacks_arn]))
        self.ops.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ListBucket",
                     "s3:ListBucketVersions"],
            resources=[f"arn:{p}:s3:::{PREFIX}*", f"arn:{p}:s3:::{PREFIX}*/*"]))

        def op(name: str, action: str, payload: dict, result_path: str | None = sfn.JsonPath.DISCARD):
            return tasks.LambdaInvoke(
                self, name, lambda_function=self.ops, payload_response_only=True, result_path=result_path,
                payload=sfn.TaskInput.from_object({"action": action, **payload}),
                retry_on_service_exceptions=True)

        request_id = sfn.JsonPath.string_at("$.request_id")
        error = sfn.JsonPath.string_at("$.error.Cause")

        # Deploy ------------------------------------------------------------------------------
        failed_before_stack = op("FailedToCreate", "failed", {"request_id": request_id, "error": error})
        failed = op("Failed", "failed", {"request_id": request_id, "stack": sfn.JsonPath.string_at("$.stack_id"),
                                         "error": "The deploy failed."})
        failed_caught = op("FailedWithError", "failed", {
            "request_id": request_id, "stack": sfn.JsonPath.string_at("$.stack_id"), "error": error})
        deploy_failed = sfn.Fail(self, "DeployFailed")
        for f in (failed_before_stack, failed, failed_caught):
            f.next(deploy_failed)

        create = tasks.CallAwsService(
            self, "CreateStack", service="cloudformation", action="createStack",
            iam_resources=[stacks_arn],
            parameters={
                "StackName": sfn.JsonPath.string_at("$.stack_name"),
                "TemplateBody": sfn.JsonPath.string_at("$.template_body"),
                "Parameters": sfn.JsonPath.list_at("$.parameters"),
                "RoleARN": role_arn,
                "OnFailure": "DELETE",
                "Capabilities": ["CAPABILITY_IAM"],
                "ClientRequestToken": sfn.JsonPath.format("ip-{}", request_id),
                "Tags": [{"Key": "lifecycle", "Value": "ephemeral"}, {"Key": "project", "Value": "infrapilot"},
                         {"Key": "infrapilot-request", "Value": request_id}],
            },
            result_selector={"stack_id": sfn.JsonPath.string_at("$.StackId")},
            result_path="$.created",
        )
        create.add_catch(failed_before_stack, result_path="$.error")
        # Keep only what later steps need (drops the template from the state).
        keep = sfn.Pass(self, "KeepIds", parameters={
            "request_id": request_id, "stack_id": sfn.JsonPath.string_at("$.created.stack_id")})
        started = op("Started", "started", {"request_id": request_id,
                                            "stack_id": sfn.JsonPath.string_at("$.stack_id")})
        wait = sfn.Wait(self, "WaitForCreate", time=sfn.WaitTime.duration(POLL))
        check = op("CreateStatus", "status", {"stack": sfn.JsonPath.string_at("$.stack_id")}, "$.check")
        deployed = op("Deployed", "deployed", {"request_id": request_id,
                                               "stack": sfn.JsonPath.string_at("$.stack_id")})
        for step in (started, check, deployed):
            step.add_catch(failed_caught, result_path="$.error")

        status = "$.check.status"
        deploy = (create.next(keep).next(started).next(wait).next(check).next(
            sfn.Choice(self, "Created?")
            .when(sfn.Condition.string_equals(status, "CREATE_COMPLETE"), deployed.next(sfn.Succeed(self, "Live")))
            # Also waits while a failed stack is deleted, so Failed can tell the stack is gone.
            .when(sfn.Condition.string_matches(status, "*_IN_PROGRESS"), wait)
            .otherwise(failed)))

        # Teardown ----------------------------------------------------------------------------
        teardown_failed = op("TeardownFailed", "teardown_failed", {"request_id": request_id, "error": error})
        teardown_failed_status = op("DeleteFailed", "teardown_failed", {"request_id": request_id})
        not_deleted = sfn.Fail(self, "NotDeleted")
        teardown_failed.next(not_deleted)
        teardown_failed_status.next(not_deleted)

        stack_ref = sfn.JsonPath.string_at("$.stack")
        empty = op("EmptyBuckets", "empty", {"stack": stack_ref})
        delete = tasks.CallAwsService(
            self, "DeleteStack", service="cloudformation", action="deleteStack", iam_resources=[stacks_arn],
            parameters={"StackName": stack_ref, "RoleARN": role_arn}, result_path=sfn.JsonPath.DISCARD)
        wait_delete = sfn.Wait(self, "WaitForDelete", time=sfn.WaitTime.duration(POLL))
        check_delete = op("DeleteStatus", "status", {"stack": stack_ref}, "$.check")
        torn_down = op("TornDown", "torn_down", {"request_id": request_id})
        for step in (empty, delete, check_delete, torn_down):
            step.add_catch(teardown_failed, result_path="$.error")

        teardown = (empty.next(delete).next(wait_delete).next(check_delete).next(
            sfn.Choice(self, "Deleted?")
            .when(sfn.Condition.string_equals(status, "GONE"), torn_down.next(sfn.Succeed(self, "Gone")))
            .when(sfn.Condition.string_equals(status, "DELETE_FAILED"), teardown_failed_status)
            .otherwise(wait_delete)))

        def machine(name: str, definition) -> sfn.StateMachine:
            m = sfn.StateMachine(
                self, name, definition_body=sfn.DefinitionBody.from_chainable(definition),
                timeout=Duration.hours(1),
                logs=sfn.LogOptions(destination=logs.LogGroup(
                    self, f"{name}Logs", retention=logs.RetentionDays.TWO_WEEKS,
                    removal_policy=RemovalPolicy.DESTROY), level=sfn.LogLevel.ERROR))
            # CloudFormation needs to be handed the sandbox role; nothing else may be passed.
            m.add_to_role_policy(iam.PolicyStatement(
                actions=["iam:PassRole"], resources=[role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}))
            m.add_to_role_policy(iam.PolicyStatement(actions=["cloudformation:TagResource"],
                                                     resources=[stacks_arn]))
            return m

        self.deploy = machine("Deploy", deploy)
        self.teardown = machine("Teardown", teardown)

        # Janitor -----------------------------------------------------------------------------
        self.janitor = lambda_.Function(
            self, "JanitorFunction",
            description="InfraPilot sandbox janitor: every hour, tears down ip-sbx-* sandboxes past their 4 hours, "
                        "deletes stray ones and releases stuck one-sandbox locks.",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="infrapilot_api.janitor.handler",
            code=code,
            memory_size=256,
            timeout=Duration.minutes(5),
            environment={"REQUESTS_TABLE": requests.table_name, "TEARDOWN_STATE_MACHINE": self.teardown.state_machine_arn,
                         "SANDBOX_ROLE_ARN": role_arn, "POWERTOOLS_SERVICE_NAME": "infrapilot-janitor"},
            log_group=logs.LogGroup(self, "JanitorLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
        )
        requests.grant(self.janitor, "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan")
        self.teardown.grant_start_execution(self.janitor)
        self.janitor.add_to_role_policy(iam.PolicyStatement(  # ListStacks can't be limited to some stacks
            actions=["cloudformation:ListStacks"], resources=["*"]))
        self.janitor.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudformation:ListStackResources", "cloudformation:DeleteStack"], resources=[stacks_arn]))
        self.janitor.add_to_role_policy(iam.PolicyStatement(  # stray stacks are deleted as the sandbox role
            actions=["iam:PassRole"], resources=[role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}))
        self.janitor.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ListBucket", "s3:ListBucketVersions"],
            resources=[f"arn:{p}:s3:::{PREFIX}*", f"arn:{p}:s3:::{PREFIX}*/*"]))
        events.Rule(self, "Hourly", description="InfraPilot: run the sandbox janitor every hour",
                    schedule=events.Schedule.rate(Duration.hours(1)),
                    targets=[targets.LambdaFunction(self.janitor, retry_attempts=0)])
