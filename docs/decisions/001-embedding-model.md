# 001: Embedding model and credits

- **Status:** proposed (confirm with the Phase 1 retrieval eval)
- **Date:** 2026-10-09

## Decision

Use **Amazon Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`) on Amazon Bedrock,
1024 dimensions, normalized vectors. Use it both locally (LanceDB) and in the cloud
(Bedrock Knowledge Base on S3 Vectors). No OpenAI or Gemini credits are needed.

## Why

| Model | Price per 1M input tokens | Notes |
|---|---:|---|
| **Titan Text Embeddings V2 (Bedrock)** | **$0.02** | First-party AWS model. Native Bedrock KB embedding model. 256/512/1024 dims, 8K-token input. |
| Cohere Embed v4 (Bedrock) | $0.12 | 6x the price. Multimodal, which we don't need. Billed as a third-party model. |
| OpenAI text-embedding-3-small | $0.02 ($0.01 batch) | Same price, but a second vendor and key, data leaves AWS, and Bedrock KB can't use it, so we'd run our own vector pipeline. |
| Gemini embedding-001 | $0.15 | 7.5x the price; also outside AWS. |

Prices are from AWS's Titan V2 launch post and third-party trackers (Aug–Sep 2026). Check
the Bedrock pricing page for `us-east-1` before relying on them.

**Cost for our corpus:** the first full collection produced **~3.15M tokens** (1,438 docs, 11,271 chunks), so one
full embed costs about **$0.06**. After that, content hashing and KB incremental
sync re-embed only changed chunks, typically a few percent per week. Embedding cost is
not a concern; LLM tokens and hourly-billed infrastructure are.

**Vector store:** Bedrock Knowledge Bases have no separate fee; we pay for embeddings and
the vector store. S3 Vectors (GA since Dec 2025) is pay-per-use: storage, PUTs, and
queries come to cents at our scale. Do **not** pick OpenSearch Serverless; it has an
always-on minimum of roughly $350/month (2 OCUs).

## AWS credits

- **Titan V2 is a first-party AWS service**, so standard AWS promotional credits cover it
  like any other AWS usage.
- **Third-party models on Bedrock** (Anthropic Claude, Cohere, Meta, Mistral) are billed
  through AWS Marketplace. AWS Activate terms explicitly let credits cover this
  "Bedrock 3P Model Spend". Other promotional credit programs may exclude Marketplace
  charges. **Check the eligible-services list for our specific credits** in the Billing
  console (Credits page). This matters more for the Claude LLM calls than for embeddings.
- **AWS Academy Learner Lab:** if the course uses a Learner Lab account, Bedrock model
  access may be restricted. I couldn't confirm what Learner Lab allows. Ask the instructor
  before planning around it.
- Credits don't raise service quotas. Bedrock model access still has to be enabled per
  model in `us-east-1`.

## Revisit if

- The retrieval eval misses targets (hit@5 < 0.8) after chunking/prompt tuning. Then A/B
  test Cohere Embed v4 or Amazon Rerank (about $1 per 1,000 queries) on the same eval set.
  The pipeline only needs `EmbeddingConfig.model_id` changed and a re-embed.
- Corpus grows past ~10M chunks. Then consider 512 dims to halve vector storage.

## Sources

- [Titan Text Embeddings V2 launch post (AWS ML blog)](https://aws.amazon.com/blogs/machine-learning/get-started-with-amazon-titan-text-embeddings-v2-a-new-state-of-the-art-embeddings-model-on-amazon-bedrock/)
- [Amazon Titan Text Embeddings models (Bedrock docs)](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)
- [Cohere Embed v4 on Bedrock pricing](https://futureagi.com/llm-cost-calculator/bedrock/cohere-embed-v4-0/)
- [OpenAI embedding pricing guide](https://www.layer3labs.io/guides/openai-embedding-models-pricing)
- [Gemini vs OpenAI embedding pricing](https://tokencost.app/embeddings)
- [S3 Vectors GA announcement](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-vectors-generally-available/)
- [Using S3 Vectors with Bedrock Knowledge Bases](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html)
- [Bedrock pricing and the OpenSearch Serverless minimum](https://cloudburn.io/blog/amazon-bedrock-pricing)
- [AWS Activate terms (Bedrock 3P model spend)](https://aws.amazon.com/activate/terms)
- [AWS for Startups: credits on Bedrock third-party models](https://aws.amazon.com/startups/)
