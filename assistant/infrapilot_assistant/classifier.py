"""One structured-output model call per message: intent, confidence, extracted details, and the
message rewritten as a standalone question (used for search when the intent is a question)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from infrapilot_rag.llm import cost

from .intents import ALL_SLOTS, INTENTS
from .state import DialogState

log = logging.getLogger(__name__)
HISTORY_TURNS = 6


@dataclass
class Classification:
    intent: str
    confidence: float
    standalone_question: str
    params: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0


SCHEMA = {
    "name": "classification",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "confidence", "standalone_question", "params"],
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "confidence": {"type": "number", "description": "0 to 1"},
            "standalone_question": {"type": "string"},
            "params": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["slot", "value"],
                "properties": {"slot": {"type": "string", "enum": ALL_SLOTS}, "value": {"type": "string"}},
            }},
        },
    },
}


def _catalogue() -> str:
    lines = []
    for i in INTENTS.values():
        lines.append(f"- {i.name}: {i.description}")
        for s in i.slots:
            opts = f" (one of: {', '.join(s.options)})" if s.options else f" ({s.type})"
            lines.append(f"    - {s.name}{opts}: {s.description}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You route messages for InfraPilot, an assistant that answers questions about AWS \
infrastructure and CI/CD and generates infrastructure (workflows, Dockerfiles, CDK apps).

Intents and the details (slots) each one uses:
{_catalogue()}

Rules:
1. Pick the intent of the latest message, using the conversation and state for context.
2. If there is a current task and the message answers the assistant's question or adds or changes \
details of that task, keep the task's intent.
3. Questions about how something works or how to do something are qa, even when they are about \
deploying. Use a task intent only when the user asks InfraPilot to create, set up, generate, write \
or deploy something for them.
4. A pasted error or log, or a report that something failed, is explain_failure. Put the key error \
lines in error_text, copied exactly (at most about 1500 characters). error_text is only for actual \
error output (messages, codes, log lines). If the user just says something failed without showing \
the error, leave error_text out.
5. Extract every value the user stated or clearly implied in the latest message ("my React app" means \
framework=react, "512 MB" means memory_mb=512), even when other details are still missing. \
Never fill in defaults or guess. Use the listed option values exactly, true/false for yes/no, and \
digits for numbers. Only use slots that belong to the chosen intent.
6. confidence (0 to 1): how sure you are about the intent.
7. standalone_question: the latest message rewritten so it makes sense without the conversation. \
Keep tool names, versions, error messages and code identifiers exactly as written. If it is already \
standalone, copy it."""


def _context(state: DialogState, history) -> str:
    parts = []
    if history:
        convo = "\n".join(f"{m.role}: {m.content[:600]}" for m in history[-HISTORY_TURNS:])
        parts.append(f"<conversation>\n{convo}\n</conversation>")
    if state.intent:
        parts.append(f"<state>current task: {state.intent}; known details: {json.dumps(state.params)}; "
                     f"assistant last asked about: {', '.join(state.asked) or 'nothing'}; "
                     f"waiting for the user to confirm the task: {state.awaiting_confirmation}</state>")
    return "\n\n".join(parts)


class Classifier:
    def __init__(self, llm):
        self.llm = llm

    def classify(self, message: str, state: DialogState, history=()) -> Classification:
        user = f"{_context(state, list(history))}\n\nLatest message: {message}".strip()
        text, usage = self.llm.complete(SYSTEM_PROMPT, user, json_schema=SCHEMA)
        spent = cost(self.llm.model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        try:
            data = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except ValueError:
            log.warning("classifier returned non-JSON; treating as a question: %r", text[:200])
            return Classification("qa", 0.0, message, cost_usd=spent)
        intent = data.get("intent") if data.get("intent") in INTENTS else "qa"
        params = {p["slot"]: str(p["value"]) for p in data.get("params", [])
                  if isinstance(p, dict) and p.get("slot") and p.get("value") not in (None, "")}
        return Classification(intent, float(data.get("confidence", 0.0)),
                              (data.get("standalone_question") or message).strip(), params, spent)
