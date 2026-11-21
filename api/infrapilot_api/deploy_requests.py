"""Deploy requests (plan C12, C14): one record per approved-or-not plan, and its sandbox's life.

    pending ──approve──▶ deploying ──▶ deployed ──teardown──▶ tearing_down ──▶ torn_down
       │                     └──────▶ failed ───teardown──▶ tearing_down
       └──reject──▶ rejected

A user may have one active sandbox (deploying, deployed, failed or tearing_down: a failed stack
still exists until it's deleted). That rule is kept by a lock record per user, taken when a
request is approved and released when its stack is gone, so two approvals at the same moment
can't both win.

Locally the records are in memory; on AWS they're in DynamoDB (`DynamoRequestStore`).
"""

from __future__ import annotations

import json
import secrets
import string
import threading
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .schemas import utcnow

Status = Literal["pending", "rejected", "deploying", "deployed", "failed", "tearing_down", "torn_down"]
ACTIVE: frozenset[str] = frozenset({"deploying", "deployed", "failed", "tearing_down"})
_ID_CHARS = string.ascii_lowercase + string.digits


def new_request_id() -> str:
    """10 lowercase letters and digits: the sandbox stack is `ip-sbx-<id>` (decision 004)."""
    return "".join(secrets.choice(_ID_CHARS) for _ in range(10))


class DeployRequest(BaseModel):
    id: str
    user_id: str
    session_id: str
    message_id: str
    plan_key: str
    intent: str
    summary: str
    stack_name: str
    stack_id: str | None = Field(None, description="Set once CloudFormation creates the stack; its events stay "
                                                   "readable by id after it's deleted")
    status: Status = "pending"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    deploy_started_at: datetime | None = None
    teardown_at: datetime | None = Field(None, description="When the sandbox is deleted automatically")
    outputs: dict[str, str] = Field(default_factory=dict, description="Stack outputs (site URL, function URL)")
    error: str | None = None
    feedback: str | None = Field(None, description="Why the plan was rejected")


class Conflict(Exception):
    """The request isn't in a state that allows this change (or the user's lock is taken)."""

    def __init__(self, message: str, active_id: str | None = None):
        super().__init__(message)
        self.active_id = active_id


class RequestStore(Protocol):
    def create(self, req: DeployRequest) -> None: ...
    def get(self, request_id: str) -> DeployRequest | None: ...
    def list(self, user: str, limit: int = 50) -> list[DeployRequest]: ...
    def transition(self, request_id: str, allowed: set[str], **fields) -> DeployRequest: ...
    def claim(self, user: str, request_id: str, **fields) -> DeployRequest: ...
    def release(self, user: str, request_id: str) -> None: ...
    def locks(self) -> list[tuple[str, str]]:
        """Every (user, request id) lock: the janitor checks none is left behind."""


class InMemoryRequestStore:
    def __init__(self):
        self._items: dict[str, DeployRequest] = {}
        self._active: dict[str, str] = {}  # user -> request id holding the lock
        self._lock = threading.Lock()

    def create(self, req: DeployRequest) -> None:
        with self._lock:
            self._items[req.id] = req.model_copy(deep=True)

    def get(self, request_id: str) -> DeployRequest | None:
        with self._lock:
            r = self._items.get(request_id)
            return r.model_copy(deep=True) if r else None

    def list(self, user: str, limit: int = 50) -> list[DeployRequest]:
        with self._lock:
            mine = [r.model_copy(deep=True) for r in self._items.values() if r.user_id == user]
        return sorted(mine, key=lambda r: r.created_at, reverse=True)[:limit]

    def _set(self, r: DeployRequest, fields: dict) -> DeployRequest:
        updated = r.model_copy(update={**fields, "updated_at": utcnow()})
        self._items[r.id] = DeployRequest.model_validate(updated.model_dump())  # re-validate the fields
        return self._items[r.id].model_copy(deep=True)

    def transition(self, request_id: str, allowed: set[str], **fields) -> DeployRequest:
        with self._lock:
            r = self._items.get(request_id)
            if r is None or r.status not in allowed:
                raise Conflict(f"request is {r.status if r else 'missing'}")
            return self._set(r, fields)

    def claim(self, user: str, request_id: str, **fields) -> DeployRequest:
        """pending → `fields` (deploying), only while the user has no other active sandbox."""
        with self._lock:
            r = self._items.get(request_id)
            if r is None or r.status != "pending":
                raise Conflict(f"request is {r.status if r else 'missing'}")
            if user in self._active:
                raise Conflict("active sandbox", self._active[user])
            self._active[user] = request_id
            return self._set(r, fields)

    def release(self, user: str, request_id: str) -> None:
        with self._lock:
            if self._active.get(user) == request_id:
                del self._active[user]

    def locks(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._active.items())


