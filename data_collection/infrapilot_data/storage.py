
"""AWS-backed HTTP response cache using DynamoDB and S3."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


class AwsPageCache:
    """Persist HTTP response bodies in S3 and cache metadata in DynamoDB."""

    def __init__(
        self,
        table_name: str,
        bucket_name: str,
        region: str = "us-east-1",
    ):
        self.s3 = boto3.client("s3", region_name=region)
        self.table = boto3.resource(
            "dynamodb", region_name=region
        ).Table(table_name)
        self.bucket_name = bucket_name

    def get(self, url: str) -> dict | None:
        """Retrieve cached metadata and response body for a URL."""

        result = self.table.get_item(Key={"url": url})
        item = result.get("Item")

        if not item:
            return None

        try:
            response = self.s3.get_object(
                Bucket=self.bucket_name,
                Key=item["raw_s3_key"],
            )
            text = response["Body"].read().decode("utf-8")
        except (KeyError, ClientError) as exc:
            # A missing snapshot must not result in an unusable 304.
            if isinstance(exc, ClientError):
                code = exc.response.get("Error", {}).get("Code")
                if code not in ("NoSuchKey", "404", "NotFound"):
                    raise
            return None

        return {**item, "text": text}

    def save(
        self,
        url: str,
        text: str,
        content_type: str,
        etag: str | None,
        last_modified: str | None,
    ) -> dict:
        """Save an immutable response snapshot, then update its DynamoDB record."""

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # Content-addressed key: identical content reuses the same S3 object.
        s3_key = f"raw/{url_hash}/{content_hash}.txt"

        self.s3.put_object(
            Bucket=self.bucket_name,
            Key=s3_key,
            Body=text.encode("utf-8"),
            ContentType=content_type or "text/plain",
        )

        item = {
            "url": url,
            "content_hash": content_hash,
            "raw_s3_key": s3_key,
            "content_type": content_type or "text/plain",
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

        if etag:
            item["etag"] = etag
        if last_modified:
            item["last_modified"] = last_modified

        # Update DynamoDB only after the S3 upload succeeds.
        self.table.put_item(Item=item)

        return item
