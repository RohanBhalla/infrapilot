# InfraPilot assistant: intent + clarification

Component C9 of the [development plan](../docs/DEVELOPMENT_PLAN.md). For each chat message it works
out what the user wants and, for tasks, whether there's enough detail to build it.

```
message ──► classifier (1 structured model call) ──► dialog rules (plain code) ──► reply type
            intent, confidence, details,                merge + validate details     answer | clarification |
            standalone question                         ask only for what's missing  confirmation | ready |
                                                                                     out_of_scope
```

| File | What it does |
|---|---|
| `intents.py` | The 8 intents and each task's details (slots): type, allowed values, required or not, default, question to ask. Defaults like the Node version or Lambda runtime come from the version registry. |
| `classifier.py` | One OpenAI call with structured output (JSON schema). Returns intent, confidence, extracted details and the message rewritten as a standalone question (used for search, replacing the old follow-up rewrite). |
| `dialog.py` | Decides the reply. Questions go to RAG; tasks ask for missing **required** details in one round and state the defaults; low confidence on a new task asks for confirmation; all details known means **ready**. |
| `state.py` | `DialogState` (saved with the chat session) and `Turn` (one reply). |

Rules enforced in code, not left to the model:
- Values are type-checked and must be allowed options; placeholders like "unknown" are rejected.
- **Consent slots** (EKS hourly cost) count only if InfraPilot asked or the user's own words mention cost.
- A question mid-task is answered without losing the task; a new task starts fresh; confirming means
  repeating the same task; declining drops it.
- If the classifier call fails, the message is treated as a plain question.

Cost: about **$0.0014 per message** (gpt-5.4-mini), ~1.2 s median. When a task is ready, the
Chat API runs its generator (below) and returns the files with the reply.

## Generators (C10)

