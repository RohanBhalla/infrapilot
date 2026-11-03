"""Chat logic shared by the local FastAPI app and the Lambda handler.

Web frameworks stay at the edges: app.py (FastAPI, local) and lambda_handler.py (API Gateway)
only translate requests and errors. Sessions belong to a user; locally that's always "local",
on AWS it's the Cognito `sub` claim.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from infrapilot_assistant.generators import generate
from infrapilot_assistant.plan import Plan
from infrapilot_rag.models import Citation

from .schemas import Message, MessageIn, MessageOut, Session
from .sessions import SessionStore, new_message_id

log = logging.getLogger(__name__)


class ChatError(Exception):
    status = 500


class Unauthorized(ChatError):
    status = 401


class NotFound(ChatError):
    status = 404


class BadRequest(ChatError):
    status = 400


class Unavailable(ChatError):
    status = 503


class UpstreamError(ChatError):
    status = 502


class ChatService:
    """The answer service and assistant are built on the first message, so creating or reading
    a session (and a Lambda cold start) doesn't need the OpenAI key or the search backend."""

    def __init__(self, store: SessionStore, make_answers: Callable[[], object],
                 make_assistant: Callable[[object], object]):
        self.store = store
        self._make_answers, self._make_assistant = make_answers, make_assistant
        self._answers = self._assistant = None

    def _build(self) -> None:
        try:
            if self._answers is None:
                self._answers = self._make_answers()
            if self._assistant is None:
                self._assistant = self._make_assistant(self._answers.llm)
        except Exception as exc:  # e.g. missing OpenAI key or vector index
            raise Unavailable(f"answer service unavailable: {exc}") from exc

    def create_session(self, user: str) -> Session:
        return self.store.create(user)

    def get_session(self, user: str, session_id: str) -> Session:
        session = self.store.get(user, session_id)
        if session is None:
            raise NotFound("session not found")
        return session

    def post_message(self, user: str, session_id: str, body: MessageIn) -> MessageOut:
        session = self.get_session(user, session_id)
        self._build()
        answers, assistant = self._answers, self._assistant
        started = time.monotonic()

        turn = assistant.handle(body.content, session.dialog, session.messages)
        model, spent, citations, refused, text = None, turn.cost_usd, [], None, turn.message
        if turn.type == "answer":
            question = turn.standalone_question
            if turn.error_text and turn.error_text not in question:
                question = f"{question}\n\nError output:\n{turn.error_text[:1500]}"
            filters = body.filters.model_dump(exclude_none=True) if body.filters else None
            try:
                ans = answers.ask(question, filters=filters)
            except ValueError as exc:  # bad filter field
                raise BadRequest(str(exc)) from exc
            except Exception as exc:
                log.exception("answer failed")
                raise UpstreamError(f"upstream model or search error: {type(exc).__name__}") from exc
            model, spent = ans.model, spent + ans.cost_usd
            citations, refused, text = ans.citations, ans.refused, ans.text

        plan = None
        if turn.type == "ready":
            plan, text = self._plan(turn, text)
            if plan:
                citations = [Citation(n=i, title=s.title, url=s.url, chunk_id="")
                             for i, s in enumerate(plan.citations, 1)]

        user_msg = Message(id=new_message_id(), role="user", content=body.content)
        reply = Message(id=new_message_id(), role="assistant", content=text, citations=citations, refused=refused,
                        files=plan.files if plan else [])
        self.store.append(user, session_id, user_msg, reply)
        self.store.save_dialog(user, session_id, turn.state)
        out = MessageOut(session_id=session_id, type=turn.type, message=reply, intent=turn.intent,
                         confidence=turn.confidence, params=turn.params, questions=turn.questions,
                         standalone_question=turn.standalone_question, plan=plan, model=model,
                         latency_ms=int((time.monotonic() - started) * 1000), cost_usd=round(spent, 6))
        log.info("message answered", extra={"type": out.type, "intent": out.intent, "model": model,
                                            "files": len(reply.files),
                                            "latency_ms": out.latency_ms, "cost_usd": out.cost_usd,
                                            "refused": refused})
        return out

    def _plan(self, turn, text: str) -> tuple[Plan | None, str]:
        """Generate the files for a ready task and describe them under the task summary."""
        try:
            plan = generate(turn.intent, turn.params, getattr(self._assistant, "versions", None))
        except Exception:
            log.exception("generator failed")
            return None, text + "\n\nSorry, something went wrong while writing the files. Please try again."
        if plan is None:
            return None, text + "\n\nWriting the files for this task isn't available yet."
        parts = [text, plan.summary]
        if plan.setup:
            parts.append("Before the first run:\n" + "\n".join(f"- {s}" for s in plan.setup))
        if plan.notes:
            parts.append("Notes:\n" + "\n".join(f"- {n}" for n in plan.notes))
        checks = ", ".join(f"{v.tool} {'passed' if v.status == 'pass' else v.status}" for v in plan.validations)
        parts.append(f"Checks: {checks}.")
        if not plan.valid:
            parts.append("Some checks failed, so review the file before using it.")
        return plan, "\n\n".join(parts)


def build_answers():
    """The real answer service. INFRAPILOT_BACKEND=kb searches the Bedrock Knowledge Base
    (INFRAPILOT_KB_ID); the default searches the local LanceDB index."""
    from infrapilot_rag.answer import AnswerService
    from infrapilot_rag.llm import make_chat
    from infrapilot_rag.reranker import make_reranker
    from infrapilot_rag.retriever import BedrockKBRetriever, LocalLanceRetriever
    from infrapilot_rag.versions import VersionRegistry

    reranker = make_reranker()
    backend = os.environ.get("INFRAPILOT_BACKEND", "local")
    retriever = (BedrockKBRetriever(reranker=reranker) if backend == "kb"
                 else LocalLanceRetriever(reranker=reranker))
    return AnswerService(retriever, make_chat(), VersionRegistry())


def build_assistant(llm):
    from infrapilot_assistant.classifier import Classifier
    from infrapilot_assistant.dialog import Assistant
    from infrapilot_rag.versions import VersionRegistry

    return Assistant(Classifier(llm), VersionRegistry().facts)
