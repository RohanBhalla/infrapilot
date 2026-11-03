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
(`cicd_github_actions`). Other tasks reply that file generation isn't available yet.

- **Deterministic, no model call.** The workflow is built from tested blocks per language (Node
  npm/yarn/pnpm, Python pip/poetry, Java Maven/Gradle, Go) and deploy target (none, S3 + CloudFront,
  Lambda, ECS). The same details always give the same file.
- **Current versions.** Every `uses:` comes from the version registry. Five actions the registry
  doesn't track yet (setup-python, setup-java, setup-go, download-artifact, pnpm/action-setup) use
  `FALLBACK_PINS` in `pins.py`; add those repos to the sources sheet so they're refreshed too.
- **Safe defaults.** Read-only token by default; deploy jobs sign in with OIDC (no stored AWS
  keys), run only on pushes/manual runs of the main branch, and use a `production` environment.
- **Checks.** `version-pins` (every action on its current major) runs everywhere. `actionlint`
  runs when installed (locally, and in the tests); on Lambda it reports "skipped" until the
  validation loop (C11) bundles the linters.
- Free-text steps ("lint and unit tests") and triggers ("nightly", "PRs to develop") are read
  with keyword rules; anything not used is listed in the notes.

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

The generator tests build every language x package manager x deploy target (36 workflows), parse
them and run actionlint (`pip install -e "assistant[dev]"` installs it).