`generators/` turns a ready task's details into a `Plan` (`plan.py`, plan contract 5): files,
a summary, one-time setup steps, notes, checks and sources. So far: **GitHub Actions**
(`cicd_github_actions`), **static site** (`deploy_static_site`), **Lambda** (`deploy_lambda`) and
**Dockerfile** (`containerize`). Other tasks (EKS, or a language a generator doesn't cover) reply that
file generation isn't available yet.

- **Deterministic, no model call.** The workflow is built from tested blocks per language (Node
  npm/yarn/pnpm, Python pip/poetry, Java Maven/Gradle, Go) and deploy target (none, S3 + CloudFront,
  Lambda, ECS). The same details always give the same file.
- **Current versions.** Every `uses:` comes from the version registry. Eight actions the registry
  doesn't track yet (setup-python, setup-java, setup-go, download-artifact, pnpm/action-setup and
  Docker's setup-qemu, setup-buildx and build-push) use `FALLBACK_PINS` in `pins.py`; add those repos
  to the sources sheet so they're refreshed too.
- **Safe defaults.** Read-only token by default; deploy jobs sign in with OIDC (no stored AWS
  keys), run only on pushes/manual runs of the main branch, and use a `production` environment.
- **Checks.** `version-pins` (every action on its current major) runs everywhere; the linters
  run as described under [Validation](#validation-c11).
- Free-text steps ("lint and unit tests") and triggers ("nightly", "PRs to develop") are read
  with keyword rules; anything not used is listed in the notes.

### Static site

`static_site.py` writes `infra/static-site.yaml`, a CloudFormation template:

- **Private S3 bucket** (all public access blocked, encrypted, HTTPS only). CloudFront reads it
  through Origin Access Control. Versioning keeps old files for 30 days, so a bad upload can be
  rolled back.
- **CloudFront** with HTTPS redirect, the AWS managed CachingOptimized and SecurityHeaders policies,
  and the cheapest price class.
  - Single-page apps: unknown paths load `index.html`.
  - Custom domain: an ACM certificate (from us-east-1) and TLS 1.2 minimum.
- **GitHub deploy role** (when a CI workflow is wanted and a repo is given). It trusts only that
  repo's `production` environment and can only upload to this bucket, clear this distribution's
  cache and read this stack.

With CI, it also writes `.github/workflows/deploy-site.yml`. On each push to main it builds the site
with npm (not for plain HTML), finds the bucket from the stack outputs, uploads the files and clears
the cache.

Four checkov rules are skipped in the template, each with its reason. Three add cost a small site
usually doesn't need: WAF, the S3 access-log bucket and CloudFront access logs. The fourth (a
minimum TLS version) can't be set on the default `*.cloudfront.net` certificate.

### Lambda

`lambda_function.py` writes `infra/lambda.yaml`, a CloudFormation template:

- **The function** on arm64 (about 20% cheaper), with the runtime, handler, memory and timeout from
  the chat. It starts with inline placeholder code; the workflow ships the real code, so pushes
  never need a stack update.
- **Runtime** from what the user said: "python", "Python 3.13", "node 22", "nodejs24.x". With no
  version, it's the Node LTS or the newest Python that Lambda supports (from the version registry).
  Deprecated or unknown runtimes are replaced, with a note. Only Node and Python for now.
- **Its own role** that can only write to its **log group** (kept 30 days).
- **One trigger:**
  - Function URL: public (`AuthType: NONE`), with both permissions AWS now requires.
  - API Gateway: an HTTP API (quick create, one catch-all route).
  - Schedule: EventBridge Scheduler with its own invoke-only role. Rate expressions are fixed up
    ("rate(1 hours)" becomes "rate(1 hour)"); anything unusable falls back to hourly, with a note.
  - S3 uploads: a private bucket where new files under `uploads/` run the function. The role can
    read `uploads/` and write `processed/`, so results don't trigger the function again.
  - Manual: nothing.
- **GitHub deploy role** (same pattern as the static site): trusts only the repo's `production`
  environment and can only update this function's code.

With CI, it also writes `.github/workflows/deploy-lambda.yml`. On each push to main it zips the code
folder (with production dependencies; Python wheels are fetched for arm64 Linux), finds the function
from the stack outputs and runs `aws lambda update-function-code`.

Five checkov rules are skipped where they apply, each with its reason: VPC (needs a NAT gateway to
reach the internet), dead-letter queue, reserved concurrency, a KMS key for logs, and S3 access logs.
The public function URL check is skipped only for that trigger, since that's what was asked for.

### Dockerfile

`container.py` writes a `Dockerfile` and `.dockerignore` for Node, Python, Java (Maven) or Go:

- **Two stages:** dependencies and builds happen in the first, so the final image only has what
  runs. Java and Go build on the runner's own platform (no emulation); Go ends up in a 3 MB
  distroless image.
- **Current official images** from the version registry (Node LTS, newest Python and Java that AWS
  supports; Go from a dated fallback), pulled from the ECR Public mirror so CI doesn't hit Docker
  Hub's pull limits.
- **Non-root** (numeric user ids) and a **health check** that the app's port accepts connections,
  which works whatever the app's routes are. Go's distroless image has no shell, so it has none.
- **Exec-form `CMD`**, so the app gets stop signals; commands with shell syntax go through `sh -c`.

With ECR (the default), `infra/container.yaml` adds a private repository (immutable tags, scan on
push, keeps 10 images) and `.github/workflows/deploy-container.yml` builds for arm64 and pushes on
each push to main. With ECS, the template also creates a small VPC (two public subnets, no NAT
gateway), a Fargate service on arm64 (0.25 vCPU, 0.5 GB) with a circuit breaker, a 30-day log group
and least-privilege roles. The service starts with 0 tasks; the workflow sets the stack's `ImageTag`
parameter to the new commit, which starts or replaces the task. There's no load balancer, so the
task is reached on its public IP.

Four checkov rules are skipped where they apply, each with its reason: KMS keys for ECR and logs,
Container Insights (billed per metric), and the Docker health check for Go's distroless image.

All four sample apps (one per language) were built and run from the generated Dockerfiles; each
answered on its port and became healthy.

## Validation (C11)

`generators/validate.py` runs each linter on the generated files it understands:

| Linter | Files |
|---|---|
| actionlint | `.github/workflows/*.yml` |
| hadolint | `Dockerfile`, `Dockerfile.*`, `*.Dockerfile` |
| cfn-lint | CloudFormation templates (`AWSTemplateFormatVersion` or `*.template.*`) |
| checkov | all of the above (GitHub Actions, Dockerfile and CloudFormation checks) |

A linter that isn't installed reports "skipped", never "fail". Locally that's usually checkov,
hadolint and cfn-lint. On AWS the chat Lambda doesn't carry them. It sends the files to the
validator Lambda, a container image with all four (`validator/Dockerfile`), named by
`INFRAPILOT_VALIDATOR_FUNCTION`. If that call fails or takes over 12 s, the checks report
"skipped" and the reply still goes out.

There's no retry loop. The generators are templates, so a failed check is a template bug, and the
tests catch it. To run every generated workflow through all four linters:

```bash
docker build --platform linux/arm64 -t infrapilot-validator -f validator/Dockerfile .
docker run --rm -p 9000:8080 infrapilot-validator
INFRAPILOT_VALIDATOR_URL=http://localhost:9000/2015-03-31/functions/function/invocations \
  ../data_collection/.venv/bin/pytest -q tests/test_validate.py
```

## Eval

`eval/intent/utterances.jsonl` has 120 labeled messages (single-turn, multi-turn with saved state,
ambiguous ones with several acceptable answers). Run `infrapilot-eval intent` (~$0.17).

| Metric | Result | Target |
|---|---:|---:|
| Intent accuracy | 1.000 | ≥ 0.90 |
| Action accuracy (asked when needed, not otherwise) | 1.000 | ≥ 0.85 |
| Slot F1 (precision 0.954, recall 1.000) | 0.977 | ≥ 0.85 |

The labels and the rules were written by the same person and tuned on this set, so expect lower
scores on new messages. More cases from teammates, especially ambiguous ones, are the next step.

## Tests

```bash
cd assistant && ../data_collection/.venv/bin/pytest -q
```

The generator tests build every combination: 36 GitHub Actions workflows (language x package
manager x deploy target), 48 static site plans (framework x SPA x domain x CI) and 20 Lambda plans
(trigger x runtime x CI) and 16 Dockerfile plans (language x ECR x ECS). They parse each
file and run actionlint and cfn-lint (`pip install -e "assistant[dev]"` installs both). Checkov
runs only against the validator image (see Validation).
