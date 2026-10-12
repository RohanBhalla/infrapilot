# InfraPilot evals

RAG evaluation harness (plan, Feature 1.3). Questions live in `rag/questions.jsonl`, reports in `reports/`.

```bash
cd data_collection
.venv/bin/pip install -e ../eval
.venv/bin/infrapilot-eval check                  # free: validate questions against the corpus
.venv/bin/infrapilot-eval run --retrieval-only   # ~free: hit@k / MRR only
.venv/bin/infrapilot-eval run                    # full run, default model (~$0.25)
.venv/bin/infrapilot-eval run --model gpt-5.6-luna --tag luna
.venv/bin/infrapilot-eval run --judge            # add LLM-judge scores (extra cost)
.venv/bin/infrapilot-eval rescore ../eval/reports/<report>.json   # free: re-grade after fixing labels
```

Run it before and after changes to chunking, retrieval, prompts or models, not on every commit.

## Questions

One JSON object per line, three kinds:

```json
{"id": "cfn-retain", "kind": "answer", "category": "IaC – CloudFormation",
 "question": "How do I keep an S3 bucket when its stack is deleted?",
 "expected_urls": ["https://docs.aws.amazon.com/.../aws-attribute-deletionpolicy.html"],
 "key_facts": ["Retain"]}
{"id": "ver-node-lts", "kind": "version", "question": "What is the current Node.js LTS?",
 "version": {"tool": "nodejs", "field": "lts"}}
{"id": "ref-pulumi", "kind": "refuse", "question": "How do I configure a Pulumi stack?"}
```

- **answer:** `expected_urls` are every page that properly answers the question. Find them by
  searching titles and text, not with `infrapilot-ask`, so search isn't graded against itself.
  `key_facts` are short strings a correct answer must contain (case- and hyphen-insensitive).
- **version:** checked against the version registry at run time, so answers stay graded
  against the current release. `field`: `major`, `lts`, `latest`, `latest_minor`,
  `runtime_id` or `status`.
- **refuse:** topics the corpus doesn't cover. Check with `grep -ril` that they really don't appear.

`check` fails if an expected URL isn't in the corpus, a key fact isn't in any expected page, or a
version question can't be resolved.

## Metrics

| Metric | Meaning | Target |
|---|---|---|
| hit@5 / MRR@10 | An expected page is in the top 5 search results / how high it ranks | ≥ 0.80 |
| citation_accuracy | Answer cites at least one expected page and no non-existent excerpt | ≥ 0.90 |
| citation_coverage | Share of factual lines that carry a citation | |
| key_fact_recall | Share of key facts present in the answer | |
| version_correctness | Version answers match the registry | 1.00 |
| refusal_accuracy | Out-of-scope questions get the refusal | ≥ 0.80 |
| false_refusals | Answerable questions that were refused | |
| stale_pins | `owner/action@vN` pins older than the registry, across all answers | 0 |
| judge_* (optional) | LLM judge, 1–5: faithfulness to excerpts, correctness | |
