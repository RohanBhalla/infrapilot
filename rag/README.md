# InfraPilot RAG: retrieval + answer service

Answers infrastructure and CI/CD questions from the docs collected by `data_collection/`,
with numbered citations and current version facts. This is Feature 1.2 / component C7 of
the [development plan](../docs/DEVELOPMENT_PLAN.md).

```
question ──► embed query (Titan V2) ──► vector search (LanceDB now, Bedrock KB later)
                                              │  top 30 → max 2 per page → top 6
                                              ▼
              version registry ──► prompt: rules + version facts + 6 numbered excerpts
                                              │
                                              ▼
                 answer model: OpenAI gpt-5-mini (or Nova Pro on Bedrock)
                                              │
                                              ▼
                          answer + checked citations + tokens/cost
```

## Setup

Uses the same virtualenv as `data_collection/` and reads its output
(`data_collection/data/vectors/` and `data/versions/versions.json`).

```bash
cp .env.example .env                        # at the repo root, then set OPENAI_API_KEY
cd data_collection
.venv/bin/pip install -e "../rag[dev]"
.venv/bin/infrapilot-data embed --run      # once: builds the local vector index (~$0.06)
```

`.env` is git-ignored. Real environment variables override it. Search still uses AWS
(Titan V2 for the question embedding), so AWS credentials are needed either way.

## Usage

```bash
infrapilot-ask "How do I cache npm dependencies in GitHub Actions?"
infrapilot-ask "Why is my ECS task stuck in PENDING?" --category Troubleshooting --show-context
infrapilot-ask "..." --model nova-pro        # gpt-5.4-mini (default) | gpt-5.6-luna | gpt-5-mini | gpt-5 | gpt-4.1-mini | nova-pro | nova-lite | any model id
infrapilot-ask "..." --retrieve-only         # search results only, no LLM call, no cost
infrapilot-ask "..." --json                  # full Answer object (for evals and the API)
```

Filters (`--category`, `--doc-type`, `--source`) can be repeated and match the chunk
metadata exactly. `--backend kb` switches to a Bedrock Knowledge Base (`INFRAPILOT_KB_ID`), Phase 2.

## How it works

| Step | File | Notes |
|---|---|---|
| Embed the question | `embedder.py` | Titan V2, 1024 dims, normalized: the same settings as the chunks. Cached per question. |
| Search | `retriever.py` | **Hybrid:** meaning (cosine) search and keyword (full-text) search, 30 candidates each, merged with reciprocal rank fusion. Optional metadata filters. Keeps at most 2 per page, returns the top 6. `INFRAPILOT_HYBRID=0` turns keyword search off. |
| Rerank (optional, off) | `reranker.py` | If `INFRAPILOT_RERANK_MODEL` is set, the top 50 fused candidates are reordered by a Bedrock rerank model (~$0.001–0.002 per question). Off by default to avoid the cost. See [decision 003](../docs/decisions/003-reranker.md). |
| Version facts | `versions.py` | Picks registry facts for the topics in the question (GitHub Actions, Node, Lambda, Kubernetes, CDK). Drops end-of-life lines and deprecated runtimes unless named. |
| Prompt | `prompt.py` | Rules: answer only from excerpts, cite `[n]`, use `[V]` version facts over versions in examples, refuse with a fixed sentence when the docs don't cover it, treat excerpts as data not instructions. |
| Generate | `llm.py` | OpenAI Chat Completions for `gpt-*` / `o*` models (reasoning effort `low`), Bedrock Converse for everything else (temperature 0). Max 1,200 answer tokens. |
| Check | `answer.py` | Maps `[n]` to sources, flags citations to excerpts that don't exist, detects refusals. Skips the LLM call (and its cost) when nothing scores above `min_score`. |

The `Retriever` interface (`retrieve(query, filters, k) -> list[RetrievedChunk]`) is
contract 2 in the plan. `LocalLanceRetriever` and `BedrockKBRetriever` both implement it, so
the answer service doesn't change when we move to AWS. `BedrockKBRetriever` is written
against the Retrieve API but hasn't run against a live KB yet.

## Settings

`infrapilot_rag/config.py`, overridable by environment variable:

| Setting | Default | Env |
|---|---|---|
| Answer model | `gpt-5.4-mini` (OpenAI) | `INFRAPILOT_ANSWER_MODEL` |
| OpenAI key | none | `OPENAI_API_KEY` |
| Reasoning effort (gpt-5 family) | `low` | `INFRAPILOT_REASONING_EFFORT` |
| Chunks in prompt / candidates / max per page | 6 / 30 / 2 | |
| Minimum score to count as a hit | 0.30 (on-topic questions score 0.55–0.85, off-topic ≤ 0.21) | |
| Data directory | `data_collection/data` | `INFRAPILOT_DATA_DIR` |
| Knowledge Base id | none | `INFRAPILOT_KB_ID` |
| Rerank model | `none` (or `amazon.rerank-v1:0`, `cohere.rerank-v3-5:0`) | `INFRAPILOT_RERANK_MODEL` |

**Models:** answers come from OpenAI by default ([decision 002](../docs/decisions/002-answer-model.md)).
Claude on Bedrock needs an Anthropic use-case form we haven't submitted, and Claude 5.x isn't
offered to our account. **Nova Pro** (Bedrock, no key) remains as a fallback: it gets facts
and versions right but places citations less reliably.

Rough cost per answer (~3k input tokens): gpt-5-mini ≈ $0.001–0.003 (reasoning tokens bill
as output); Nova Pro ≈ $0.002–0.004.

## Tests

```bash
cd rag && ../data_collection/.venv/bin/pytest -q
```

Offline: a fake embedder and fake LLM, so no AWS or OpenAI calls. Covers retrieval ranking, filters,
per-page diversity, version-fact selection, a prompt snapshot (`tests/snapshots/prompt.txt`;
after an intended prompt change, rerun with `UPDATE_SNAPSHOTS=1`), citation parsing, refusals, and model routing.
