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

Cost: about **$0.0014 per message** (gpt-5.4-mini), ~1.2 s median. Generating the files for a
"ready" task is the next component (C10).

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
