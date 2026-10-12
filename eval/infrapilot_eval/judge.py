"""Optional LLM-as-judge: rates faithfulness and correctness 1-5. Opt-in (--judge) to save cost."""

from __future__ import annotations

import json
import re

JUDGE_PROMPT = """You grade answers from a documentation assistant. You get the question, the \
numbered excerpts the assistant was given, and its answer.

Score two things from 1 (bad) to 5 (excellent):
- faithfulness: every claim in the answer is supported by the excerpts (or the [V] version \
facts). Penalize claims the excerpts don't support, even if true.
- correctness: the answer correctly and completely addresses the question, given what the \
excerpts contain.

Reply with JSON only: {"faithfulness": n, "correctness": n, "reason": "<one sentence>"}"""


def judge(llm, question: str, user_message: str, answer: str) -> dict:
    prompt = f"{user_message}\n\n<answer>\n{answer}\n</answer>\n\nQuestion being answered: {question}"
    text, usage = llm.complete(JUDGE_PROMPT, prompt)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        out = {}
    return {"faithfulness": out.get("faithfulness"), "correctness": out.get("correctness"),
            "reason": out.get("reason", text[:200]), "usage": usage}
