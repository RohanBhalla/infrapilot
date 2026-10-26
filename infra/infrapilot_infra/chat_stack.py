"""The chat app on AWS (plan C8, C15): website, login, chat API and session table.

One stack, so `cdk deploy` creates all of it and `cdk destroy` removes all of it. The Lambda is
still the test stub in aws/lambda_chat (fixed reply, no RAG).
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

REPO = Path(__file__).resolve().parents[2]
LOCAL_DEV = "http://localhost:5173"  # Vite dev server; Cognito allows plain http only for localhost


class ChatStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, frontend_dist: Path | None = None,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        dist = frontend_dist or REPO / "frontend" / "dist"

        # Website: a private bucket that only CloudFront can read.
        site_bucket = s3.Bucket(
            self, "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        site = cloudfront.Distribution(
            self, "Site",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,  # US/Europe edges only: cheapest
        )
        site_url = f"https://{site.distribution_domain_name}"
        app_urls = [site_url, f"{site_url}/", LOCAL_DEV, f"{LOCAL_DEV}/"]

        # Login: Cognito hosted sign-in. Users are added by the team, not self-registered.
        users = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        domain = users.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(
            domain_prefix=f"infrapilot-{self.account}"))  # must be unique across AWS
        client = users.add_client(
            "WebClient",
            generate_secret=False,  # a browser app can't keep a secret
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=app_urls,
                logout_urls=app_urls,
            ),
            prevent_user_existence_errors=True,
        )

        # Sessions and messages, one partition per Cognito user (see aws/lambda_chat/handler.py).
        sessions = dynamodb.Table(
            self, "Sessions",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        chat_fn = lambda_.Function(
            self, "ChatFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(REPO / "aws" / "lambda_chat"),
                                         exclude=["test_*", "README.md", "__pycache__"]),
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={"TABLE_NAME": sessions.table_name},
            log_group=logs.LogGroup(self, "ChatLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=RemovalPolicy.DESTROY),
        )
        sessions.grant(chat_fn, "dynamodb:PutItem", "dynamodb:Query")

        # API: Cognito checks the token before the Lambda runs; /health stays open.
        api = apigw.HttpApi(
            self, "Api",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[site_url, LOCAL_DEV],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST,
                               apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["Authorization", "Content-Type"],
                max_age=Duration.hours(1),
            ),
        )
        # Throttle the whole API so a runaway client can't run up Lambda and DynamoDB costs.
        stage = api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=20, throttling_rate_limit=10)

        integration = integrations.HttpLambdaIntegration("Chat", chat_fn)
        cognito_auth = authorizers.HttpUserPoolAuthorizer("Cognito", users, user_pool_clients=[client])
        api.add_routes(path="/health", methods=[apigw.HttpMethod.GET], integration=integration)
        for path, method in [("/sessions", apigw.HttpMethod.POST),
                             ("/sessions/{session_id}", apigw.HttpMethod.GET),
                             ("/sessions/{session_id}/messages", apigw.HttpMethod.POST)]:
            api.add_routes(path=path, methods=[method], integration=integration, authorizer=cognito_auth)

        # The frontend reads these at startup, so nobody copies IDs into a .env file to deploy.
        config = {
            "region": self.region,
            "userPoolId": users.user_pool_id,
            "clientId": client.user_pool_client_id,
            "domain": domain.base_url(),
            "apiUrl": api.api_endpoint,
        }
        if dist.is_dir():
            s3deploy.BucketDeployment(
                self, "DeploySite",
                destination_bucket=site_bucket,
                sources=[s3deploy.Source.asset(str(dist)), s3deploy.Source.json_data("config.json", config)],
                distribution=site,
                distribution_paths=["/*"],
            )
        else:
            Annotations.of(self).add_warning(
                f"{dist} not found, so the website files won't be uploaded. "
                "Run `npm run build` in frontend/ first.")

        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=users.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=domain.base_url())
        CfnOutput(self, "SessionsTable", value=sessions.table_name)