class DynamoRequestStore:
    """Table `requests` (InfraPilotChat stack), key `id`:

        id = <request id>       the request; GSI `by_user` (user_id, created_at) lists a user's history
        id = ACTIVE#<user>      the user's lock: holds `request_id`; has no user_id, so it's not in the GSI

    Request items keep their datetimes as ISO text and `outputs` as JSON text.
    """

    def __init__(self, table_name: str, table=None):
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        # The resource's client converts plain Python values to DynamoDB types, transactions too.
        self.table, self.client = table, table.meta.client

    @staticmethod
    def _item(fields: dict) -> dict:
        out = {}
        for k, v in fields.items():
            if isinstance(v, datetime):
                v = v.isoformat()
            elif k == "outputs":
                v = json.dumps(v)
            out[k] = v
        return out

    @staticmethod
    def _model(item: dict) -> DeployRequest:
        item = dict(item)
        item["outputs"] = json.loads(item.get("outputs") or "{}")
        return DeployRequest.model_validate(item)

    def create(self, req: DeployRequest) -> None:
        item = {k: v for k, v in self._item(req.model_dump()).items() if v is not None}
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(id)")

    def get(self, request_id: str) -> DeployRequest | None:
        item = self.table.get_item(Key={"id": request_id}, ConsistentRead=True).get("Item")
        return self._model(item) if item and "user_id" in item else None

    def list(self, user: str, limit: int = 50) -> list[DeployRequest]:
        from boto3.dynamodb.conditions import Key

        page = self.table.query(IndexName="by_user", KeyConditionExpression=Key("user_id").eq(user),
                                ScanIndexForward=False, Limit=limit)
        return [self._model(i) for i in page.get("Items", [])]

    def _update(self, request_id: str, allowed: set[str], fields: dict) -> dict:
        """The UpdateItem arguments, in the form a transaction also takes."""
        fields = self._item({**fields, "updated_at": utcnow()})
        names, values, sets, removes = {}, {}, [], []
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            if v is None:
                removes.append(f"#f{i}")
            else:
                values[f":v{i}"] = v
                sets.append(f"#f{i} = :v{i}")
        allowed_values = {f":s{i}": s for i, s in enumerate(sorted(allowed))}
        expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
        return {
            "Key": {"id": request_id},
            "UpdateExpression": expr,
            "ConditionExpression": "attribute_exists(user_id) AND #status IN (" + ", ".join(allowed_values) + ")",
            "ExpressionAttributeNames": {**names, "#status": "status"},
            "ExpressionAttributeValues": {**values, **allowed_values},
        }

    def transition(self, request_id: str, allowed: set[str], **fields) -> DeployRequest:
        from botocore.exceptions import ClientError

        try:
            out = self.client.update_item(TableName=self.table.name, ReturnValues="ALL_NEW",
                                     **self._update(request_id, allowed, fields))
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                current = self.get(request_id)
                raise Conflict(f"request is {current.status if current else 'missing'}") from exc
            raise
        return self._model(out["Attributes"])

    def claim(self, user: str, request_id: str, **fields) -> DeployRequest:
        from botocore.exceptions import ClientError

        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.table.name, **self._update(request_id, {"pending"}, fields)}},
                {"Put": {"TableName": self.table.name,
                         "Item": {"id": f"ACTIVE#{user}", "request_id": request_id},
                         "ConditionExpression": "attribute_not_exists(id)"}},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            # One code per item; DynamoDB says "None" for an item whose condition held.
            reasons = [r.get("Code") or "None" for r in exc.response.get("CancellationReasons", [])] + ["None"] * 2
            if reasons[0] == "None" and reasons[1] == "ConditionalCheckFailed":  # pending, but locked
                lock = self.table.get_item(Key={"id": f"ACTIVE#{user}"}, ConsistentRead=True).get("Item") or {}
                raise Conflict("active sandbox", lock.get("request_id")) from exc
            current = self.get(request_id)
            raise Conflict(f"request is {current.status if current else 'missing'}") from exc
        return self.get(request_id)

    def release(self, user: str, request_id: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self.table.delete_item(Key={"id": f"ACTIVE#{user}"}, ConditionExpression="request_id = :r",
                                   ExpressionAttributeValues={":r": request_id})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def locks(self) -> list[tuple[str, str]]:
        """A scan, but only the janitor calls it, once an hour, and the table is small."""
        from boto3.dynamodb.conditions import Attr

        out, kwargs = [], {"FilterExpression": Attr("id").begins_with("ACTIVE#"), "ConsistentRead": True}
        while True:
            page = self.table.scan(**kwargs)
            out += [(i["id"].removeprefix("ACTIVE#"), i["request_id"]) for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
