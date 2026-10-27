"""FastAPI app for local development. Run with `infrapilot-api`; docs at /docs.

On AWS the same ChatService runs behind API Gateway in lambda_handler.py. There's no login
locally, so every session belongs to the user "local".
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .schemas import MessageIn, MessageOut, Session, SessionCreated
from .service import ChatError, ChatService, build_answers, build_assistant
from .sessions import InMemorySessionStore, SessionStore

LOCAL_USER = "local"


def create_app(service=None, store: SessionStore | None = None, assistant=None) -> FastAPI:
    """`service`, `store` and `assistant` can be injected (tests); otherwise built on first use."""
    app = FastAPI(title="InfraPilot Chat API", version="0.1.0",
                  description="Ask infrastructure and CI/CD questions; answers cite the docs they use.")
    origins = os.environ.get("INFRAPILOT_CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    chat = ChatService(store or InMemorySessionStore(),
                       (lambda: service) if service else build_answers,
                       (lambda llm: assistant) if assistant else build_assistant)

    def call(fn, *args):
        try:
            return fn(LOCAL_USER, *args)
        except ChatError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def create_session() -> SessionCreated:
        s = call(chat.create_session)
        return SessionCreated(id=s.id, created_at=s.created_at)

    @app.get("/sessions/{session_id}")
    def read_session(session_id: str) -> Session:
        return call(chat.get_session, session_id)

    # Plain `def` (not async): the model and search calls block, so FastAPI runs this in a
    # worker thread and other requests aren't held up.
    @app.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, body: MessageIn) -> MessageOut:
        return call(chat.post_message, session_id, body)

    return app
