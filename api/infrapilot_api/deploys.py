"""Deploy requests: from a saved plan to a sandbox and back (plan C12–C14, decision 004–005).

`DeployService` holds the rules (who may do what, and when); the `Deployer` does the AWS work.
Locally the deployer is `DryRunDeployer`, which deploys nothing and reports success at once, so
the whole flow (and the frontend) can be tried without an AWS account. On AWS it's the request
workflow (Step Functions), which records each outcome back through `record`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from infrapilot_assistant import sandbox
from infrapilot_assistant.sandbox import Bundle, NotDeployable

from .artifacts import plan_key
from .deploy_requests import ACTIVE, Conflict, DeployRequest, RequestStore, new_request_id
from .schemas import utcnow
from .service import ChatError, NotFound

log = logging.getLogger(__name__)

SANDBOX_HOURS = 4
LOCAL_BOUNDARY = "arn:aws:iam::000000000000:policy/local-sandbox-boundary"


class ConflictError(ChatError):
    status = 409


class NotDeployableError(ChatError):
    status = 422


class DeployRequestIn(BaseModel):
    """The chat reply whose plan should be deployed (its `session_id` and `message.id`)."""
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=64)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback: str | None = Field(None, max_length=2000, description="What to change in the plan")


class StackEvent(BaseModel):
    id: str
    at: str
    resource: str
    status: str
    reason: str | None = None


class Logs(BaseModel):
    events: list[StackEvent] = Field(default_factory=list, description="Oldest first")
    next: str | None = Field(None, description="Pass back as `next` to get only newer events")


class Outcome(BaseModel):
    status: str  # deployed | failed | torn_down
    outputs: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class Deployer(Protocol):
    def start(self, req: DeployRequest, bundle: Bundle) -> Outcome | None:
        """Start the deploy. None: it runs on its own and reports through `record`."""

    def teardown(self, req: DeployRequest) -> Outcome | None: ...

    def logs(self, req: DeployRequest, after: str | None) -> Logs: ...


class DryRunDeployer:
    """Local development: no AWS, every deploy succeeds immediately with a made-up URL."""

    def start(self, req: DeployRequest, bundle: Bundle) -> Outcome:
        outputs = {k: f"dry-run-{req.stack_name}" for k in bundle.template.get("Outputs") or {}}
        return Outcome(status="deployed", outputs=outputs)

    def teardown(self, req: DeployRequest) -> Outcome:
        return Outcome(status="torn_down")

    def logs(self, req: DeployRequest, after: str | None) -> Logs:
        events = [StackEvent(id=f"{req.id}-{i}", at=req.updated_at.isoformat(), resource=r, status=s)
                  for i, (r, s) in enumerate([(req.stack_name, "CREATE_IN_PROGRESS"),
                                              (req.stack_name, "CREATE_COMPLETE")])]
        if after:
            ids = [e.id for e in events]
            events = events[ids.index(after) + 1:] if after in ids else events
        return Logs(events=events, next=events[-1].id if events else after)


class DeployService:
    def __init__(self, requests: RequestStore, plans, deployer: Deployer, boundary_arn: str = LOCAL_BOUNDARY):
        self.requests, self.plans, self.deployer, self.boundary_arn = requests, plans, deployer, boundary_arn

    # Reading -------------------------------------------------------------------------------

    def get(self, user: str, request_id: str) -> DeployRequest:
        req = self.requests.get(request_id) if sandbox.REQUEST_ID.fullmatch(request_id) else None
        if req is None or req.user_id != user:  # someone else's request looks the same as none
            raise NotFound("request not found")
        return req

    def history(self, user: str) -> list[DeployRequest]:
        return self.requests.list(user)

    def logs(self, user: str, request_id: str, after: str | None = None) -> Logs:
        req = self.get(user, request_id)
        if req.status in ("pending", "rejected"):
            return Logs(next=after)
        return self.deployer.logs(req, after)

    # Changing ------------------------------------------------------------------------------

    def _bundle(self, plan, request_id: str) -> Bundle:
        try:
            return sandbox.prepare(plan, request_id, self.boundary_arn)
        except NotDeployable as exc:
            raise NotDeployableError(str(exc)) from exc

    def create(self, user: str, session_id: str, message_id: str) -> DeployRequest:
        """A pending request for the plan in a chat reply. Refused now, not at approval, if the
        sandbox can't run it, so the user hears why straight away."""
        if self.plans is None:
            raise NotFound("plans aren't saved here")
        key = plan_key(user, session_id, message_id)  # the user's own prefix: no one else's plans
        plan = self.plans.load(key)
        if plan is None:
            raise NotFound("plan not found")
        request_id = new_request_id()
        bundle = self._bundle(plan, request_id)
        req = DeployRequest(id=request_id, user_id=user, session_id=session_id, message_id=message_id,
                            plan_key=key, intent=plan.intent, summary=plan.summary.split("\n", 1)[0],
                            stack_name=bundle.stack_name)
        self.requests.create(req)
        log.info("deploy requested", extra={"request_id": req.id, "intent": req.intent})
        return req

    def approve(self, user: str, request_id: str) -> DeployRequest:
        req = self.get(user, request_id)
        plan = self.plans.load(req.plan_key)
        if plan is None:  # plans expire after 30 days
            raise NotFound("this plan has expired; ask for it again")
        bundle = self._bundle(plan, req.id)
        now = utcnow()
        try:
            req = self.requests.claim(user, req.id, status="deploying", deploy_started_at=now,
                                      teardown_at=now + timedelta(hours=SANDBOX_HOURS))
        except Conflict as exc:
            if exc.active_id:
                raise ConflictError(f"You already have a sandbox ({exc.active_id}). Tear it down first.") from exc
            raise ConflictError(f"This request can't be approved: it's {req.status}.") from exc
        try:
            outcome = self.deployer.start(req, bundle)
        except Exception as exc:
            log.exception("deploy didn't start")
            self.record(req.id, Outcome(status="failed", error=f"The deploy didn't start ({type(exc).__name__})."))
            self.requests.release(user, req.id)  # nothing was created
            return self.requests.get(req.id)
        return self.record(req.id, outcome) if outcome else req

    def reject(self, user: str, request_id: str, feedback: str | None = None) -> DeployRequest:
        self.get(user, request_id)
        try:
            return self.requests.transition(request_id, {"pending"}, status="rejected", feedback=feedback)
        except Conflict as exc:
            raise ConflictError(f"Only a pending request can be rejected ({exc}).") from exc

    def teardown(self, user: str, request_id: str) -> DeployRequest:
        req = self.get(user, request_id)
        before = req.status
        try:
            req = self.requests.transition(req.id, {"deployed", "failed"}, status="tearing_down")
        except Conflict as exc:
            raise ConflictError(f"There's nothing to tear down ({exc}).") from exc
        try:
            outcome = self.deployer.teardown(req)
        except Exception:
            self.requests.transition(req.id, {"tearing_down"}, status=before)  # so it can be tried again
            raise
        return self.record(req.id, outcome) if outcome else req

    def record(self, request_id: str, outcome: Outcome) -> DeployRequest:
        """An outcome from the deployer. Releases the user's lock once the stack is gone."""
        allowed = {"deploying"} if outcome.status in ("deployed", "failed") else {"tearing_down", *ACTIVE}
        fields = {"status": outcome.status}
        if outcome.outputs:
            fields["outputs"] = outcome.outputs
        if outcome.error:
            fields["error"] = outcome.error
        req = self.requests.transition(request_id, allowed, **fields)
        if req.status == "torn_down":
            self.requests.release(req.user_id, req.id)
        log.info("deploy outcome", extra={"request_id": req.id, "status": req.status})
        return req

