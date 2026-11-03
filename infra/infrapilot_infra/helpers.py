"""Descriptions for the Lambda functions CDK adds on its own, so every function in the console
says what it's for. Call at the end of a stack, after the buckets that create them."""

from __future__ import annotations

from aws_cdk import CfnResource, Stack
from aws_cdk import aws_lambda as lambda_

AUTO_DELETE = "Custom::S3AutoDeleteObjectsCustomResourceProvider"
BUCKET_DEPLOYMENT = "Custom::CDKBucketDeployment"


def describe_cdk_helpers(stack: Stack, emptied_buckets: str) -> None:
    for child in stack.node.children:
        if child.node.id == AUTO_DELETE:
            handler = child.node.find_child("Handler")
            assert isinstance(handler, CfnResource)
            handler.add_property_override(
                "Description", f"InfraPilot (CDK helper): empties {emptied_buckets} when the stack is "
                               "destroyed, so the buckets can be deleted.")
        elif child.node.id.startswith(BUCKET_DEPLOYMENT):
            fn = child.node.default_child
            assert isinstance(fn, lambda_.CfnFunction)
            fn.description = ("InfraPilot (CDK helper): uploads the website and config.json to the site "
                              "bucket and clears the CloudFront cache on deploy.")
