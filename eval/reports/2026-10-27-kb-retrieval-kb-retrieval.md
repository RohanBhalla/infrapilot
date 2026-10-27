# RAG eval: 2026-10-27 · kb · retrieval only

Judge: off · retrieval {'k': 6, 'candidates': 30, 'max_per_doc': 2, 'min_score': 0.3, 'hybrid': None, 'rerank': None} · 7.1 s

| Metric | Value | Target |
|---|---:|---:|
| questions | 79 |  |
| hit@1 | 0.661 |  |
| hit@5 | 0.881 ✅ | 0.800 |
| mrr@10 | 0.751 |  |
| total_cost_usd | 0.000 |  |

## hit@5 by category

| Category | hit@5 | n |
|---|---:|---:|
| CI/CD – AWS | 0.83 | 6 |
| CI/CD – GitHub Actions | 1.00 | 14 |
| Containers | 1.00 | 3 |
| Deploy targets | 0.93 | 14 |
| Deploy targets (stretch) | 1.00 | 4 |
| IaC – CDK | 0.71 | 7 |
| IaC – CloudFormation | 0.75 | 8 |
| Security | 0.67 | 3 |

## Failures (7)

| Question | Problem |
|---|---|
| cp-github-source | retrieval miss (rank None) |
| cfn-retain | retrieval miss (rank 7) |
| cfn-sub | retrieval miss (rank None) |
| cdk-nodejs-function | retrieval miss (rank 6) |
| cdk-s3-deploy | retrieval miss (rank None) |
| cf-invalidate | retrieval miss (rank 10) |
| sec-checkov-gha | retrieval miss (rank None) |
