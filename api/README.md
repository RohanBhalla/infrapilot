# InfraPilot Chat API

Component C8 of the [development plan](../docs/DEVELOPMENT_PLAN.md): chat sessions and grounded
answers with citations. The logic lives in `ChatService` (`service.py`), with two thin entry points:

| Entry point | Where | Sessions | Search | Users |
|---|---|---|---|---|
| `app.py` (FastAPI) | Local development, docs at `/docs` | In memory | Local LanceDB | One user, `local` |
| `lambda_handler.py` (Powertools router) | AWS Lambda behind API Gateway | DynamoDB (`dynamo.py`) | Bedrock Knowledge Base | Cognito `sub` claim |

```bash
cd data_collection
.venv/bin/pip install -e "../api[dev]"
.venv/bin/infrapilot-api                  # http://127.0.0.1:8000, interactive docs at /docs
.venv/bin/infrapilot-api --reload         # restart on code changes
```

Needs the same setup as `infrapilot-ask`: AWS credentials (question embeddings) and
`OPENAI_API_KEY` in the repo's `.env`. Each message costs about $0.0014 for the assistant, plus ~$0.003 when it's answered from the docs.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/sessions` | Start a conversation; returns its `id` |
| GET | `/sessions/{id}` | Conversation history |
| POST | `/sessions/{id}/messages` | Send `{"content": "...", "filters": {"category": "..."}}`; returns the reply with citations |

```bash
SID=$(curl -s -X POST localhost:8000/sessions | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST localhost:8000/sessions/$SID/messages -H 'content-type: application/json' \
  -d '{"content": "How do I deploy a new image to ECS from GitHub Actions?"}'
```

The OpenAPI spec is at `/openapi.json`; the frontend can be built against it.

## How it works

- **Every message goes through the assistant** ([`assistant/`](../assistant/README.md)) first. The
  reply's `type` says what happened:
  - `answer`: a question (or a pasted error), answered from the docs with citations
  - `clarification`: a task is missing required details; `questions` lists them, one per detail
  - `confirmation`: unsure it's a task, so it asks first
  - `ready`: all details known; `params` has them, with defaults filled in
  - `out_of_scope`: not something InfraPilot does
- **Follow-ups:** the classifier also rewrites each message as a standalone question
  ("and on Fargate?" → "How do I deploy to ECS on Fargate?"), shown as `standalone_question`.
- **Task state** (`dialog`) is saved with the session, so answers to clarification questions
  and changes ("use us-west-2 instead") apply to the task in progress.
- **Sessions** belong to a user and nobody else can read them. Locally they're kept in memory and
  lost on restart. On AWS, `DynamoSessionStore` keeps one partition per user (`PK=USER#<sub>`), the
  session's task state in a `META` item, one item per message, and a 30-day TTL.
- **Errors:** 401 no login (AWS only), 404 unknown session, 422 invalid request (unknown fields are rejected), 400 bad
  filter, 502 model or search failure (nothing is saved), 503 service not configured.
- **CORS** allows `http://localhost:5173` (Vite dev server); set `INFRAPILOT_CORS_ORIGINS`.
- `INFRAPILOT_BACKEND=kb` uses the Bedrock Knowledge Base retriever instead of local LanceDB (Phase 2).

On AWS the Lambda reads the OpenAI key from SSM Parameter Store once per instance, and API Gateway
does the Cognito check and CORS. Deployment: [`infra/`](../infra/README.md).

Not yet: streaming, and the `/requests/*` endpoints for generate/approve/deploy (Phases 3–4).

## Tests

```bash
cd api && ../data_collection/.venv/bin/pytest -q
```

Offline: a fake answer service and scripted classifier, moto's in-memory DynamoDB, and API
Gateway–style events for the Lambda handler. No network calls.
