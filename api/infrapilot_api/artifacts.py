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


# A user's built static site (decision 006), uploaded straight from the browser for one pending
# request. The bucket deletes uploads after a day; the sandbox only needs the zip at deploy time.
UPLOAD_SECONDS = 15 * 60


def site_key(request_id: str) -> str:
    return f"uploads/{request_id}/site.zip"


class S3SiteUploads:
    def __init__(self, bucket: str, client=None):
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket, self.client = bucket, client

    def link(self, request_id: str, max_bytes: int) -> dict:
        """A presigned POST: S3 itself refuses a file over `max_bytes`, and the link expires in 15 min."""
        post = self.client.generate_presigned_post(
            Bucket=self.bucket, Key=site_key(request_id),
            Conditions=[["content-length-range", 1, max_bytes]], ExpiresIn=UPLOAD_SECONDS)
        return {"method": "POST", "url": post["url"], "fields": post["fields"], "max_bytes": max_bytes}

    def read(self, request_id: str, max_bytes: int) -> bytes | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=site_key(request_id))
        except self.client.exceptions.NoSuchKey:
            return None
        return obj["Body"].read(max_bytes + 1)  # one byte more, so a too-large file is still caught

    def exists(self, request_id: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=site_key(request_id))
        except self.client.exceptions.ClientError:
            return False
        return True


class InMemorySiteUploads:
    """Local development: the browser PUTs the zip to the local API (`PUT /requests/{id}/site`)."""

    def __init__(self):
        self._files: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def link(self, request_id: str, max_bytes: int) -> dict:
        return {"method": "PUT", "url": f"/requests/{request_id}/site", "fields": {}, "max_bytes": max_bytes}

    def put(self, request_id: str, data: bytes) -> None:
        with self._lock:
            self._files[request_id] = data

    def read(self, request_id: str, max_bytes: int) -> bytes | None:
        with self._lock:
            data = self._files.get(request_id)
        return data[:max_bytes + 1] if data is not None else None

    def exists(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._files
