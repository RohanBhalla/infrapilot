# InfraPilot Chat API

FastAPI app for component C8 of the [development plan](../docs/DEVELOPMENT_PLAN.md): chat sessions
and grounded answers with citations, using the same answer service as `infrapilot-ask`.

```bash
cd data_collection
.venv/bin/pip install -e "../api[dev]"
.venv/bin/infrapilot-api                  # http://127.0.0.1:8000, interactive docs at /docs
.venv/bin/infrapilot-api --reload         # restart on code changes
```

Needs the same setup as `infrapilot-ask`: AWS credentials (question embeddings) and
`OPENAI_API_KEY` in the repo's `.env`. Each answer costs about $0.003–0.006.

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

- **Follow-ups:** when a session has history, the latest message is first rewritten into a
  standalone question ("and on Fargate?" → "How do I deploy to ECS on Fargate?") with one
  small extra model call (~$0.0005). The reply shows it as `standalone_question`. If the
  rewrite fails, the message is used as is.
- **Sessions** are kept in memory (`InMemorySessionStore`) and lost on restart. A DynamoDB
  store will implement the same interface for AWS.
- **Errors:** 404 unknown session, 422 invalid request (unknown fields are rejected), 400 bad
  filter, 502 model or search failure (nothing is saved), 503 service not configured.
- **CORS** allows `http://localhost:5173` (Vite dev server); set `INFRAPILOT_CORS_ORIGINS`.
- `INFRAPILOT_BACKEND=kb` uses the Bedrock Knowledge Base retriever instead of local LanceDB (Phase 2).

Not yet: authentication (Cognito, Phase 3), streaming, and the `/requests/*` endpoints for
generate/approve/deploy (Phases 3–4).

## Tests

```bash
cd api && ../data_collection/.venv/bin/pytest -q
```

Offline: a fake answer service and rewriter, no network calls.
