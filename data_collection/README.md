# InfraPilot data collection

Collects the documentation corpus for InfraPilot's RAG pipeline and the tool version
registry. This is Phase 1, Feature 1.1 of [the development plan](../docs/DEVELOPMENT_PLAN.md).
It runs locally first; the same stages move to Lambda + Step Functions in Phase 2.

```
sources (sheet + additions) ─► fetch ─► data/raw/            immutable snapshots, one JSON per page
                                    └─► data/versions/       version registry (→ DynamoDB later)
data/raw ─► process ─► data/normalized/                      clean Markdown + metadata per doc
                   ├─► data/chunks/chunks.jsonl              heading-aware chunks (local retrieval)
                   ├─► data/kb_export/                       Bedrock KB layout (→ S3 later)
                   └─► data/reports/report.md                corpus stats, drops, cost estimate
data/chunks ─► embed --run ─► data/vectors/ (LanceDB)        Titan V2 vectors for local testing
```

## Quick start

```bash
cd data_collection
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # add ,embed for the embedding step

export GITHUB_TOKEN=$(gh auth token)       # raises the GitHub API limit from 60 to 5,000 req/h

.venv/bin/infrapilot-data list                         # every source and how it is collected
.venv/bin/infrapilot-data fetch --max-pages 3          # quick smoke test of every source
.venv/bin/infrapilot-data fetch                        # full fetch (~20 min, polite rate limits)
.venv/bin/infrapilot-data process                      # normalize, filter, dedupe, chunk, export
open data/reports/report.md
```

Useful filters for `fetch`: `--source 1,2,37`, `--category "GitHub Actions"`,
`--strategy sitemap`, `--no-cache`. Re-running `fetch` sends conditional requests
(ETag / Last-Modified), so unchanged pages come back as cheap 304s.

### Embeddings (optional, costs cents)

```bash
.venv/bin/pip install -e ".[embed]"
.venv/bin/infrapilot-data embed                        # dry run: tokens and USD estimate
.venv/bin/infrapilot-data embed --run --limit 200      # needs AWS creds + Titan V2 model access
.venv/bin/infrapilot-data search "pin actions/checkout version" --category "CI/CD – GitHub Actions"
```

Embeddings are cached by chunk content hash, so re-runs only embed changed chunks. See
[the embedding decision](../docs/decisions/001-embedding-model.md) for why Titan V2.

## Sources

| File | What it is |
|---|---|
| `sources/sources.csv` | Snapshot of the team's Google Sheet. Refresh with `infrapilot-data sync-sources`. |
| `sources/additional_sources.csv` | Proposed additions (#60+) that fill gaps in the sheet. Copy them into the sheet once agreed. |
| `sources/source_config.yaml` | The machine-readable version of each row's "Filter / Notes": strategy, URL filters, page caps, doc type. |
| `uploads/` | Team-authored golden templates (#58) and guardrail policy (#59). |

Every row in either CSV needs an entry in `source_config.yaml`; the tests enforce this.
To add a source: add the row to the sheet, sync, add its rule, and run
`fetch --source <id> --max-pages 3` to check it.

## Dataset preparation practices used here

**Collection**
- *Markdown first.* Most sites serve a Markdown rendition (AWS docs `.md`, docs.github.com
  article API, Docker docs `.md`, raw GitHub files). That keeps headings, tables, and code
  blocks intact. HTML is the fallback, extracted with trafilatura.
- *Polite crawling.* robots.txt is honored (that's why `nodejs.org/dist/` is replaced by
  endoflife.date), there is a 1 req/s per-host limit, an identifying User-Agent, retries with
  backoff on 429/5xx, and conditional GETs.
- *Scoped, capped crawls.* Each source has include/exclude URL rules and a `max_pages`
  cap. Capped URLs are listed in the fetch log so nothing is dropped silently.
- *Immutable raw layer.* Raw snapshots are kept separately from processed output, so
  processing can be changed and re-run without re-downloading. A failed fetch keeps the
  previous snapshot instead of wiping it.
- *Provenance on everything.* Every doc and chunk carries source id, URL, category, doc
  type, and fetch time. Answers can cite URLs, and stale content is easy to find.

**Cleaning and filtering**
- Strip link URLs (the link text stays), images, HTML anchors and wrappers, front matter,
  and Liquid/Hugo template tags. Code blocks are never modified, so `${{ github.sha }}`
  and YAML stay exact.
- Remove per-source boilerplate: lines that appear in at least 50% of a source's docs.
- Drop navigation/index pages (mostly short list items) and near-empty pages.
- Deduplicate by URL, exact content hash, and near-duplicate SimHash (Hamming ≤ 3).
  When duplicates collide, the earlier source in the sheet wins, so primary references
  beat mirrors. Every drop is logged in `data/reports/dedup.json` with its reason.

**Chunking**
- Split on headings first, then pack paragraphs to ~450 tokens (max 700) with ~60 tokens
  of overlap. Code blocks are never cut unless one block alone exceeds the max; then the
  fence is reopened on each piece.
- Tiny sections are merged forward so a heading with one sentence doesn't become a chunk.
- Each chunk starts with a context header (document title, section path, category), which
  improves retrieval for chunks that don't repeat their topic.
- Chunks are pre-built here and exported one file per chunk with a `.metadata.json`
  sidecar. The Bedrock KB uses chunking strategy `NONE`, so local and cloud retrieval run
  on identical chunks and eval results are comparable.
- Sidecar metadata stays under the ~2 KB filterable-metadata limit of S3 Vectors.

**Versions**
- Version facts (latest action tags, Node LTS/EOL, Lambda runtimes, Kubernetes/EKS
  support) go to `data/versions/versions.json`, the local stand-in for the DynamoDB
  registry. Each version source also writes a short Markdown summary into the corpus,
  so answers can cite it.

## Tests

```bash
.venv/bin/pytest -q
```

Offline tests cover source config coverage, URL rewriting, cleaning, boilerplate
removal, navigation filtering, chunk boundaries and code fences, and dedup.
