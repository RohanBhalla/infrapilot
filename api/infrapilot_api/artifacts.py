"""Generated plans saved to S3 (the InfraPilotChat stack's artifacts bucket).

Each ready task's full plan (files, checks, setup, sources) is saved as JSON, so the request
workflow (C12) can review and deploy it later without reading it back out of the chat:

    plans/<user>/<session id>/<message id>.json

The bucket deletes plans after 30 days, like the session table. Locally nothing is saved.
"""

from __future__ import annotations

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
