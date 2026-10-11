# Guardrail policy (source #59)

Plain-language security rules InfraPilot must follow when generating infrastructure,
written as Markdown (one rule per heading works best for retrieval). Ingested as
`doc_type=rule`. Example rule:

```markdown
## S3 buckets block public access
Every generated S3 bucket sets BlockPublicAccess to BLOCK_ALL, unless the request is a
static website served directly from S3 and the user confirmed that.
```

This README is not ingested.
