# 002: Answer model

- **Status:** accepted (2026-10-12); revisit after the Phase 1 RAG eval
- **Date:** 2026-10-12

## Decision

Generate answers with an **OpenAI model through the OpenAI API**, default `gpt-5-mini`
(reasoning effort `low`), with the key in `OPENAI_API_KEY`. Keep **Amazon Nova Pro on
Bedrock** as the no-key fallback. Embeddings and retrieval don't change: Titan V2 + LanceDB
now, Bedrock Knowledge Base + S3 Vectors later ([001](001-embedding-model.md)).

## Why

- **Claude on Bedrock is blocked.** Claude 4.x needs the Anthropic use-case form for our
  account, and Claude 5.x isn't offered to the account at all (checked 2026-10-12).
- **Nova Pro works but cites loosely.** In the first tests it got facts and versions right
  but sometimes bunched citations at the end of the answer instead of after each claim.
- **Only the embedding model is tied to the Knowledge Base.** The KB embeds documents and
  queries itself, so embeddings must stay on its supported list. The model that writes the
  answer just receives the retrieved text, so it can come from any provider.
- **Cheap:** gpt-5-mini is about $0.25 / $2.00 per 1M input/output tokens, so ~$0.001–0.003
  per answer at our ~3k-token prompts.

## Consequences

- A second vendor and an API key to manage. Locally: `.env` (git-ignored, template in
  `.env.example`). In AWS: **Secrets Manager**, read by the answer Lambda.
- Retrieved doc excerpts and user questions are sent to OpenAI. They're public docs, so this
  is acceptable; don't send user secrets or account data in questions.
- **AWS credits don't cover OpenAI usage.** Set a monthly usage limit in the OpenAI dashboard.
- The plan's Bedrock Claude references (Haiku for routing, Sonnet for generation) become
  "small/large answer model". The intent classifier (C9) and generators (C10) can use
  gpt-5-mini / gpt-5 the same way.

## Revisit if

- The RAG eval misses citation accuracy ≥ 0.9 or refusal accuracy ≥ 0.8 with gpt-5-mini:
  try `gpt-5` or a higher reasoning effort on the same eval set.
- Claude access on Bedrock is approved and the team prefers a single AWS bill.
