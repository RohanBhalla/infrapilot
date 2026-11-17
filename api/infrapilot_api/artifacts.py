"""Generated plans saved to S3 (the InfraPilotChat stack's artifacts bucket).

Each ready task's full plan (files, checks, setup, sources) is saved as JSON, so the request
workflow (C12) can review and deploy it later without reading it back out of the chat:

    plans/<user>/<session id>/<message id>.json

The bucket deletes plans after 30 days, like the session table. Locally they are kept in memory.
"""

from __future__ import annotations

import threading

from infrapilot_assistant.plan import Plan


def plan_key(user: str, session_id: str, message_id: str) -> str:
    return f"plans/{user}/{session_id}/{message_id}.json"


class S3PlanStore:
    def __init__(self, bucket: str, client=None):
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket, self.client = bucket, client

    def save(self, user: str, session_id: str, message_id: str, plan: Plan) -> str:
        key = plan_key(user, session_id, message_id)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=plan.model_dump_json().encode(),
                               ContentType="application/json")
        return key

    def load(self, key: str) -> Plan | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        return Plan.model_validate_json(body)


class InMemoryPlanStore:
    """Local development: plans live as long as the process, like the sessions."""

    def __init__(self):
        self._plans: dict[str, str] = {}
        self._lock = threading.Lock()

    def save(self, user: str, session_id: str, message_id: str, plan: Plan) -> str:
        key = plan_key(user, session_id, message_id)
        with self._lock:
            self._plans[key] = plan.model_dump_json()
        return key

    def load(self, key: str) -> Plan | None:
        with self._lock:
            data = self._plans.get(key)
        return Plan.model_validate_json(data) if data else None
