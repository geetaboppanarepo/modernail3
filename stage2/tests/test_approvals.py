"""Approvals: human-in-the-loop sign-off in ami/approvals.py, and the rule
in ami/policy.py that sends a call there.

Pins the promises the module docstring makes:
- a refund at or over the limit is SAVED as PENDING_APPROVAL, the call
  returns at once, and the tool has not run
- only an approval lets it run; a rejection, an expiry, or a second
  approval never does, and two workers resuming run it once
- the record survives a restart, including a different process
- a bad id or an illegal transition is refused, and changes nothing
- below the limit, Stage 2 behaves exactly as before

No test sleeps: the deadline is moved by a fake clock.
"""

import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ami import approvals, planner, policy, store, tools
from ami.memory import ConversationMemory, WorkingMemory
from fakes import Reply, tool_call

HEADPHONES = "112-1111111-1111111"     # delivered, $348.00 — over the $200 limit
KINDLE = "112-3333333-3333333"         # preparing, $149.99 — under it
STAGE = Path(__file__).resolve().parent.parent


class Clock:
    """Starts at the real time — a subprocess on the real clock must agree
    the request is still open — and moves only when a test moves it."""

    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_state, clock, monkeypatch):
    """The store every module-level call uses, on the fake clock, 1h timeout."""
    path = tmp_state / "state" / "approvals.db"
    monkeypatch.setattr(approvals, "default",
                        lambda: approvals.Approvals(path, clock, timedelta(hours=1)))
    return approvals.default()


@pytest.fixture
def ran(monkeypatch):
    """Every tool that actually executed, in order."""
    calls, real = [], tools.run

    def spy(name, args, **kw):
        calls.append(name)
        return real(name, args, **kw)
    monkeypatch.setattr(tools, "run", spy)
    return calls


def confirmed_return(work=None):
    """The customer asks (turn 1), then says yes (turn 2) — the existing
    confirmation gate — so the approval gate is what decides next."""
    work = work or WorkingMemory()
    work.turn = 1
    policy.guarded_run("start_return", {"order_id": HEADPHONES, "reason": "broken"}, work)
    work.turn = 2
    return policy.guarded_run("start_return", {"order_id": HEADPHONES, "reason": "broken",
                                               "confirmed": True}, work)


# --------------------------------------------------------------------------
# Proposing: saved, pending, and nothing has run
# --------------------------------------------------------------------------

def test_a_refund_over_the_limit_becomes_a_persisted_pending_request(db, clock, fresh_store, ran):
    result = confirmed_return()

    assert result["pending_approval"] is True
    rec = db.get(result["request_id"])
    assert rec["status"] == approvals.PENDING
    assert rec["tool"] == "start_return"
    assert rec["args"] == {"order_id": HEADPHONES, "reason": "broken"}
    assert "348.00" in rec["policy_reason"]
    assert rec["deadline"] == (clock.now + timedelta(hours=1)).isoformat()
    assert rec["execution_status"] == "waiting"
    assert ran == []                                      # nothing executed
    assert store.get(HEADPHONES)["status"] == "delivered"


def test_the_agent_turn_returns_without_waiting_for_a_reviewer(db, fresh_store, ran, fake_llm):
    """A whole ReAct turn, as web.py runs it: the reply comes back, the
    request is PENDING, and the tool never ran."""
    convo, work = ConversationMemory("system"), WorkingMemory()
    work.session_id = "s1"
    args = {"order_id": HEADPHONES, "reason": "broken", "thought": "t"}
    fake_llm.script(
        Reply(tool_calls=[tool_call("start_return", **args)]),
        Reply(content="Shall I start the return?"),
        Reply(tool_calls=[tool_call("start_return", confirmed=True, **args)]),
        Reply(content="It's with a reviewer."),
    )
    work.turn = 1
    planner.react(convo, work, trace=False)
    work.turn = 2
    rid = approvals.begin("yes please", session_id="s1", context=["return my headphones"])
    reply = planner.react(convo, work, trace=False)
    rec = approvals.finish(rid, reply)

    assert reply == "It's with a reviewer."
    assert rec["status"] == approvals.PENDING
    assert rec["request"] == "yes please"
    assert rec["context"] == ["return my headphones"]
    assert rec["resolution"] == "It's with a reviewer."
    assert ran == []
    assert work.actions == []                 # a proposal is not an action


def test_the_same_action_is_not_proposed_twice(db, fresh_store):
    first = confirmed_return()
    again = confirmed_return()

    assert again["request_id"] == first["request_id"]
    assert len(db.list()) == 1


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------

