# RAG eval: 2026-10-12 · local · retrieval only

Judge: off · retrieval {'k': 6, 'candidates': 30, 'max_per_doc': 2, 'min_score': 0.3} · 3.2 s

| Metric | Value | Target |
|---|---:|---:|
| questions | 79 |  |
| hit@1 | 0.627 |  |
| hit@5 | 0.949 ✅ | 0.800 |
| mrr@10 | 0.753 |  |

## hit@5 by category

| Category | hit@5 | n |
|---|---:|---:|
| CI/CD – AWS | 1.00 | 6 |
| CI/CD – GitHub Actions | 1.00 | 14 |
| Containers | 1.00 | 3 |
| Deploy targets | 1.00 | 14 |
| Deploy targets (stretch) | 1.00 | 4 |
| IaC – CDK | 0.86 | 7 |
| IaC – CloudFormation | 0.88 | 8 |
| Security | 0.67 | 3 |

## Failures (3)

| Question | Problem |
|---|---|
| cfn-sub | retrieval miss (rank 8) |
| cdk-s3-deploy | retrieval miss (rank None) |
| sec-checkov-gha | retrieval miss (rank None) |
