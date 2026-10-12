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

from .followups import standalone_question
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


def create_app(service=None, store: SessionStore | None = None, rewriter=None) -> FastAPI:
    """`service`, `store` and `rewriter` can be injected (tests); otherwise built on first use."""
    app = FastAPI(title="InfraPilot Chat API", version="0.1.0",
                  description="Ask infrastructure and CI/CD questions; answers cite the docs they use.")
    origins = os.environ.get("INFRAPILOT_CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    state = {"service": service, "store": store or InMemorySessionStore(), "rewriter": rewriter}

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

    # Plain `def` (not async): the answer service blocks on network calls, so FastAPI runs
    # it in a worker thread and other requests aren't held up.
    @app.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, body: MessageIn) -> MessageOut:
        session = get_session(session_id)
        service = svc()
        started = time.monotonic()
        question, rewrite_cost = standalone_question(state["rewriter"] or service.llm,
                                                     session.messages, body.content)
        filters = body.filters.model_dump(exclude_none=True) if body.filters else None
        try:
            ans = service.ask(question, filters=filters)
        except ValueError as exc:  # bad filter field
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            log.exception("answer failed")
            raise HTTPException(502, f"upstream model or search error: {type(exc).__name__}") from exc

        user_msg = Message(id=new_message_id(), role="user", content=body.content)
        reply = Message(id=new_message_id(), role="assistant", content=ans.text,
                        citations=ans.citations, refused=ans.refused)
        state["store"].append(session_id, user_msg, reply)
        return MessageOut(session_id=session_id, message=reply, standalone_question=question,
                          model=ans.model, latency_ms=int((time.monotonic() - started) * 1000),
                          cost_usd=round(ans.cost_usd + rewrite_cost, 6))

    return app
