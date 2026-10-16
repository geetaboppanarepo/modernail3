"""Recovering a request a crashed worker left EXECUTING.

A crash is simulated by raising Crash (a BaseException, so nothing in
resume() swallows it) at the exact point a worker would die: after the
order store committed, or before it was reached. The approval transaction
that was open at that moment rolls back, as it would if the process died.
test_cross_process.py does the same with a real process that exits.
"""

import sqlite3
import threading

import pytest

from ami import approvals, policy, store, tools
from ami.memory import WorkingMemory

HEADPHONES = "112-1111111-1111111"     # delivered, $348.00: needs a reviewer


class Crash(BaseException):
    """The worker process dying."""


@pytest.fixture
def db(tmp_state):
    return approvals.default()


@pytest.fixture
def ran(monkeypatch):
    calls, real = [], tools.run

    def spy(name, args, **kw):
        calls.append(name)
        return real(name, args, **kw)
    monkeypatch.setattr(tools, "run", spy)
    return calls


def approved(db):
    work = WorkingMemory()
    work.turn = 1
    args = {"order_id": HEADPHONES, "reason": "broken"}
    policy.guarded_run("start_return", args, work)
    work.turn = 2
    rid = policy.guarded_run("start_return", {**args, "confirmed": True}, work)["request_id"]
    db.approve(rid, "dana")
    return rid


def crash_after_commit(db, rid, monkeypatch):
    """The store commits the return; the worker dies before COMPLETED."""
    real = approvals.Approvals._move

    def dying(self, conn, rid_, frm, to, actor, *a, **kw):
        if frm == approvals.EXECUTING and actor == "worker":
            raise Crash()
        return real(self, conn, rid_, frm, to, actor, *a, **kw)
    monkeypatch.setattr(approvals.Approvals, "_move", dying)
    with pytest.raises(Crash):
        db.resume(rid)
    monkeypatch.setattr(approvals.Approvals, "_move", real)


def crash_before_store(db, rid, monkeypatch):
    """The worker claims the request and dies before reaching the store."""
    real = tools.run
    monkeypatch.setattr(tools, "run", lambda *a, **kw: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        db.resume(rid)
    monkeypatch.setattr(tools, "run", real)


def transitions(db, rid):
    return [(e["from_status"], e["to_status"], e["actor"]) for e in db.events(rid)]


# --------------------------------------------------------------------------
# The order store committed; the approval record did not
# --------------------------------------------------------------------------

def test_a_crash_after_the_store_committed_leaves_the_request_executing(db, monkeypatch):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)

    assert db.get(rid)["status"] == approvals.EXECUTING
    assert store.get(HEADPHONES)["status"] == "return started"
    assert db.diagnose(rid)["state"] == "committed"
    assert db.sweep() == {"expired": [], "resumed": []}            # nothing re-runs it


def test_recovery_finishes_from_the_store_without_a_second_refund(db, monkeypatch, ran):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    ran.clear()

    rec, d = db.reconcile(rid, "ops-sam")

    assert d["state"] == "committed"
    assert ran == []                                                # not run again
    assert (rec["status"], rec["execution_status"]) == (approvals.COMPLETED, "succeeded")
    assert rec["outcome"] == store.operation(rid) and rec["outcome"]["rma"] == "RMA-1001"
    assert len(store.refunds()) == 1 and len(store.returns()) == 1
    assert transitions(db, rid)[-1] == (approvals.EXECUTING, approvals.COMPLETED,
                                        "recovery:ops-sam")
    assert "not run again" in db.events(rid)[-1]["detail"]


def test_recovering_twice_changes_nothing_the_second_time(db, monkeypatch):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    first, _ = db.reconcile(rid, "ops-sam")
    events = db.events(rid)

    again, d = db.reconcile(rid, "ops-sam")

    assert d["state"] == "not_executing"
    assert again == first and db.events(rid) == events


# --------------------------------------------------------------------------
# The store never decided: a retry under the same id is safe
# --------------------------------------------------------------------------

def test_a_request_that_never_reached_the_store_is_retried_once(db, monkeypatch, ran):
    rid = approved(db)
    crash_before_store(db, rid, monkeypatch)
    assert db.diagnose(rid)["state"] == "not_committed"
    assert store.get(HEADPHONES)["status"] == "delivered"

    rec, d = db.reconcile(rid, "ops-sam")

    assert ran == ["start_return"]
    assert d["state"] == "committed"
    assert rec["status"] == approvals.COMPLETED
    assert store.refunds()[HEADPHONES]["op_id"] == rid
    assert "retried under the same id" in db.events(rid)[-1]["detail"]


def test_a_retry_still_checks_the_approved_order_facts(db, monkeypatch):
    rid = approved(db)
    crash_before_store(db, rid, monkeypatch)
    store.update(HEADPHONES, price=900.00)

    rec, d = db.reconcile(rid, "ops-sam")

    assert d["state"] == "refused"
    assert (rec["status"], rec["execution_status"]) == (approvals.FAILED, "failed")
    assert "348.0 -> 900.0" in rec["outcome"]["error"]
    assert store.refunds() == {}


