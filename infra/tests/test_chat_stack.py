import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template

from infrapilot_infra.chat_stack import ChatStack


def synth(dist):
    app = cdk.App()
    stack = ChatStack(app, "Test", frontend_dist=dist, env=cdk.Environment(account="123456789012",
                                                                          region="us-east-1"))
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
    t.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "handler.lambda_handler",
        "Runtime": "python3.12",
        "Environment": {"Variables": {"TABLE_NAME": Match.any_value()}},
    })


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
