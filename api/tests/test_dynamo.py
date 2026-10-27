"""DynamoSessionStore against moto's in-memory DynamoDB (no AWS calls)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from infrapilot_api.dynamo import DynamoSessionStore
from infrapilot_api.schemas import Message
from infrapilot_assistant.state import DialogState
from infrapilot_rag.models import Citation


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="sessions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        yield DynamoSessionStore("sessions", table=table)


def test_round_trip_keeps_messages_in_order_with_citations(store):
    s = store.create("alice")
    q = Message(id="m1", role="user", content="How do I deploy to ECS?")
    a = Message(id="m2", role="assistant", content="Use ... [1]", refused=False,
                citations=[Citation(n=1, title="ECS", url="https://example.com", chunk_id="c1")])
    store.append("alice", s.id, q, a)
    got = store.get("alice", s.id)
    assert [m.id for m in got.messages] == ["m1", "m2"]
    assert got.messages[1].citations[0].url == "https://example.com" and got.messages[1].refused is False
    assert got.messages[0].refused is None


def test_dialog_state_is_saved(store):
    s = store.create("alice")
    store.save_dialog("alice", s.id, DialogState(intent="deploy_lambda", params={"memory_mb": 512, "x": True},
                                                 status="collecting"))
    d = store.get("alice", s.id).dialog
    assert d.intent == "deploy_lambda" and d.params == {"memory_mb": 512, "x": True}


def test_users_cannot_see_each_others_sessions(store):
    s = store.create("alice")
    assert store.get("bob", s.id) is None
    store.save_dialog("bob", s.id, DialogState(intent="qa"))  # must not create a session for bob
    assert store.get("bob", s.id) is None and store.get("alice", s.id).dialog.intent is None


def test_unknown_session_is_none(store):
    assert store.get("alice", "nope") is None


def test_items_expire(store):
    s = store.create("alice")
    item = store.table.get_item(Key={"PK": "USER#alice", "SK": f"SESSION#{s.id}#META"})["Item"]
    assert int(item["expires_at"]) > 0


def test_reads_sessions_written_by_the_first_test_lambda(store):
    # That Lambda stored the dialog as a map, not JSON text.
    store.table.put_item(Item={"PK": "USER#alice", "SK": "SESSION#old#META", "session_id": "old",
                               "created_at": "2026-10-24T00:00:00+00:00",
                               "dialog": {"intent": None, "params": {}, "asked": [], "status": "idle",
                                          "awaiting_confirmation": False, "confirmed": False,
                                          "defaults_shown": False}})
    store.table.put_item(Item={"PK": "USER#alice", "SK": "SESSION#old#MESSAGE#2026-10-24T00:00:01+00:00#x",
                               "message_id": "x", "role": "user", "content": "hi",
                               "created_at": "2026-10-24T00:00:01+00:00"})
    got = store.get("alice", "old")
    assert got.dialog.status == "idle" and got.messages[0].content == "hi"
