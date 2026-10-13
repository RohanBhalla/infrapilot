"""Decide the reply for one message: answer, ask for missing details, confirm, or mark ready.

The classifier extracts; this module decides, using the slot schemas in intents.py.
"""

from __future__ import annotations

import logging
import re

from .classifier import Classification
from .intents import ALIASES, INTENTS, LABELS, Intent, Slot, resolve_default
from .state import DialogState, Question, Turn

log = logging.getLogger(__name__)
CONFIRM_BELOW = 0.7  # ask "did you mean ...?" when intent confidence is lower than this
TRUE = {"true", "yes", "y", "1", "on"}
# Values models emit when they don't actually know; never accept them as an answer.
PLACEHOLDERS = {"unknown", "n/a", "na", "null", "not specified", "unspecified", "tbd", "?", "not provided",
                "not given", "none given"}
COST_WORDS = re.compile(r"cost|price|pric|bill|charge|money|pay|expens|\$|budget", re.I)
FALSE = {"false", "no", "n", "0", "off"}

OUT_OF_SCOPE = ("I can help with AWS infrastructure and CI/CD: answering questions about GitHub Actions, "
                "CloudFormation, CDK, ECS, EKS, Lambda, S3 static sites and Docker, and setting those up "
                "for you. What would you like to do?")


def parse_value(slot: Slot, raw: str):
    """Convert an extracted string to the slot's type, or None if it isn't valid."""
    v = str(raw).strip()
    if not v or v.lower() in PLACEHOLDERS or (slot.required and v.lower() == "none" and slot.type == "str"):
        return None
    if slot.type == "enum":
        v = ALIASES.get(v.lower(), v.lower())
        return v if v in slot.options else None
    if slot.type == "bool":
        return True if v.lower() in TRUE else False if v.lower() in FALSE else None
    if slot.type == "int":
        m = re.search(r"\d+", v)
        if not m:
            return None
        n = int(m.group())
        if (slot.min is not None and n < slot.min) or (slot.max is not None and n > slot.max):
            return None
        return n
    return v[:2000]


def _apply(intent: Intent, state: DialogState, extracted: dict[str, str], message: str) -> None:
    for name, raw in extracted.items():
        slot = intent.slot(name)
        if slot is None:
            continue
        value = parse_value(slot, raw)
        if value is not None and slot.consent and name not in state.asked and not COST_WORDS.search(message):
            continue  # consent (yes or no) must come from the user, not the model
        if value is not None:
            state.params[name] = value


def _missing(intent: Intent, state: DialogState) -> list[Slot]:
    return [s for s in intent.slots if s.required and s.name not in state.params]


def _resolved(intent: Intent, params: dict, versions) -> dict:
    out = dict(params)
    for s in intent.slots:
        if s.name not in out:
            d = resolve_default(s, out, versions)
            if d is not None:
                out[s.name] = d
    return out


def _reset(state: DialogState, **fields) -> None:
    for k, v in DialogState(**fields):
        setattr(state, k, v)


def _fmt(value) -> str:
    return "yes" if value is True else "no" if value is False else str(value)


def step(message: str, state: DialogState, classification: Classification, versions=None) -> Turn:
    """One turn. `state` is updated in place and also returned on the Turn."""
    c = classification
    intent = INTENTS[c.intent]
    base = {"intent": c.intent, "confidence": c.confidence, "standalone_question": c.standalone_question,
            "cost_usd": c.cost_usd}

    if intent.kind != "task" and state.awaiting_confirmation:
        _reset(state)  # the user didn't confirm the task; drop it

    if intent.kind == "other":
        return Turn(type="out_of_scope", message=OUT_OF_SCOPE, state=state, **base)

    if intent.kind == "answer":
        error_text = None
        if intent.name == "explain_failure":
            error_text = c.params.get("error_text") or (
                message if len(message) > 120 or "\n" in message else None)
            if not error_text:
                slot = intent.slot("error_text")
                return Turn(type="clarification", message=slot.question, state=state,
                            questions=[Question(slot=slot.name, question=slot.question)], **base)
        # Questions don't touch an unfinished task, so the user can come back to it.
        return Turn(type="answer", error_text=error_text, state=state, **base)

    # A task. A different task than the one in progress starts fresh; carrying on with the
    # same task (answering our questions, changing a detail) counts as confirmed.
    continuing = state.intent == intent.name and state.status in ("collecting", "ready")
    if state.intent != intent.name:
        _reset(state, intent=intent.name)
    if state.awaiting_confirmation:
        state.awaiting_confirmation, state.confirmed = False, True  # same intent again means "yes"
    _apply(intent, state, c.params, message)

    if c.confidence < CONFIRM_BELOW and not (state.confirmed or continuing):
        state.awaiting_confirmation, state.status = True, "collecting"
        return Turn(type="confirmation", state=state, params=dict(state.params),
                    message=f"Just to check: do you want me to set up {LABELS[intent.name]}?", **base)

    if intent.name == "eks_cluster" and state.params.get("cost_ack") is False:
        state.status = "cancelled"
        return Turn(type="out_of_scope", state=state, message="OK, I won't create an EKS cluster. "
                    "I can still answer questions about EKS or set up something that isn't billed by the hour.",
                    **base)

    state.status = "collecting"
    missing = _missing(intent, state)
    if missing:
        state.asked = [s.name for s in missing]
        lines = [f"To set up {LABELS[intent.name]}, I need to know:"] + [f"- {s.question}" for s in missing]
        if not state.defaults_shown:
            defaults = _resolved(intent, state.params, versions)
            shown = [f"{s.name.replace('_', ' ')}: {_fmt(defaults[s.name])}" for s in intent.slots
                     if not s.required and s.name not in state.params and s.name in defaults]
            if shown:
                lines.append("\nUnless you say otherwise I'll use: " + "; ".join(shown) + ".")
            state.defaults_shown = True
        return Turn(type="clarification", state=state, params=dict(state.params), message="\n".join(lines),
                    questions=[Question(slot=s.name, question=s.question, options=list(s.options))
                               for s in missing], **base)

    state.asked, state.status = [], "ready"
    params = _resolved(intent, state.params, versions)
    summary = "\n".join(f"- {k.replace('_', ' ')}: {_fmt(v)}" for k, v in params.items())
    return Turn(type="ready", state=state, params=params, **base,
                message=f"Here's what I'll set up for {LABELS[intent.name]}:\n{summary}\n\n"
                        "Tell me if you want to change anything. (Generating the files is the next "
                        "InfraPilot feature.)")


class Assistant:
    """Classifier + dialog, the unit the Chat API calls."""

    def __init__(self, classifier, versions=None):
        self.classifier, self.versions = classifier, versions

    def handle(self, message: str, state: DialogState, history=()) -> Turn:
        try:
            c = self.classifier.classify(message, state, history)
        except Exception as exc:  # classifier down: still answer it as a plain question
            log.warning("classifier failed (%s); treating the message as a question", exc)
            c = Classification("qa", 0.0, message)
        return step(message, state, c, self.versions)