def test_a_pending_action_cannot_be_resumed_before_approval(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]

    with pytest.raises(approvals.InvalidTransition):
        db.resume(rid)
    assert ran == []
    assert db.get(rid)["status"] == approvals.PENDING


def test_approval_resumes_the_workflow_and_runs_the_action(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]

    db.approve(rid, "dana", "receipt checked")
    rec = db.resume(rid)

    assert ran == ["start_return"]
    assert rec["status"] == approvals.COMPLETED
    assert rec["execution_status"] == "succeeded"
    assert rec["outcome"]["rma"].startswith("RMA-")
    assert (rec["reviewer"], rec["decision"], rec["decision_reason"]) == \
           ("dana", "approved", "receipt checked")
    assert store.get(HEADPHONES)["status"] == "return started"
    assert [e["to_status"] for e in db.events(rid)] == [
        "RECEIVED", "DRAFTED", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "COMPLETED"]


def test_a_rejection_never_runs_the_action(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]

    rec = db.reject(rid, "dana", "item was used")

    assert rec["status"] == approvals.COMPLETED
    assert rec["decision"] == "rejected"
    assert rec["execution_status"] == "not_executed"
    with pytest.raises(approvals.InvalidTransition):
        db.resume(rid)
    assert db.sweep() == {"expired": [], "resumed": []}
    assert ran == []
    assert store.get(HEADPHONES)["status"] == "delivered"


def test_an_approved_action_that_the_tool_refuses_is_failed(db, fresh_store):
    rid = confirmed_return()["request_id"]
    store.update(HEADPHONES, status="return started")      # changed meanwhile

    db.approve(rid, "dana")
    rec = db.resume(rid)

    assert rec["status"] == approvals.FAILED
    assert "error" in rec["outcome"]


def test_the_order_facts_the_reviewer_saw_are_saved_with_the_proposal(db, fresh_store):
    rec = db.get(confirmed_return()["request_id"])
    assert rec["basis"] == {"price": 348.00, "status": "delivered",
                            "delivered_on": store.get(HEADPHONES)["delivered_on"]}


def test_an_order_repriced_after_approval_is_not_run(db, fresh_store, ran):
    """A stale approval: the reviewer said yes to a $348 refund. The tool's
    own guardrail would allow the return at any price, so only the saved
    basis stops a $900 refund going out on that yes."""
    rid = confirmed_return()["request_id"]
    db.approve(rid, "dana")
    store.update(HEADPHONES, price=900.00)

    rec = db.resume(rid)

    assert ran == []
    assert (rec["status"], rec["execution_status"]) == (approvals.FAILED, "not_executed")
    assert "348.0 -> 900.0" in rec["outcome"]["error"]
    assert "348.0 -> 900.0" in db.events(rid)[-1]["detail"]       # in the audit trail
    assert store.get(HEADPHONES)["status"] == "delivered"
    assert db.resume(rid)["status"] == approvals.FAILED            # and stays that way
    assert ran == []


