# 005: Deploy requests and approval

- **Status:** accepted
- **Date:** 2026-11-17

## Decision

1. **A request is made from a chat reply.** `POST /requests {session_id, message_id}` loads the
   plan saved for that reply (`plans/<user>/<session>/<message>.json`) and runs
   `sandbox.prepare` on it straight away, so a plan the sandbox can't run (workflow-only, custom
   domain, failed checks) is refused with the reason (422) before anyone approves it.
2. **Approval starts the workflow; the workflow doesn't wait for approval.** The plan said Step
   Functions would hold a task token for up to 24 hours. Instead the request record (DynamoDB
   `requests`) is the waiting state: `pending` until the user clicks Approve, which starts the
   deploy. Rejecting just marks it `rejected` with the feedback. That leaves no executions
   waiting for a click that never comes, and approve/reject are plain record updates.
3. **States:** `pending → deploying → deployed / failed → tearing_down → torn_down`, or
   `pending → rejected`. Each change is a conditional update, so a double click or a late
   workflow callback can't move a request backwards.
4. **One active sandbox per user** is a lock record (`id = ACTIVE#<user>`), written in the same
   transaction that moves a request to `deploying` and deleted when its stack is gone. Two
   approvals at the same moment can't both win. A `failed` sandbox still holds the lock until
   it's torn down, because its half-built stack may still exist.
5. **Ownership:** every route checks the request's `user_id` against the Cognito `sub`; someone
   else's request answers 404, like a missing one. Plans are looked up under the caller's own
   prefix, so nobody can request a deploy of another user's plan.
6. **Locally** the deployer is a dry run: approve "succeeds" at once and nothing is created, so
   the flow and the frontend work without AWS.

## Cost

DynamoDB on demand: a few reads and writes per request, well under a cent a month at our usage.
