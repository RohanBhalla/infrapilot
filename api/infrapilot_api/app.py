"""FastAPI app for local development. Run with `infrapilot-api`; docs at /docs.

On AWS the same ChatService runs behind API Gateway in lambda_handler.py. There's no login
locally, so every session belongs to the user "local".
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .artifacts import InMemoryPlanStore, InMemorySiteUploads
from .deploy_requests import DeployRequest, InMemoryRequestStore
from .deploys import DeployRequestIn, DeployService, DryRunDeployer, Logs, RejectIn, UploadLink
from .site_zip import MAX_ZIP_BYTES
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

    plans = InMemoryPlanStore()
    chat = ChatService(store or InMemorySessionStore(),
                       (lambda: service) if service else build_answers,
                       (lambda llm: assistant) if assistant else build_assistant, plans=plans)
    uploads = InMemorySiteUploads()
    deploys = DeployService(InMemoryRequestStore(), plans, DryRunDeployer(), uploads=uploads)

    def call(fn, *args):  # also used for DeployService, whose errors are ChatErrors too
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

    # Deploy requests: locally nothing is deployed (DryRunDeployer), so every deploy "succeeds".
    @app.post("/requests", status_code=201)
    def create_request(body: DeployRequestIn) -> DeployRequest:
        return call(deploys.create, body.session_id, body.message_id)

    @app.get("/requests")
    def list_requests() -> list[DeployRequest]:
        return call(deploys.history)

    @app.get("/requests/{request_id}")
    def read_request(request_id: str) -> DeployRequest:
        return call(deploys.get, request_id)

    @app.post("/requests/{request_id}/approve")
    def approve_request(request_id: str) -> DeployRequest:
        return call(deploys.approve, request_id)

    # The user's own site (decision 006). On AWS the browser sends the zip straight to S3; locally
    # it PUTs it here.
    @app.post("/requests/{request_id}/upload")
    def upload_link(request_id: str) -> UploadLink:
        return call(deploys.upload_link, request_id)

    @app.put("/requests/{request_id}/site", status_code=204, include_in_schema=False)
    async def put_site(request_id: str, request: Request) -> None:
        call(deploys.upload_link, request_id)  # same checks: the user's pending static site
        data = await request.body()
        if len(data) > MAX_ZIP_BYTES:
            raise HTTPException(413, "The zip is too large.")
        uploads.put(request_id, data)

    @app.post("/requests/{request_id}/upload/check")
    def check_upload(request_id: str) -> DeployRequest:
        return call(deploys.check_upload, request_id)

    @app.post("/requests/{request_id}/reject")
    def reject_request(request_id: str, body: RejectIn | None = None) -> DeployRequest:
        return call(deploys.reject, request_id, body.feedback if body else None)

    @app.post("/requests/{request_id}/teardown")
    def teardown_request(request_id: str) -> DeployRequest:
        return call(deploys.teardown, request_id)

    @app.get("/requests/{request_id}/logs")
    def request_logs(request_id: str, next: str | None = Query(None, max_length=200)) -> Logs:
        return call(deploys.logs, request_id, next)

    return app