def test_an_unchanged_order_still_runs_after_approval(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    db.approve(rid, "dana")
    assert db.resume(rid)["execution_status"] == "succeeded"
    assert ran == ["start_return"]


def test_a_request_with_no_saved_basis_fails_closed(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    with db._tx() as conn:
        conn.execute("UPDATE requests SET basis = NULL WHERE id = ?", (rid,))
    db.approve(rid, "dana")

    rec = db.resume(rid)

    assert ran == [] and rec["status"] == approvals.FAILED
    assert "no record of the order" in rec["outcome"]["error"]


def test_a_database_from_before_the_basis_column_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = approvals._SCHEMA.replace(
        "    basis            TEXT,              -- JSON: the order facts it was proposed on\n", "")
    assert "basis" not in old
    with sqlite3.connect(path) as conn:
        conn.executescript(old)
    db = approvals.Approvals(path)
    rid = db.create("hello")
    assert db.get(rid)["basis"] is None
    approvals.Approvals(path)                                       # a second open is a no-op


# --------------------------------------------------------------------------
# Exactly once
# --------------------------------------------------------------------------

def test_a_second_approval_is_refused_and_nothing_runs_twice(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    db.approve(rid, "dana")
    db.resume(rid)

    with pytest.raises(approvals.InvalidTransition):
        db.approve(rid, "sam")
    db.resume(rid)                                   # idempotent, not a rerun
    db.sweep()

    assert ran == ["start_return"]
    assert len(store.returns()) == 1


def test_concurrent_resumes_run_the_action_once(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    db.approve(rid, "dana")

    workers = [threading.Thread(target=db.resume, args=(rid,)) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert ran == ["start_return"]
    assert db.get(rid)["status"] == approvals.COMPLETED


def test_the_http_approve_route_cannot_double_execute(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]

    first = approvals.api("POST", f"/approvals/{rid}/approve", {"reviewer": "dana"}, db=db)
    second = approvals.api("POST", f"/approvals/{rid}/approve", {"reviewer": "dana"}, db=db)

    assert first[0] == 200 and first[1]["status"] == approvals.COMPLETED
    assert second[0] == 409
    assert ran == ["start_return"]


# --------------------------------------------------------------------------
# Nobody answered: fail closed
# --------------------------------------------------------------------------

def test_an_expired_request_cannot_be_approved_and_never_runs(db, clock, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    clock.now += timedelta(hours=1)                  # exactly at the deadline

    with pytest.raises(approvals.Expired):
        db.approve(rid, "dana")
    rec = db.get(rid)
    assert rec["status"] == approvals.EXPIRED
    assert rec["execution_status"] == "not_executed"
    with pytest.raises(approvals.InvalidTransition):
        db.resume(rid)
    assert ran == []
    assert store.get(HEADPHONES)["status"] == "delivered"


def test_the_sweep_expires_overdue_requests_and_keeps_their_history(db, clock, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    clock.now += timedelta(minutes=59)
    assert db.sweep()["expired"] == []               # not yet

    clock.now += timedelta(minutes=2)
    assert db.sweep() == {"expired": [rid], "resumed": []}

    rec = db.get(rid)
    assert rec["status"] == approvals.EXPIRED
    assert rec["request"] and rec["tool"] == "start_return"          # audit kept
    assert db.events(rid)[-1]["to_status"] == "EXPIRED"
    with pytest.raises(approvals.InvalidTransition):
        db.approve(rid, "dana")
    assert ran == []


def test_an_approval_given_in_time_still_runs_after_the_deadline(db, clock, fresh_store, ran):
    """The deadline bounds the DECISION, not the worker that executes it."""
    rid = confirmed_return()["request_id"]
    db.approve(rid, "dana")
    clock.now += timedelta(days=1)

    assert db.sweep() == {"expired": [], "resumed": [rid]}
    assert ran == ["start_return"]


# --------------------------------------------------------------------------
# Restart
# --------------------------------------------------------------------------

def test_a_pending_request_survives_a_new_store_instance(tmp_state, clock, fresh_store, db):
    rid = confirmed_return()["request_id"]

    reopened = approvals.Approvals(tmp_state / "state" / "approvals.db", clock)
    assert [r["id"] for r in reopened.list(approvals.PENDING)] == [rid]


def test_a_pending_request_is_decided_from_another_process(db, tmp_state, fresh_store, ran):
    """The real restart: a separate Python process lists and approves it;
    this one then resumes it from what is on disk."""
    rid = confirmed_return()["request_id"]
    path = tmp_state / "state" / "approvals.db"
    code = ("import sys, json; from pathlib import Path; from ami import observe, approvals;"
            "observe.LOGFILE = Path(sys.argv[1]);"
            "db = approvals.Approvals(sys.argv[2]);"
            "ids = [r['id'] for r in db.list(approvals.PENDING)];"
            "db.approve(ids[0], 'night-shift');"
            "print(json.dumps(ids))")
    # No cwd and close_fds=False, so CPython uses posix_spawn rather than
    # fork() — see spawn() in test_cross_process.py for why that matters.
    out = subprocess.run([sys.executable, "-c", code, str(tmp_state / "trace.jsonl"), str(path)],
                         close_fds=False, capture_output=True, text=True, timeout=120,
                         env={**os.environ, "PYTHONPATH": str(STAGE)})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [rid]

    assert db.get(rid)["status"] == approvals.APPROVED
    assert db.resume(rid)["status"] == approvals.COMPLETED
    assert ran == ["start_return"]


# --------------------------------------------------------------------------
# Bad input
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "nope", "REQ-123", "REQ-ZZZZZZZZZZZZ", None, "REQ-'; DROP"])
def test_a_malformed_id_is_refused(db, bad):
    with pytest.raises(approvals.ApprovalError):
        db.approve(bad, "dana")


def test_an_unknown_id_is_not_found(db):
    with pytest.raises(approvals.NotFound):
        db.get("REQ-000000000000")
    assert approvals.api("POST", "/approvals/REQ-000000000000/approve",
                         {"reviewer": "dana"}, db=db)[0] == 404


def test_a_decision_needs_a_reviewer_name(db, fresh_store):
    rid = confirmed_return()["request_id"]
    for who in (None, "", "   "):
        with pytest.raises(approvals.ApprovalError):
            db.approve(rid, who)
    assert db.get(rid)["status"] == approvals.PENDING


def test_illegal_transitions_are_refused_and_change_nothing(db, fresh_store, ran):
    rid = confirmed_return()["request_id"]
    db.reject(rid, "dana")

    for attempt in (lambda: db.approve(rid, "sam"), lambda: db.reject(rid, "sam"),
                    lambda: db.resume(rid)):
        with pytest.raises(approvals.InvalidTransition):
            attempt()
    assert db.get(rid)["decision"] == "rejected"
    assert ran == []


def test_a_request_with_nothing_proposed_cannot_be_approved(db):
    rid = db.create("where is my order?")
    db.finish(rid, "It shipped.")
    with pytest.raises(approvals.InvalidTransition):
        db.approve(rid, "dana")


def test_the_api_rejects_an_unknown_status_filter(db):
    assert approvals.api("GET", "/approvals", query={"status": "bogus"}, db=db)[0] == 400


# --------------------------------------------------------------------------
# Below the limit: Stage 2 as it was
# --------------------------------------------------------------------------

def test_a_refund_under_the_limit_runs_after_the_customer_confirms(db, fresh_store, ran):
    work = WorkingMemory()
    work.turn = 1
    policy.guarded_run("cancel_order", {"order_id": KINDLE}, work)
    work.turn = 2
    result = policy.guarded_run("cancel_order", {"order_id": KINDLE, "confirmed": True}, work)

    assert result["cancelled"] is True
    assert ran == ["cancel_order"]
    assert db.list() == []


def test_the_customer_confirmation_still_comes_before_any_approval(db, fresh_store):
    work = WorkingMemory()
    work.turn = 1
    result = policy.guarded_run("start_return", {"order_id": HEADPHONES, "reason": "x",
                                                 "confirmed": True}, work)
    assert result.get("needs_confirmation") is True
    assert db.list() == []


def test_read_only_tools_never_need_approval(db, fresh_store, ran):
    policy.guarded_run("get_order", {"order_id": HEADPHONES}, WorkingMemory())
    assert ran == ["get_order"]
    assert policy.approval_reason("get_order", {"order_id": HEADPHONES}) is None
    assert policy.approval_reason("escalate", {"summary": "x"}) is None


def test_the_limit_comes_from_the_store_not_the_model(monkeypatch, fresh_store):
    assert policy.approval_reason("cancel_order", {"order_id": KINDLE, "price": 999}) is None
    monkeypatch.setattr(policy, "APPROVAL_LIMIT", 100.0)
    assert policy.approval_reason("cancel_order", {"order_id": KINDLE}) is not None


# An approval protects a refund. A call the tool's own guardrail refuses
# issues none, so it is not gated; one the tool would carry out still is.
# The orders below are seed orders with the price raised over the limit.

SHIPPED = "112-2222222-2222222"        # shipped, not delivered
LATE = "112-4444444-4444444"           # delivered 64 days ago, past the window


def confirmed(name, args):
    work = WorkingMemory()
    work.turn = 1
    first = policy.guarded_run(name, args, work)
    assert first.get("needs_confirmation") is True       # the preview still comes first
    work.turn = 2
    return policy.guarded_run(name, {**args, "confirmed": True}, work)


def test_cancelling_a_shipped_order_over_the_limit_is_refused_not_gated(db, fresh_store, ran):
    """Regression: the $260 shipped-order case in evals.HITL_CASES."""
    store.update(SHIPPED, price=260.00)

    result = confirmed("cancel_order", {"order_id": SHIPPED})

    assert "already shipped" in result["error"]
    assert ran == ["cancel_order"]                        # the guardrail answered
    assert db.list() == []                                # no approval request
    assert store.get(SHIPPED)["status"] == "shipped"


@pytest.mark.parametrize("status", ["shipped", "delivered", "cancelled"])
def test_no_approval_for_a_cancel_the_tool_refuses(fresh_store, status):
    store.update(KINDLE, price=500.00, status=status)
    assert policy.approval_reason("cancel_order", {"order_id": KINDLE}) is None


@pytest.mark.parametrize("oid, change", [
    (SHIPPED, {}),                                        # not delivered yet
    (KINDLE, {}),                                         # still preparing
    (LATE, {}),                                           # past the return window
    (HEADPHONES, {"status": "return started"}),           # already being returned
])
def test_no_approval_for_a_return_the_tool_refuses(fresh_store, oid, change):
    store.update(oid, price=500.00, **change)
    assert policy.approval_reason("start_return", {"order_id": oid, "reason": "x"}) is None


def test_a_return_of_a_shipped_order_is_refused_not_gated(db, fresh_store, ran):
    store.update(SHIPPED, price=260.00)

    result = confirmed("start_return", {"order_id": SHIPPED, "reason": "x"})

    assert "not delivered" in result["error"]
    assert ran == ["start_return"]
    assert db.list() == []


def test_shipped_orders_are_not_exempt_from_approval_wholesale(db, fresh_store, ran):
    """Once the same order is delivered, a refund on it is gated again."""
    store.update(SHIPPED, price=260.00, status="delivered",
                 delivered_on=store.get(HEADPHONES)["delivered_on"])

    result = confirmed("start_return", {"order_id": SHIPPED, "reason": "x"})

    assert result["pending_approval"] is True
    assert ran == []
    assert store.get(SHIPPED)["status"] == "delivered"


def test_an_unshipped_cancel_over_the_limit_is_still_gated(db, fresh_store, ran):
    store.update(KINDLE, price=260.00)

    result = confirmed("cancel_order", {"order_id": KINDLE})

    assert result["pending_approval"] is True
    assert "260.00" in db.get(result["request_id"])["policy_reason"]
    assert ran == []
    assert store.get(KINDLE)["status"] == "preparing"


def test_a_return_on_the_last_day_of_the_window_is_still_gated(fresh_store):
    last = (date.today() - timedelta(days=store.RETURN_WINDOW_DAYS)).isoformat()
    store.update(LATE, price=260.00, delivered_on=last)
    assert policy.approval_reason("start_return", {"order_id": LATE, "reason": "x"}) is not None
    store.update(LATE, delivered_on=(date.today() - timedelta(
        days=store.RETURN_WINDOW_DAYS + 1)).isoformat())
    assert policy.approval_reason("start_return", {"order_id": LATE, "reason": "x"}) is None


def test_read_only_tools_on_a_shipped_order_over_the_limit_never_need_approval(fresh_store):
    store.update(SHIPPED, price=260.00)
    for name in ("get_order", "track_package"):
        assert policy.approval_reason(name, {"order_id": SHIPPED}) is None


def test_the_refusal_check_is_the_tools_own(fresh_store):
    """The policy asks the same function the tool runs: same verdict, same words."""
    for name, args in [("cancel_order", {"order_id": SHIPPED}),
                       ("start_return", {"order_id": LATE, "reason": "x"}),
                       ("start_return", {"order_id": KINDLE, "reason": "x"})]:
        reason = tools.refusal(name, args)
        assert reason and tools.run(name, args) == {"error": reason}
    assert tools.refusal("cancel_order", {"order_id": KINDLE}) is None
    assert tools.refusal("start_return", {"order_id": HEADPHONES, "reason": "x"}) is None
    assert tools.refusal("get_order", {"order_id": SHIPPED}) is None


def test_a_turn_with_no_protected_action_completes(db):
    rid = approvals.begin("where is my order?", session_id="s1")
    rec = approvals.finish(rid, "It shipped yesterday.")
    assert rec["status"] == approvals.COMPLETED
    assert rec["execution_status"] == "not_required"


# --------------------------------------------------------------------------
# Back to the customer
# --------------------------------------------------------------------------

def test_sync_tells_the_session_what_was_decided_once(db, fresh_store):
    work = WorkingMemory()
    work.session_id = "s1"
    rid = confirmed_return(work)["request_id"]
    assert "PENDING_APPROVAL" in approvals.sync(work, "s1")

    db.approve(rid, "dana")
    db.resume(rid)
    note = approvals.sync(work, "s1")

    assert "APPROVED" in note and rid in note
    assert any("RMA-" in a for a in work.actions)        # check_output will trust it
    assert approvals.sync(work, "s1") is None            # told once
    assert approvals.sync(work, "someone-else") is None


def test_sync_reports_a_rejection_without_recording_an_action(db, fresh_store):
    work = WorkingMemory()
    work.session_id = "s1"
    rid = confirmed_return(work)["request_id"]
    db.reject(rid, "dana", "outside policy")

    note = approvals.sync(work, "s1")
    assert "REJECTED" in note and "outside policy" in note and "NOT done" in note
    assert work.actions == []
