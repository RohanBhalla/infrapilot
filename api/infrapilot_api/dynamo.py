"""DynamoDB session store (the InfraPilotChat stack's session table).

One partition per user, so a user can only ever query their own sessions:

    PK = USER#<cognito sub>
    SK = SESSION#<id>#META                          created_at, dialog (task state)
    SK = SESSION#<id>#MESSAGE#<created_at>#<msg id> role, content, citations, refused

Messages sort by time within a session. Every item has `expires_at` (epoch seconds), which
DynamoDB's TTL uses to delete old sessions for free.
"""

from __future__ import annotations

import json
import time
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from infrapilot_assistant.state import DialogState

from .schemas import Message, Session

TTL_DAYS = 30


def _plain(value):
    """DynamoDB returns numbers as Decimal; turn them back into int/float."""
    return json.loads(json.dumps(value, default=lambda d: int(d) if d == int(d) else float(d)))


class DynamoSessionStore:
    def __init__(self, table_name: str, ttl_days: int = TTL_DAYS, table=None):
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table, self.ttl_days = table, ttl_days

    def _expires(self) -> int:
        return int(time.time()) + self.ttl_days * 86400

    @staticmethod
    def _pk(user: str) -> str:
        return f"USER#{user}"

    def create(self, user: str) -> Session:
        s = Session(id=uuid.uuid4().hex)
        self.table.put_item(
            Item={"PK": self._pk(user), "SK": f"SESSION#{s.id}#META", "session_id": s.id,
                  "created_at": s.created_at.isoformat(), "dialog": s.dialog.model_dump_json(),
                  "expires_at": self._expires()},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return s

    def _items(self, user: str, session_id: str) -> list[dict]:
        kwargs = {"KeyConditionExpression": Key("PK").eq(self._pk(user))
                  & Key("SK").begins_with(f"SESSION#{session_id}#"), "ConsistentRead": True}
        items = []
        while True:
            page = self.table.query(**kwargs)
            items += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def get(self, user: str, session_id: str) -> Session | None:
        items = self._items(user, session_id)
        meta = next((i for i in items if i["SK"].endswith("#META")), None)
        if meta is None:
            return None
        dialog = meta.get("dialog") or {}
        # Older items (the first test Lambda) stored the dialog as a map instead of JSON text.
        dialog = DialogState.model_validate_json(dialog) if isinstance(dialog, str) else \
            DialogState.model_validate(_plain(dialog))
        messages = [
            Message(id=i["message_id"], role=i["role"], content=i["content"], created_at=i["created_at"],
                    citations=json.loads(i.get("citations") or "[]"), refused=i.get("refused"))
            for i in items if "#MESSAGE#" in i["SK"]  # SK order = time order
        ]
        return Session(id=session_id, created_at=meta["created_at"], messages=messages, dialog=dialog)

    def append(self, user: str, session_id: str, *messages: Message) -> None:
        expires = self._expires()
        with self.table.batch_writer() as batch:
            for m in messages:
                batch.put_item(Item={
                    "PK": self._pk(user),
                    "SK": f"SESSION#{session_id}#MESSAGE#{m.created_at.isoformat()}#{m.id}",
                    "message_id": m.id, "role": m.role, "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    "citations": json.dumps([c.model_dump() for c in m.citations]),
                    "refused": m.refused, "expires_at": expires,
                })

    def save_dialog(self, user: str, session_id: str, dialog: DialogState) -> None:
        try:
            self.table.update_item(
                Key={"PK": self._pk(user), "SK": f"SESSION#{session_id}#META"},
                UpdateExpression="SET dialog = :d, expires_at = :e",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":d": dialog.model_dump_json(), ":e": self._expires()},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

