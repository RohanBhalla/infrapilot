import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template

from infrapilot_infra.chat_stack import ChatStack


KB_ARN = "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123"


def synth(dist):
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})  # don't pip-install in unit tests
    stack = ChatStack(app, "Test", frontend_dist=dist, knowledge_base_id="KB123", knowledge_base_arn=KB_ARN,
                      env=cdk.Environment(account="123456789012", region="us-east-1"))
    return stack, Template.from_stack(stack)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    dist = tmp_path_factory.mktemp("dist")
    (dist / "index.html").write_text("<!doctype html>")
    return synth(dist)


def test_session_table_matches_the_lambda(built):
    _, t = built
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        "BillingMode": "PAY_PER_REQUEST",
    })
    t.has_resource_properties("AWS::DynamoDB::Table", {"TimeToLiveSpecification": {
        "AttributeName": "expires_at", "Enabled": True}})
    t.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "infrapilot_api.lambda_handler.lambda_handler",
        "Runtime": "python3.12",
        "Architectures": ["arm64"],
        "Timeout": 29,
        "Environment": {"Variables": Match.object_like({
            "SESSIONS_TABLE": Match.any_value(), "INFRAPILOT_BACKEND": "kb", "INFRAPILOT_KB_ID": "KB123",
            "OPENAI_KEY_PARAM": "/infrapilot/openai-api-key", "INFRAPILOT_RERANK_MODEL": "none",
            "INFRAPILOT_LLM_TIMEOUT": "12", "INFRAPILOT_LLM_RETRIES": "1"})},
    })


def test_lambda_can_read_only_the_key_and_search_only_the_kb(built):
    _, t = built
    statements = [s for p in t.find_resources("AWS::IAM::Policy").values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    by_action = {s["Action"] if isinstance(s["Action"], str) else tuple(s["Action"]): s["Resource"]
                 for s in statements}
    assert by_action["bedrock:Retrieve"] == KB_ARN
    assert "parameter/infrapilot/openai-api-key" in str(by_action["ssm:GetParameter"])
    assert not any("OPENAI_API_KEY" in str(f) for f in t.find_resources("AWS::Lambda::Function").values())


def test_session_routes_need_cognito_and_health_does_not(built):
    _, t = built
    routes = t.find_resources("AWS::ApiGatewayV2::Route")
    auth = {r["Properties"]["RouteKey"]: r["Properties"].get("AuthorizationType", "NONE") for r in routes.values()}
    assert auth == {
        "GET /health": "NONE",
        "POST /sessions": "JWT",
        "GET /sessions/{session_id}": "JWT",
        "POST /sessions/{session_id}/messages": "JWT",
        "POST /requests": "JWT",
        "GET /requests": "JWT",
        "GET /requests/{request_id}": "JWT",
        "POST /requests/{request_id}/approve": "JWT",
        "POST /requests/{request_id}/reject": "JWT",
        "POST /requests/{request_id}/upload": "JWT",
        "POST /requests/{request_id}/upload/check": "JWT",
        "POST /requests/{request_id}/teardown": "JWT",
        "GET /requests/{request_id}/logs": "JWT",
    }


def test_browser_client_has_no_secret_and_uses_code_flow(built):
    _, t = built
    t.has_resource_properties("AWS::Cognito::UserPoolClient", {
        "GenerateSecret": False,
        "AllowedOAuthFlows": ["code"],
    })


def test_site_bucket_is_private_and_served_by_cloudfront(built):
    _, t = built
    t.has_resource_properties("AWS::S3::Bucket", {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}})
    t.has_resource_properties("AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({
        "DefaultRootObject": "index.html"})})
    t.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_api_is_throttled(built):
    _, t = built
    t.has_resource_properties("AWS::ApiGatewayV2::Stage", {"DefaultRouteSettings": {
        "ThrottlingBurstLimit": 20, "ThrottlingRateLimit": 10}})


def test_website_upload_includes_config(built):
    _, t = built
    t.resource_count_is("Custom::CDKBucketDeployment", 1)



def test_config_sets_the_sites_own_sign_in_redirect(tmp_path, monkeypatch):
    """So a localhost redirect from a dev env file baked into the build can't win (2026-11-27)."""
    from aws_cdk import aws_s3_deployment as s3deploy

    seen, real = {}, s3deploy.Source.json_data
    monkeypatch.setattr(s3deploy.Source, "json_data", lambda key, data, *a, **k: seen.update({key: data}) or real(key, data, *a, **k))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    synth(dist)
    config = seen["config.json"]
    assert config["redirectUrl"] == config["signOutUrl"]
    assert config["redirectUrl"].startswith("https://") and config["redirectUrl"].endswith("/")


def test_missing_build_skips_upload_with_a_warning(tmp_path):
    stack, t = synth(tmp_path / "missing")
    t.resource_count_is("Custom::CDKBucketDeployment", 0)
    Annotations.from_stack(stack).has_warning("*", Match.string_like_regexp("npm run build"))


