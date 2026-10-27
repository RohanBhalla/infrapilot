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


def test_missing_build_skips_upload_with_a_warning(tmp_path):
    stack, t = synth(tmp_path / "missing")
    t.resource_count_is("Custom::CDKBucketDeployment", 0)
    Annotations.from_stack(stack).has_warning("*", Match.string_like_regexp("npm run build"))
