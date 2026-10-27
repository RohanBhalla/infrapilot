# InfraPilot infrastructure (AWS CDK)

Everything InfraPilot runs on AWS is defined here, so it can be created with one command and
removed with another. Nothing is created by hand in the console.

## Stacks

| Stack | What it creates |
|---|---|
| `InfraPilotChat` | The chat app: S3 + CloudFront website, Cognito login, API Gateway HTTP API with a Cognito JWT authorizer, the chat Lambda (`api/infrapilot_api/lambda_handler.py`: the assistant, searching `InfraPilotKnowledge`), and the DynamoDB session table |
| `InfraPilotKnowledge` | Document search: S3 docs bucket, S3 Vectors index, Bedrock Knowledge Base (Titan Text Embeddings V2, no re-chunking: it uses the chunks from `data_collection`) |

All resources are tagged `project=infrapilot`. The stack is pay-per-use and costs close to $0
when idle. The API is throttled to 10 requests/s (burst 20), and Lambda logs are kept for 14 days.

## Setup (once)

```bash
cd infra
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # synthesizes the stack and checks it; no AWS calls
```

You need AWS credentials for the team account in your shell and Node.js (for the CDK CLI, run
with `npx`). The deploy packages the chat Lambda with `pip` on your machine (Linux ARM wheels, no
Docker needed).

The chat Lambda reads the OpenAI key from SSM Parameter Store. CloudFormation can't create
encrypted parameters, so add it once by hand (`read -s` keeps the key off the screen and out of
your shell history):

```bash
read -s "KEY?OpenAI key: " && aws ssm put-parameter --region us-east-1 \
  --name /infrapilot/openai-api-key --type SecureString --value "$KEY" && unset KEY
``` Bootstrap the account once. This creates a small S3 bucket and the IAM roles CDK
deploys with:

```bash
npx aws-cdk@2 bootstrap aws://<account-id>/us-east-1
```

## Deploy

```bash
(cd ../frontend && npm ci && npm run build)   # the stack uploads frontend/dist
npx aws-cdk@2 diff                            # review the changes first
npx aws-cdk@2 deploy
```

The deploy uploads the website, writes `config.json` next to it with the real Cognito and API
values (the frontend reads it at startup), and clears the CloudFront cache. The outputs print
the website URL, API URL, and Cognito IDs.

Then add a test user in the Cognito console (**User pools → Users → Create user**). Self sign-up
is off, so only users the team creates can sign in.

For local frontend development against the deployed API, copy the outputs into
`frontend/.env.local` (see `frontend/.env.example`). `http://localhost:5173` is already allowed
for Cognito sign-in and CORS.

## Load the documents into the Knowledge Base

After deploying `InfraPilotKnowledge`, upload the local chunks and start an ingestion job:

```bash
scripts/sync_knowledge.sh
```

It syncs `data_collection/data/kb_export/` to the docs bucket (adding, updating and deleting files)
and waits for the Knowledge Base to embed the changes. Embedding the full corpus once costs about
$0.07 with Titan V2. To search it from `rag/` or `eval/`, set `INFRAPILOT_KB_ID` to the printed id
and use `--backend kb`.

## Tear down

```bash
npx aws-cdk@2 destroy
```

This deletes everything, including the session table and the Cognito users.
