"""Chat API on AWS Lambda behind API Gateway (HTTP API, payload v2) with a Cognito JWT authorizer.

Powertools routes the request; the logic is the same ChatService the local FastAPI app uses.
API Gateway checks the token and adds the CORS headers, so neither happens here.

Environment: SESSIONS_TABLE, OPENAI_KEY_PARAM (SSM SecureString name), INFRAPILOT_BACKEND=kb,
INFRAPILOT_KB_ID, INFRAPILOT_DATA_DIR (bundled versions.json).
"""

from __future__ import annotations

import json
import os

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers
from aws_lambda_powertools.utilities import parameters
from pydantic import ValidationError

from .dynamo import DynamoSessionStore
from .schemas import MessageIn
from .service import ChatError, ChatService, Unauthorized, build_answers, build_assistant

logger = Logger(service="infrapilot-chat")
copy_config_to_registered_loggers(source_logger=logger)  # JSON logs from rag/assistant too
app = APIGatewayHttpResolver()


def _answers():
    # Read the key once per Lambda instance; it stays in memory, never in an environment
    # variable that the console would show.
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = parameters.get_parameter(os.environ["OPENAI_KEY_PARAM"], decrypt=True)
    return build_answers()


chat = ChatService(DynamoSessionStore(os.environ.get("SESSIONS_TABLE", "")), _answers, build_assistant)


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


@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event, context):
    return app.resolve(event, context)
