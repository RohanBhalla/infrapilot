"""FastAPI app for the Chat API. Run locally with `infrapilot-api`; docs at /docs.

The same app will run on Lambda behind API Gateway (Phase 2) via an ASGI adapter.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .schemas import Message, MessageIn, MessageOut, Session, SessionCreated
from .sessions import InMemorySessionStore, SessionStore, new_message_id

log = logging.getLogger(__name__)


def build_service():
    """The real answer service, same setup as `infrapilot-ask` (local LanceDB backend)."""
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


def create_app(service=None, store: SessionStore | None = None, assistant=None) -> FastAPI:
    """`service`, `store` and `assistant` can be injected (tests); otherwise built on first use."""
    app = FastAPI(title="InfraPilot Chat API", version="0.1.0",
                  description="Ask infrastructure and CI/CD questions; answers cite the docs they use.")
    origins = os.environ.get("INFRAPILOT_CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    state = {"service": service, "store": store or InMemorySessionStore(), "assistant": assistant}

    def svc():
        if state["service"] is None:
            try:
                state["service"] = build_service()
            except Exception as exc:  # e.g. missing OPENAI_API_KEY or vector index
                raise HTTPException(503, f"answer service unavailable: {exc}") from exc
        return state["service"]

    def get_session(session_id: str) -> Session:
        s = state["store"].get(session_id)
        if s is None:
            raise HTTPException(404, "session not found")
        return s

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def create_session() -> SessionCreated:
        s = state["store"].create()
        return SessionCreated(id=s.id, created_at=s.created_at)

    @app.get("/sessions/{session_id}")
    def read_session(session_id: str) -> Session:
        return get_session(session_id)

    # Plain `def` (not async): the model and search calls block, so FastAPI runs this in a
    # worker thread and other requests aren't held up.
    @app.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, body: MessageIn) -> MessageOut:
        session = get_session(session_id)
        service = svc()
        if state["assistant"] is None:
            state["assistant"] = build_assistant(service.llm)
        started = time.monotonic()

        turn = state["assistant"].handle(body.content, session.dialog, session.messages)
        model, spent, citations, refused, text = None, turn.cost_usd, [], None, turn.message
        if turn.type == "answer":
            question = turn.standalone_question
            if turn.error_text and turn.error_text not in question:
                question = f"{question}\n\nError output:\n{turn.error_text[:1500]}"
            filters = body.filters.model_dump(exclude_none=True) if body.filters else None
            try:
                ans = service.ask(question, filters=filters)
            except ValueError as exc:  # bad filter field
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                log.exception("answer failed")
                raise HTTPException(502, f"upstream model or search error: {type(exc).__name__}") from exc
            model, spent = ans.model, spent + ans.cost_usd
            citations, refused, text = ans.citations, ans.refused, ans.text

        user_msg = Message(id=new_message_id(), role="user", content=body.content)
        reply = Message(id=new_message_id(), role="assistant", content=text, citations=citations, refused=refused)
        state["store"].append(session_id, user_msg, reply)
        state["store"].save_dialog(session_id, turn.state)
        return MessageOut(session_id=session_id, type=turn.type, message=reply, intent=turn.intent,
                          confidence=turn.confidence, params=turn.params, questions=turn.questions,
                          standalone_question=turn.standalone_question, model=model,
                          latency_ms=int((time.monotonic() - started) * 1000), cost_usd=round(spent, 6))

    return app
