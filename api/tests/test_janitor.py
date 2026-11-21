"""The hourly janitor: expired sandboxes are torn down, strays deleted, stuck locks released."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from infrapilot_api import janitor
from infrapilot_api.schemas import utcnow

from test_deploys import new, table  # noqa: F401  (fixture)
from test_sandbox_ops import Ctx, FakeCfn, ops  # noqa: F401  (fixture)

NOW = utcnow()


class Sfn:
    def __init__(self):
        self.started = []

    def start_execution(self, **kw):
        self.started.append(json.loads(kw["input"]))


def summary(name, status="CREATE_COMPLETE", age=timedelta(hours=1)):
    return {"StackName": name, "StackId": f"arn:stack/{name}/1", "StackStatus": status, "CreationTime": NOW - age}


@pytest.fixture
def jan(ops, monkeypatch):  # noqa: F811
    h, store, s3 = ops
    monkeypatch.setenv("TEARDOWN_STATE_MACHINE", "arn:teardown")
    monkeypatch.setenv("SANDBOX_ROLE_ARN", "arn:role/sandbox")
    sfn = Sfn()
    h._clients["stepfunctions"] = sfn
    return h, store, sfn


def live(store, status="deployed", hours_left=1.0):
    req = new(user=f"user-{len(store.locks())}")  # one sandbox per user
    store.create(req)
    store.claim(req.user_id, req.id, status="deploying")
    store.transition(req.id, {"deploying"}, teardown_at=NOW + timedelta(hours=hours_left))
    if status != "deploying":
        store.transition(req.id, {"deploying"}, status=status)
    return store.get(req.id)


def test_tears_down_only_sandboxes_past_their_time(jan):
    h, store, sfn = jan
    old, young, stuck = live(store, hours_left=-0.1), live(store), live(store, "deploying", hours_left=-2)
    h._clients["cloudformation"] = FakeCfn(summaries=[summary(r.stack_name) for r in (old, young, stuck)]
                                           + [summary("ip-sbx-gonegonego", "DELETE_COMPLETE", timedelta(days=1)),
                                              summary("other-stack", age=timedelta(days=9))])
    done = janitor.handler({}, Ctx())
    assert sorted(done["expired"]) == sorted([old.id, stuck.id])
    assert sorted(s["request_id"] for s in sfn.started) == sorted([old.id, stuck.id])
    assert store.get(old.id).status == "tearing_down" and store.get(stuck.id).status == "tearing_down"
    assert store.get(young.id).status == "deployed"
    assert janitor.sweep(NOW)["expired"] == []  # a teardown already running isn't started twice


def test_a_teardown_stuck_for_an_hour_is_started_again(jan):
    h, store, sfn = jan
    req = live(store, hours_left=-3)
    store.transition(req.id, {"deployed"}, status="tearing_down")
    h._clients["cloudformation"] = FakeCfn(summaries=[summary(req.stack_name, "DELETE_FAILED")])
    assert janitor.sweep(NOW)["retried"] == []
    assert janitor.sweep(NOW + timedelta(hours=1, minutes=1))["retried"] == [req.id]
    assert sfn.started[0]["stack"] == req.stack_name


def test_a_failed_teardown_start_leaves_the_request_to_try_next_hour(jan):
    h, store, sfn = jan
    req = live(store, "failed", hours_left=-1)
    h._clients["cloudformation"] = FakeCfn(summaries=[summary(req.stack_name)])

    def boom(**kw):
        raise RuntimeError("throttled")

    sfn.start_execution = boom
    assert janitor.sweep(NOW)["expired"] == []
    assert store.get(req.id).status == "failed"


def test_stray_sandbox_stacks_are_deleted_after_four_hours(jan):
    h, store, _ = jan
    done = new()
    store.create(done)
    store.transition(done.id, {"pending"}, status="torn_down")
    cfn = FakeCfn(summaries=[summary("ip-sbx-nobody0000", age=timedelta(hours=5)),
                             summary(done.stack_name, age=timedelta(hours=5)),
                             summary("ip-sbx-tooyoung00", age=timedelta(hours=3)),
                             summary("ip-sbx-deleting00", "DELETE_IN_PROGRESS", timedelta(hours=5))])
    h._clients["cloudformation"] = cfn
    out = janitor.sweep(NOW)
    assert sorted(out["orphans"]) == sorted(["ip-sbx-nobody0000", done.stack_name])
    assert cfn.deleted == [(f"arn:stack/{n}/1", "arn:role/sandbox") for n in ("ip-sbx-nobody0000", done.stack_name)]


def test_locks_left_behind_are_released(jan):
    h, store, _ = jan
    finished = live(store, hours_left=-0.5)  # lock kept by mistake, request already done
    store.transition(finished.id, {"deployed"}, status="torn_down")
    vanished = live(store, "failed", hours_left=-1.5)  # stack gone, but never recorded
    waiting = live(store, "deploying", hours_left=3)  # stack not created yet: leave it alone
    h._clients["cloudformation"] = FakeCfn()
    assert sorted(janitor.sweep(NOW)["unlocked"]) == sorted([finished.id, vanished.id])
    assert store.get(vanished.id).status == "torn_down"
    assert [rid for _, rid in store.locks()] == [waiting.id]
