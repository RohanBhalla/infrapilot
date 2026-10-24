"""Barebones authenticated chat API for API Gateway HTTP API + Lambda.

This deliberately returns a fixed test answer; it does not call the RAG or LLM services.
Configure a DynamoDB table with string partition key PK and string sort key SK.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

_table = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_table():
    global _table
    if _table is None:
        table_name = os.environ.get("TABLE_NAME")
        if not table_name:
            raise RuntimeError("TABLE_NAME Lambda environment variable is required")
        _table = boto3.resource("dynamodb").Table(table_name)
    return _table


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def _message_item(user_sub: str, session_id: str, role: str, content: str) -> dict[str, str]:
    created_at = _now()
    message_id = uuid.uuid4().hex
    return {
        "PK": f"USER#{user_sub}",
        "SK": f"SESSION#{session_id}#MESSAGE#{created_at}#{message_id}",
        "message_id": message_id,
        "role": role,
        "content": content,
        "created_at": created_at,
    }


def _create_session(table, user_sub: str) -> dict[str, Any]:
    session_id = uuid.uuid4().hex
    created_at = _now()
    table.put_item(
        Item={
            "PK": f"USER#{user_sub}",
            "SK": f"SESSION#{session_id}#META",
            "session_id": session_id,
            "created_at": created_at,
            "dialog": {
                "intent": None,
                "params": {},
                "asked": [],
                "awaiting_confirmation": False,
                "confirmed": False,
                "defaults_shown": False,
                "status": "idle",
            },
        },
        ConditionExpression=Attr("PK").not_exists(),
    )
    return _response(201, {"id": session_id, "created_at": created_at})


def _session_records(table, user_sub: str, session_id: str) -> tuple[dict | None, list[dict]]:
    pk = f"USER#{user_sub}"
    prefix = f"SESSION#{session_id}#"
    result = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(prefix),
        ConsistentRead=True,
    )
    items = [
        item for item in result.get("Items", [])
        if item.get("PK") == pk and item.get("SK", "").startswith(prefix)
    ]
    metadata = next((item for item in items if item["SK"] == f"{prefix}META"), None)
    messages = sorted(
        (item for item in items if item.get("role") in ("user", "assistant")),
        key=lambda item: (item.get("created_at", ""), item.get("message_id", "")),
    )
    return metadata, messages


def _get_session(table, user_sub: str, session_id: str) -> dict[str, Any]:
    metadata, items = _session_records(table, user_sub, session_id)
    if metadata is None:
        return _response(404, {"detail": "session not found"})
    messages = [
        {
            "id": item["message_id"],
            "role": item["role"],
            "content": item["content"],
            "created_at": item["created_at"],
            "citations": [],
            "refused": False if item["role"] == "assistant" else None,
        }
        for item in items
    ]
    return _response(
        200,
        {
            "id": session_id,
            "created_at": metadata["created_at"],
            "messages": messages,
            "dialog": metadata["dialog"],
        },
    )


def _post_message(table, user_sub: str, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    content = body.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 2000:
        return _response(422, {"detail": "content must be a non-empty string of at most 2000 characters"})

    metadata, _ = _session_records(table, user_sub, session_id)
    if metadata is None:
        return _response(404, {"detail": "session not found"})

    user_message = _message_item(user_sub, session_id, "user", content)
    assistant_content = (
        "This is a test reply from the AWS Lambda API. Documentation search and embeddings "
        "are not connected yet."
    )
    assistant_message = _message_item(user_sub, session_id, "assistant", assistant_content)
    table.put_item(Item=user_message)
    table.put_item(Item=assistant_message)

    return _response(
        200,
        {
            "session_id": session_id,
            "type": "answer",
            "message": {
                "id": assistant_message["message_id"],
                "role": "assistant",
                "content": assistant_content,
                "created_at": assistant_message["created_at"],
                "citations": [],
                "refused": False,
            },
            "intent": "qa",
            "confidence": 1.0,
            "params": {},
            "questions": [],
            "standalone_question": content,
            "model": "test-stub",
            "latency_ms": 0,
            "cost_usd": 0.0,
        },
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle API Gateway HTTP API payload format 2.0 events."""
    del context
    request_context = event.get("requestContext", {})
    http = request_context.get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath", "")

    if method == "GET" and path == "/health":
        return _response(200, {"status": "ok"})

    claims = _claims(event)
    user_sub = claims.get("sub")
    if not user_sub:
        return _response(401, {"detail": "authenticated Cognito user is required"})

    table = _get_table()

    if method == "POST" and path == "/sessions":
        return _create_session(table, user_sub)

    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "sessions":
        if method == "GET":
            return _get_session(table, user_sub, parts[1])
        return _response(405, {"detail": "method not allowed"})

    if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "messages":
        if method != "POST":
            return _response(405, {"detail": "method not allowed"})
        try:
            body = json.loads(event.get("body") or "")
        except json.JSONDecodeError:
            return _response(400, {"detail": "request body must be valid JSON"})
        if not isinstance(body, dict):
            return _response(422, {"detail": "request body must be a JSON object"})
        return _post_message(table, user_sub, parts[1], body)

    return _response(404, {"detail": "route not found"})
