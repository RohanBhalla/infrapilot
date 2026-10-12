"""Turn a follow-up ("and for Fargate?") into a standalone question before searching.

Only runs when the session has history, so first messages cost nothing extra. If the rewrite
fails, the original message is used.
"""

from __future__ import annotations

import logging

from infrapilot_rag.llm import cost

from .schemas import Message

log = logging.getLogger(__name__)
HISTORY_TURNS = 6  # messages of history shown to the rewriter

REWRITE_PROMPT = """Rewrite the user's latest message as one standalone question that can be \
understood without the conversation. Keep tool names, versions, error messages and code \
identifiers exactly as written. If it is already standalone, return it unchanged. Reply with \
the question only."""


def standalone_question(llm, history: list[Message], message: str) -> tuple[str, float]:
    """(question to search, rewrite cost in USD)."""
    if not history:
        return message, 0.0
    convo = "\n".join(f"{m.role}: {m.content[:800]}" for m in history[-HISTORY_TURNS:])
    try:
        text, usage = llm.complete(REWRITE_PROMPT, f"<conversation>\n{convo}\n</conversation>\n\n"
                                                   f"Latest message: {message}")
    except Exception as exc:
        log.warning("follow-up rewrite failed (%s); using the message as is", exc)
        return message, 0.0
    spent = cost(llm.model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    text = text.strip().strip('"')
    return (text if 0 < len(text) <= 1000 else message), spent
