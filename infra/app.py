"""CDK entry point: `npx aws-cdk@2 deploy` from this folder (see README.md)."""

import os

import aws_cdk as cdk

from infrapilot_infra.chat_stack import ChatStack
from infrapilot_infra.knowledge_stack import KnowledgeStack

app = cdk.App()
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1")
knowledge = KnowledgeStack(app, "InfraPilotKnowledge", env=env,
                           description="InfraPilot document search: docs bucket, S3 Vectors, Bedrock Knowledge Base")
ChatStack(app, "InfraPilotChat", env=env, knowledge_base_id=knowledge.knowledge_base_id,
          knowledge_base_arn=knowledge.knowledge_base_arn,
          description="InfraPilot chat app: website, Cognito login, chat API, session table")

cdk.Tags.of(app).add("project", "infrapilot")
cdk.Tags.of(app).add("owner", "pair-b")
cdk.Tags.of(app).add("lifecycle", "persistent")
app.synth()