def test_a_refusal_recorded_before_the_crash_is_not_retried(db, monkeypatch, ran):
    """The store refused (price changed), then the worker died. Putting the
    price back must not let recovery run it: the refusal was the decision."""
    rid = approved(db)
    store.update(HEADPHONES, price=900.00)
    monkeypatch.setattr(approvals, "_changed", lambda saved, args: None)   # reach the store
    crash_after_commit(db, rid, monkeypatch)
    store.update(HEADPHONES, price=348.00)
    ran.clear()

    rec, d = db.reconcile(rid, "ops-sam")

    assert d["state"] == "refused" and ran == []
    assert rec["status"] == approvals.FAILED and store.refunds() == {}


# --------------------------------------------------------------------------
# What cannot be established is flagged, never guessed
# --------------------------------------------------------------------------

def test_a_different_order_store_is_unresolved_not_retried(db, monkeypatch, tmp_path, ran):
    """The operator points at the wrong orders.db (say, a fresh one). It has
    no record under this id — which proves nothing about the real store."""
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    monkeypatch.setattr(store, "PATH", tmp_path / "other-orders.db")
    ran.clear()

    rec, d = db.reconcile(rid, "ops-sam")

    assert d["state"] == "unresolved" and "a different database" in d["why"]
    assert ran == [] and store.refunds() == {}                     # nothing ran here either
    assert (rec["status"], rec["execution_status"]) == (approvals.EXECUTING, "unresolved")
    assert db.events(rid)[-1]["detail"].startswith("unresolved: ")


def test_flagging_is_recorded_once(db, monkeypatch, tmp_path):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    monkeypatch.setattr(store, "PATH", tmp_path / "other-orders.db")
    db.reconcile(rid, "ops-sam")
    n = len(db.events(rid))
    db.reconcile(rid, "ops-kim")
    assert len(db.events(rid)) == n


def test_a_request_claimed_before_the_store_id_was_recorded_is_unresolved(db, monkeypatch):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    with db._tx() as conn:
        conn.execute("UPDATE requests SET orders_store = NULL WHERE id = ?", (rid,))
    assert db.reconcile(rid, "ops-sam")[1]["state"] == "unresolved"
    assert db.get(rid)["status"] == approvals.EXECUTING


def test_an_operation_for_another_order_is_unresolved(db, monkeypatch):
    rid = approved(db)
    crash_before_store(db, rid, monkeypatch)
    with sqlite3.connect(store.path()) as conn:
        conn.execute("INSERT INTO operations VALUES (?, 'start_return', "
                     "'112-4444444-4444444', '{\"rma\": \"RMA-9\"}', 'x')", (rid,))
    rec, d = db.reconcile(rid, "ops-sam")
    assert d["state"] == "unresolved" and "not start_return on " + HEADPHONES in d["why"]
    assert rec["status"] == approvals.EXECUTING


def test_a_result_without_its_refund_is_unresolved(db, monkeypatch):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    with sqlite3.connect(store.path()) as conn:
        conn.execute("DELETE FROM refunds WHERE op_id = ?", (rid,))
    rec, d = db.reconcile(rid, "ops-sam")
    assert d["state"] == "unresolved" and "ledger disagree" in d["why"]
    assert rec["status"] == approvals.EXECUTING


def test_no_decision_and_no_approved_facts_is_unresolved(db, monkeypatch, ran):
    rid = approved(db)
    crash_before_store(db, rid, monkeypatch)
    with db._tx() as conn:
        conn.execute("UPDATE requests SET basis = NULL WHERE id = ?", (rid,))
    ran.clear()
    assert db.reconcile(rid, "ops-sam")[1]["state"] == "unresolved"
    assert ran == []


def test_a_retry_that_never_reaches_the_store_is_unresolved(db, monkeypatch):
    rid = approved(db)
    crash_before_store(db, rid, monkeypatch)
    monkeypatch.setattr(tools, "run", lambda *a, **kw: {"error": "tool registry broken"})
    rec, d = db.reconcile(rid, "ops-sam")
    assert d["state"] == "unresolved" and "still has no decision" in d["why"]
    assert rec["status"] == approvals.EXECUTING


# --------------------------------------------------------------------------
# A person closes what recovery could not
# --------------------------------------------------------------------------