def test_validator_is_an_arm_container_the_chat_lambda_can_call(built):
    _, t = built
    t.has_resource_properties("AWS::Lambda::Function", {
        "PackageType": "Image", "Architectures": ["arm64"], "MemorySize": 2048, "Timeout": 20})
    t.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "infrapilot_api.lambda_handler.lambda_handler",
        "Environment": {"Variables": Match.object_like({
            "INFRAPILOT_VALIDATOR_FUNCTION": Match.any_value(), "ARTIFACTS_BUCKET": Match.any_value()})}})
    statements = [s for p in t.find_resources("AWS::IAM::Policy").values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    assert any("lambda:InvokeFunction" in str(s["Action"]) for s in statements)


def test_artifacts_bucket_is_private_and_expires_plans(built):
    _, t = built
    t.has_resource_properties("AWS::S3::Bucket", {
        "LifecycleConfiguration": {"Rules": [
            Match.object_like({"ExpirationInDays": 30, "Status": "Enabled"}),
            Match.object_like({"ExpirationInDays": 1, "Prefix": "uploads/", "Status": "Enabled"})]},
        # Browsers may only POST (users' site zips, through a presigned link), from our own site.
        "CorsConfiguration": {"CorsRules": [Match.object_like({"AllowedMethods": ["POST"]})]},
        "PublicAccessBlockConfiguration": Match.object_like({"BlockPublicPolicy": True})})
    statements = [s for p in t.find_resources("AWS::IAM::Policy").values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    put = [s for s in statements if "s3:PutObject" in str(s["Action"]) and "Artifacts" in str(s["Resource"])]
    assert put and all(any(f"/{prefix}/*" in str(s["Resource"]) for prefix in ("plans", "requests", "uploads"))
                       for s in put)


def test_every_lambda_has_a_description(built):
    _, t = built
    functions = t.find_resources("AWS::Lambda::Function")
    assert len(functions) == 6  # chat, validator, sandbox ops, janitor, auto-delete helper, website upload helper
    assert all(f["Properties"].get("Description", "").startswith("InfraPilot") for f in functions.values())


def test_requests_table_has_a_history_index_and_the_lambda_knows_the_boundary(built):
    _, t = built
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [Match.object_like({"IndexName": "by_user", "KeySchema": [
            {"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}]})],
    })
    t.has_resource_properties("AWS::Lambda::Function", {"Environment": {"Variables": Match.object_like({
        "REQUESTS_TABLE": Match.any_value(),
        "SANDBOX_BOUNDARY_ARN": {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"},
                                                   ":iam::123456789012:policy/InfraPilotSandboxBoundary"]]}})}})


def test_sandbox_workflows_create_stacks_only_as_the_sandbox_role(built):
    import json

    _, t = built
    machines = t.find_resources("AWS::StepFunctions::StateMachine")
    assert len(machines) == 2
    definitions = [json.dumps(m["Properties"]["DefinitionString"]) for m in machines.values()]
    create = next(d for d in definitions if "createStack" in d)
    for expected in ("OnFailure", "DELETE", "CAPABILITY_IAM", "role/InfraPilotSandboxCloudFormation", "ephemeral"):
        assert expected in create
    assert any("deleteStack" in d for d in definitions)

    statements = [s for p in t.find_resources("AWS::IAM::Policy").values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    passes = [s for s in statements if "iam:PassRole" in str(s["Action"])]
    assert len(passes) == 3 and all("role/InfraPilotSandboxCloudFormation" in str(s["Resource"]) for s in passes)
    stacks = [s for s in statements if "cloudformation:" in str(s["Action"]) and s["Action"] != "cloudformation:ListStacks"]
    assert stacks and all("stack/ip-sbx-*/*" in str(s["Resource"]) for s in stacks)
    t.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "infrapilot_api.sandbox_handler.handler",
        "Environment": {"Variables": Match.object_like({"REQUESTS_TABLE": Match.any_value()})}})


def test_janitor_runs_every_hour_and_can_only_start_teardowns(built):
    _, t = built
    t.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "rate(1 hour)", "State": "ENABLED"})
    t.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "infrapilot_api.janitor.handler",
        "Environment": {"Variables": Match.object_like({"TEARDOWN_STATE_MACHINE": Match.any_value(),
                                                         "SANDBOX_ROLE_ARN": Match.any_value()})}})
    statements = [s for p in t.find_resources("AWS::IAM::Policy").values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    starts = [s for s in statements if s["Action"] == "states:StartExecution"]
    assert starts and all("Deploy" in str(s["Resource"]) or "Teardown" in str(s["Resource"]) for s in starts)


def test_validator_is_kept_warm_every_five_minutes(built):
    _, t = built
    t.has_resource_properties("AWS::Events::Rule", {
        "ScheduleExpression": "rate(5 minutes)",
        "Targets": [Match.object_like({"Input": '{"warm":true}'})]})
