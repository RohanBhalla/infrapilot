"""Chat API on AWS Lambda behind API Gateway (HTTP API, payload v2) with a Cognito JWT authorizer.

Powertools routes the request; the logic is the same ChatService the local FastAPI app uses.
API Gateway checks the token and adds the CORS headers, so neither happens here.

Environment: SESSIONS_TABLE, OPENAI_KEY_PARAM (SSM SecureString name), INFRAPILOT_BACKEND=kb,
INFRAPILOT_KB_ID, INFRAPILOT_DATA_DIR (bundled versions.json), ARTIFACTS_BUCKET (saved plans),
INFRAPILOT_VALIDATOR_FUNCTION (the Lambda that runs the linters on generated files),
REQUESTS_TABLE (deploy requests), SANDBOX_BOUNDARY_ARN (InfraPilotSandbox's permissions boundary),
DEPLOY_STATE_MACHINE and TEARDOWN_STATE_MACHINE (the sandbox workflows).
"""

from __future__ import annotations

import json
import os

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers
from aws_lambda_powertools.utilities import parameters
from pydantic import ValidationError

from .artifacts import S3PlanStore, S3SiteUploads
from .aws_deployer import StepFunctionsDeployer
from .deploy_requests import DynamoRequestStore
from .deploys import DeployRequestIn, DeployService, RejectIn
from .dynamo import DynamoSessionStore
from .schemas import MessageIn
from .service import ChatError, ChatService, Unauthorized, Unavailable, build_answers, build_assistant

logger = Logger(service="infrapilot-chat")
copy_config_to_registered_loggers(source_logger=logger)  # JSON logs from rag/assistant too
app = APIGatewayHttpResolver()


def _answers():
    # Read the key once per Lambda instance; it stays in memory, never in an environment
    # variable that the console would show.
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = parameters.get_parameter(os.environ["OPENAI_KEY_PARAM"], decrypt=True)
    return build_answers()


bucket = os.environ.get("ARTIFACTS_BUCKET")
plans = S3PlanStore(bucket) if bucket else None
chat = ChatService(DynamoSessionStore(os.environ.get("SESSIONS_TABLE", "")), _answers, build_assistant, plans=plans)


class _NoDeployer:
    """Until the request workflow is deployed, requests can be made and read but not deployed."""

    def start(self, req, bundle):
        raise Unavailable("sandbox deploys aren't set up yet")

    teardown = start

    def logs(self, req, after):
        raise Unavailable("sandbox deploys aren't set up yet")


deployer = StepFunctionsDeployer(os.environ["DEPLOY_STATE_MACHINE"], os.environ["TEARDOWN_STATE_MACHINE"], bucket) \
    if os.environ.get("DEPLOY_STATE_MACHINE") and bucket else _NoDeployer()
deploys = DeployService(DynamoRequestStore(os.environ["REQUESTS_TABLE"]), plans, deployer,
                        os.environ.get("SANDBOX_BOUNDARY_ARN", ""), S3SiteUploads(bucket) if bucket else None) \
    if os.environ.get("REQUESTS_TABLE") else None


def _deploys() -> DeployService:
    if deploys is None:
        raise Unavailable("deploy requests aren't set up")
    return deploys


def _user() -> str:
    claims = app.current_event.request_context.authorizer.jwt_claim or {}
    if not claims.get("sub"):  # only reachable if a route is deployed without the authorizer
        raise Unauthorized("authenticated Cognito user is required")
    return claims["sub"]


def _json(status: int, body) -> Response:
    data = body.model_dump_json() if hasattr(body, "model_dump_json") else json.dumps(body)
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=data)


@app.exception_handler(ChatError)
def _chat_error(exc: ChatError) -> Response:
    return _json(exc.status, {"detail": str(exc)})


@app.exception_handler(ValidationError)
def _invalid(exc: ValidationError) -> Response:
    return _json(422, {"detail": exc.errors(include_url=False, include_context=False)})


@app.get("/health")
def health():
    return _json(200, {"status": "ok"})


@app.post("/sessions")
def create_session():
    s = chat.create_session(_user())
    return _json(201, {"id": s.id, "created_at": s.created_at.isoformat()})


@app.get("/sessions/<session_id>")
def read_session(session_id: str):
    return _json(200, chat.get_session(_user(), session_id))


@app.post("/sessions/<session_id>/messages")
def post_message(session_id: str):
    body = MessageIn.model_validate_json(app.current_event.body or "")
    return _json(200, chat.post_message(_user(), session_id, body))


@app.post("/requests")
def create_request():
    body = DeployRequestIn.model_validate_json(app.current_event.body or "")
    return _json(201, _deploys().create(_user(), body.session_id, body.message_id))


@app.get("/requests")
def list_requests():
    return _json(200, [r.model_dump(mode="json") for r in _deploys().history(_user())])


@app.get("/requests/<request_id>")
def read_request(request_id: str):
    return _json(200, _deploys().get(_user(), request_id))


@app.post("/requests/<request_id>/approve")
def approve_request(request_id: str):
    return _json(200, _deploys().approve(_user(), request_id))


@app.post("/requests/<request_id>/upload")
def upload_link(request_id: str):
    return _json(200, _deploys().upload_link(_user(), request_id))


@app.post("/requests/<request_id>/upload/check")
def check_upload(request_id: str):
    return _json(200, _deploys().check_upload(_user(), request_id))


@app.post("/requests/<request_id>/reject")
def reject_request(request_id: str):
    raw = app.current_event.body
    body = RejectIn.model_validate_json(raw) if raw else RejectIn()
    return _json(200, _deploys().reject(_user(), request_id, body.feedback))


@app.post("/requests/<request_id>/teardown")
def teardown_request(request_id: str):
    return _json(200, _deploys().teardown(_user(), request_id))


@app.get("/requests/<request_id>/logs")
def request_logs(request_id: str):
    after = app.current_event.get_query_string_value("next")
    return _json(200, _deploys().logs(_user(), request_id, after[:200] if after else None))


@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event, context):
    return app.resolve(event, context)
