"""The sandbox janitor (plan C13, deploy step 5): runs every hour and cleans up what's past its time.

Sandboxes live 4 hours (`teardown_at`). For every `ip-sbx-*` stack that still exists:

- its request is deploying, deployed or failed and past `teardown_at`: start the normal teardown
  workflow (a deploy still "deploying" by then is stuck: its workflow stops after 1 hour)
- its request is tearing_down but hasn't changed for an hour (the teardown workflow's limit):
  start the teardown again
- no request owns it (or the request says it's done), and it's older than 4 hours: delete it
  directly, since there's no request to record the outcome on

Then every user's one-sandbox lock is checked, so no one stays blocked: a lock whose request is
no longer active, or whose stack is gone an hour after its time, is released.

Two sweeps at once are safe: moving a request to tearing_down is a conditional write, so only one
of them starts its teardown.

Environment: REQUESTS_TABLE, TEARDOWN_STATE_MACHINE, SANDBOX_ROLE_ARN.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from aws_lambda_powertools import Logger

from infrapilot_assistant.sandbox import REQUEST_ID, STACK_PREFIX

from . import sandbox_handler as ops
from .aws_deployer import StepFunctionsDeployer
from .deploy_requests import ACTIVE, DeployRequest
from .deploys import SANDBOX_HOURS, Outcome
from .schemas import utcnow

logger = Logger(service="infrapilot-janitor")

LIFETIME = timedelta(hours=SANDBOX_HOURS)
GRACE = timedelta(hours=1)  # how long a workflow may run


def _stacks() -> list[dict]:
    pages = ops.client("cloudformation").get_paginator("list_stacks").paginate()
    return [s for page in pages for s in page["StackSummaries"]
            if s["StackName"].startswith(STACK_PREFIX) and s["StackStatus"] != "DELETE_COMPLETE"]


def _deployer() -> StepFunctionsDeployer:
    if "deployer" not in ops._clients:
        ops._clients["deployer"] = StepFunctionsDeployer(
            "", os.environ["TEARDOWN_STATE_MACHINE"], "",
            {"stepfunctions": ops.client("stepfunctions"), "s3": None, "cloudformation": None})
    return ops._clients["deployer"]


def _deadline(req: DeployRequest) -> datetime:
    return req.teardown_at or (req.deploy_started_at or req.created_at) + LIFETIME


def _teardown(req: DeployRequest) -> None:
    store, before = ops._service().requests, req.status
    if before != "tearing_down":
        req = store.transition(req.id, {before}, status="tearing_down")
    try:
        _deployer().teardown(req)
    except Exception:
        if before != "tearing_down":
            store.transition(req.id, {"tearing_down"}, status=before)  # tried again next hour
        raise


def _delete(stack: dict) -> None:
    ops.empty({"stack": stack["StackId"]})
    ops.client("cloudformation").delete_stack(StackName=stack["StackId"], RoleARN=os.environ["SANDBOX_ROLE_ARN"])


def sweep(now: datetime | None = None) -> dict:
    now = now or utcnow()
    store = ops._service().requests
    done: dict[str, list[str]] = {"expired": [], "retried": [], "orphans": [], "unlocked": []}

    live = set()
    for stack in _stacks():
        name = stack["StackName"]
        live.add(name)
        rid = name.removeprefix(STACK_PREFIX)
        req = store.get(rid) if REQUEST_ID.fullmatch(rid) else None
        try:
            if req and req.status in ("deploying", "deployed", "failed"):
                if _deadline(req) <= now:
                    _teardown(req)
                    done["expired"].append(req.id)
            elif req and req.status == "tearing_down":
                if req.updated_at <= now - GRACE:
                    _teardown(req)
                    done["retried"].append(req.id)
            elif stack["StackStatus"] != "DELETE_IN_PROGRESS" and stack["CreationTime"] <= now - LIFETIME:
                _delete(stack)
                done["orphans"].append(name)
        except Exception:  # one bad stack mustn't stop the rest
            logger.exception("couldn't clean up a sandbox", extra={"stack": name})

    for user, rid in store.locks():
        try:
            req = store.get(rid)
            if req is None or req.status not in ACTIVE:
                store.release(user, rid)
            elif req.stack_name not in live and _deadline(req) + GRACE <= now:
                ops._service().record(rid, Outcome(status="torn_down"))  # also releases the lock
            else:
                continue
            done["unlocked"].append(rid)
        except Exception:
            logger.exception("couldn't check a lock", extra={"request_id": rid})
    return done


@logger.inject_lambda_context(clear_state=True)
def handler(event, context):
    done = sweep()
    logger.info("sweep done", extra=done)
    return done