def test_resolve_needs_an_unresolved_request_a_name_and_a_reason(db, monkeypatch, tmp_path):
    rid = approved(db)
    crash_after_commit(db, rid, monkeypatch)
    with pytest.raises(approvals.InvalidTransition):                # not flagged yet
        db.resolve(rid, "ops-sam", "completed", "checked the ledger")
    monkeypatch.setattr(store, "PATH", tmp_path / "other-orders.db")
    db.reconcile(rid, "ops-sam")
    for who, why in (("", "x"), ("ops-sam", ""), ("ops-sam", None)):
        with pytest.raises(approvals.ApprovalError):
            db.resolve(rid, who, "completed", why)
    with pytest.raises(approvals.ApprovalError):
        db.resolve(rid, "ops-sam", "maybe", "x")

    rec = db.resolve(rid, "ops-sam", "completed", "refund RMA-1001 seen in the real orders.db")

    assert (rec["status"], rec["execution_status"]) == (approvals.COMPLETED, "succeeded")
    assert transitions(db, rid)[-1][2] == "operator:ops-sam"
    assert "refund RMA-1001 seen" in db.events(rid)[-1]["detail"]
    with pytest.raises(approvals.InvalidTransition):
        db.resolve(rid, "ops-sam", "failed", "changed my mind")


# --------------------------------------------------------------------------
# Concurrency, and what recovery must leave alone
# --------------------------------------------------------------------------

@pytest.mark.parametrize("crash", [crash_after_commit, crash_before_store])
def test_concurrent_recoveries_finish_once_and_agree(db, monkeypatch, crash):
    rid = approved(db)
    crash(db, rid, monkeypatch)
    barrier, results = threading.Barrier(6), []

    def operator(i):
        barrier.wait()
        results.append(db.reconcile(rid, f"ops-{i}")[0])

    threads = [threading.Thread(target=operator, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {r["status"] for r in results} == {approvals.COMPLETED}
    assert len({repr(r["outcome"]) for r in results}) == 1
    finished = [t for t in transitions(db, rid) if t[0] == approvals.EXECUTING]
    assert len(finished) == 1
    assert len(store.refunds()) == 1 and len(store.returns()) == 1


def test_a_live_worker_and_a_recovery_racing_agree(db, monkeypatch):
    """Recovery does not know the worker is merely slow. Both reach the
    store under the same id: one decides, the other replays, and whoever
    finishes second finds it finished."""
    rid = approved(db)
    real, gate, inside = tools.run, threading.Event(), threading.Event()

    def slow(name, args, **kw):
        if threading.current_thread().name == "worker":
            inside.set()
            gate.wait(5)
        return real(name, args, **kw)
    monkeypatch.setattr(tools, "run", slow)
    out = {}
    worker = threading.Thread(name="worker", target=lambda: out.update(w=db.resume(rid)))
    worker.start()
    inside.wait(5)                                       # claimed, not yet in the store
    out["r"] = db.reconcile(rid, "ops-sam")[0]
    gate.set()
    worker.join()

    assert out["r"]["status"] == out["w"]["status"] == approvals.COMPLETED
    assert out["r"]["outcome"] == out["w"]["outcome"]
    assert len(store.refunds()) == 1


def proposed(db, order_id=HEADPHONES):
    """Confirmed by the customer, waiting for a reviewer."""
    work = WorkingMemory()
    work.turn = 1
    tool, args = (("start_return", {"order_id": order_id, "reason": "broken"})
                  if order_id == HEADPHONES else ("cancel_order", {"order_id": order_id}))
    policy.guarded_run(tool, args, work)
    work.turn = 2
    return policy.guarded_run(tool, {**args, "confirmed": True}, work)["request_id"]


@pytest.mark.parametrize("status", ["completed", "rejected", "pending", "approved", "expired"])
def test_recovery_leaves_every_other_request_alone(db, ran, monkeypatch, status):
    rid = proposed(db)
    if status in ("completed", "approved"):
        db.approve(rid, "dana")
    if status == "completed":
        db.resume(rid)
    if status == "rejected":
        db.reject(rid, "dana", "no receipt")
    if status == "expired":
        with monkeypatch.context() as m:              # only this patch; tmp_state stays
            m.setattr(approvals.Approvals, "_now", lambda self: "9999-12-31T00:00:00+00:00")
            db.sweep()
    before, events, orders = db.get(rid), db.events(rid), store.orders()
    ran.clear()

    rec, d = db.reconcile(rid, "ops-sam")

    assert d["state"] == "not_executing"
    assert rec == before and db.events(rid) == events
    assert ran == [] and store.orders() == orders          # an APPROVED one is not run either
    if status in ("rejected", "expired"):
        assert store.refunds() == {}


def test_resume_takes_the_stores_record_over_a_failure_on_the_way_back(db, monkeypatch):
    rid = approved(db)
    real = tools.run

    def committed_then_raised(name, args, **kw):
        real(name, args, **kw)
        raise RuntimeError("lost the reply")
    monkeypatch.setattr(tools, "run", committed_then_raised)

    rec = db.resume(rid)

    assert (rec["status"], rec["execution_status"]) == (approvals.COMPLETED, "succeeded")
    assert rec["outcome"]["rma"] == "RMA-1001"
