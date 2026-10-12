# 003: Reranking search results

- **Status:** accepted (2026-10-12)

## Decision

After hybrid search (vector + keyword, fused with RRF), rerank the top 50 candidates with
**Cohere Rerank 3.5 on Bedrock** (`cohere.rerank-v3-5:0`, us-east-1) through the Rerank API, then
keep at most 2 chunks per page and the top 6. `INFRAPILOT_RERANK_MODEL=amazon.rerank-v1:0`
switches to Amazon Rerank 1.0; `none` turns reranking off. If a rerank call fails, the search
order is used.

## Why

On the 59 answerable eval questions (page-level, same candidates):

| Setting | hit@1 | hit@5 | MRR | Rerank time (p50) |
|---|---:|---:|---:|---:|
| Hybrid, no rerank | 0.644 | 0.966 | 0.762 | – |
| Cohere Rerank 3.5, top 50 | 0.644 | 0.983 | 0.791 | 0.52 s |
| Amazon Rerank 1.0, top 50 | 0.644 | 1.000 | 0.797 | 1.39 s |

The two models differ by one question, which is noise at this size. Cohere was picked because
it runs in **us-east-1** (Amazon Rerank is only in us-west-2) and is ~2.5x faster. A Bedrock
Knowledge Base can only rerank with a model in its own region, so this keeps local and cloud
the same.

Full eval with reranking (gpt-5.4-mini): hit@5 0.949 → 0.983, citation accuracy 0.949 → 0.983,
key-fact recall 0.887 → 0.991, refusal accuracy 0.90 → 1.00, no false refusals.

## Cost

About **$2 per 1,000 questions** for Cohere Rerank 3.5 ($1 for Amazon Rerank). Estimates; check
Bedrock pricing. Cohere is a third-party model billed through AWS Marketplace, so some
promotional credits may not cover it (see [001](001-embedding-model.md)); Amazon Rerank is
first-party. A full eval run now costs ~$0.39 instead of ~$0.23.

## Revisit if

- Our credits don't cover Marketplace models: switch to Amazon Rerank (cross-region call).
- The eval set grows and the two models separate by more than a couple of questions.
