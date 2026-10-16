"""The order store in SQLite: persistence, seeding, atomic changes, idempotency.

Every test gets its own temp state/orders.db (conftest.tmp_state), seeded
with the fixture orders on first use. Nothing here calls the model.
"""

import sqlite3
import threading
from datetime import date, timedelta

import pytest

from ami import approvals, policy, store, tools
from ami.memory import WorkingMemory

HEADPHONES = "112-1111111-1111111"     # delivered 4 days ago, $348.00
KINDLE = "112-3333333-3333333"         # preparing, $149.99


def reopen():
    """Forget this process's 'already set up' note, as a new process would."""
    store._READY.clear()


# --------------------------------------------------------------------------
# Where it lives, and what is in it
# --------------------------------------------------------------------------

def test_the_path_is_configurable(monkeypatch, tmp_path):
    assert store.path() == tmp_path / "state" / "orders.db"        # the default, under ROOT
    monkeypatch.setenv("AMI_ORDERS_DB", str(tmp_path / "env.db"))
    assert store.path() == tmp_path / "env.db"
    monkeypatch.setattr(store, "PATH", tmp_path / "explicit.db")
    assert store.path() == tmp_path / "explicit.db"               # PATH wins


def test_a_new_database_is_seeded_with_the_fixtures(fresh_store):
    assert set(store.orders()) == set(store.fixtures())
    assert store.returns() == {} and store.refunds() == {}


def test_orders_persist_after_the_store_is_reopened(fresh_store):
    tools.cancel_order(KINDLE)
    store.update(HEADPHONES, price=1.00)
    reopen()
    assert store.get(KINDLE)["status"] == "cancelled"
    assert store.get(HEADPHONES)["price"] == 1.00                  # not re-seeded
    assert list(store.refunds()) == [KINDLE]


def test_reopening_never_reseeds_even_an_emptied_database(fresh_store):
    assert len(store.orders()) == 4                                # created and seeded
    with sqlite3.connect(store.path()) as conn:
        conn.execute("DELETE FROM orders")
    reopen()
    assert store.orders() == {}


def test_an_existing_database_with_orders_is_never_overwritten(monkeypatch, tmp_path):
    """A file that already holds orders (made before the seed marker existed,
    or by hand) is left exactly as it is."""
    path = tmp_path / "theirs.db"
    monkeypatch.setattr(store, "PATH", path)
    with sqlite3.connect(path) as conn:
        conn.executescript(store._SCHEMA)
        conn.execute("INSERT INTO orders (order_id, email, item, price, status) "
                     "VALUES ('999-0000000-0000001', 'a@b.c', 'Lamp', 20.0, 'delivered')")
    reopen()
    assert list(store.orders()) == ["999-0000000-0000001"]


def test_reset_backs_up_then_reseeds(fresh_store):
    tools.cancel_order(KINDLE)
    saved = store.reset()
    assert store.get(KINDLE)["status"] == "preparing"
    with sqlite3.connect(saved) as conn:
        assert conn.execute("SELECT status FROM orders WHERE order_id = ?",
                            (KINDLE,)).fetchone()[0] == "cancelled"


def test_backup_is_a_readable_copy(fresh_store, tmp_path):
    tools.start_return(HEADPHONES, "broken")
    copy = store.backup(tmp_path / "copy.db")
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 1


# --------------------------------------------------------------------------
# Changing an order: one transaction, one refund
# --------------------------------------------------------------------------

def test_a_refund_is_the_stored_price(fresh_store):
    store.update(KINDLE, price=123.45)
    assert tools.cancel_order(KINDLE)["refund_amount"] == 123.45
    assert store.refunds()[KINDLE]["amount"] == 123.45


def test_a_refused_change_writes_nothing(fresh_store):
    assert "error" in tools.cancel_order("112-2222222-2222222")    # shipped
    assert "error" in tools.start_return(KINDLE, "x")              # not delivered
    assert store.refunds() == {} and store.returns() == {}
    assert store.get(KINDLE)["status"] == "preparing"


def test_retrying_an_operation_returns_its_first_result_and_refunds_once(fresh_store):
    first = tools.run("start_return", {"order_id": HEADPHONES, "reason": "x"}, op_id="REQ-aaaaaaaaaaaa")
    again = tools.run("start_return", {"order_id": HEADPHONES, "reason": "x"}, op_id="REQ-aaaaaaaaaaaa")

    assert first == again and first["rma"] == "RMA-1001"
    assert len(store.returns()) == 1
    assert store.refunds()[HEADPHONES]["op_id"] == "REQ-aaaaaaaaaaaa"
    assert store.operation("REQ-aaaaaaaaaaaa") == first


def test_a_second_operation_on_the_same_order_is_refused(fresh_store):
    tools.run("start_return", {"order_id": HEADPHONES, "reason": "x"}, op_id="REQ-aaaaaaaaaaaa")
    other = tools.run("start_return", {"order_id": HEADPHONES, "reason": "x"}, op_id="REQ-bbbbbbbbbbbb")
    assert "return started" in other["error"]
    assert len(store.returns()) == 1


def test_the_refund_ledger_holds_even_if_the_status_is_set_back(fresh_store):
    """Belt and braces: an admin edit puts the order back to delivered. The
    guardrail would now allow a second return; the ledger refuses a second
    refund for the same order, and the whole change rolls back."""
    tools.start_return(HEADPHONES, "x")
    store.update(HEADPHONES, status="delivered")

    result = tools.start_return(HEADPHONES, "x")

    assert "already been refunded" in result["error"]
    assert len(store.returns()) == 1
    assert store.get(HEADPHONES)["status"] == "delivered"          # rolled back


@pytest.mark.parametrize("tool, args", [
    ("cancel_order", {"order_id": KINDLE}),
    ("start_return", {"order_id": HEADPHONES, "reason": "x"}),
])
def test_concurrent_workers_cannot_both_change_one_order(fresh_store, tool, args):
    barrier, results = threading.Barrier(8), []

    def worker():
        barrier.wait()
        results.append(tools.run(tool, dict(args)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum("error" not in r for r in results) == 1
    assert len(store.refunds()) == 1


def test_expect_is_checked_inside_the_transaction(fresh_store):
    approved = store.basis(store.get(HEADPHONES))
    store.update(HEADPHONES, price=900.00)

    result = tools.run("start_return", {"order_id": HEADPHONES, "reason": "x"},
                       op_id="REQ-aaaaaaaaaaaa", expect=approved)

    assert "348.0 -> 900.0" in result["error"]
    assert store.refunds() == {}
    # The refusal is the store's decision for this id, and it is final:
    # putting the price back does not let a retry under the same id through.
    assert store.operation("REQ-aaaaaaaaaaaa") == result
    store.update(HEADPHONES, price=348.00)
    assert tools.run("start_return", {"order_id": HEADPHONES, "reason": "x"},
                     op_id="REQ-aaaaaaaaaaaa", expect=approved) == result
    assert store.refunds() == {}


def test_the_model_cannot_pass_an_operation_id_or_expected_facts(fresh_store):
    for extra in ({"op_id": "REQ-aaaaaaaaaaaa"}, {"expect": {"price": 1}}):
        result = tools.run("cancel_order", {"order_id": KINDLE, **extra})
        assert "unexpected arguments" in result["error"] and result["retry"]
    assert store.get(KINDLE)["status"] == "preparing"


@pytest.mark.parametrize("days, ok", [(30, True), (31, False)])
def test_the_return_window_boundary(fresh_store, days, ok):
    store.update(HEADPHONES, delivered_on=(date.today() - timedelta(days=days)).isoformat())
    result = tools.start_return(HEADPHONES, "x")
    assert ("rma" in result) is ok


# --------------------------------------------------------------------------
# With approvals: the shared store is what a reviewer's yes changes
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_state):
    return approvals.default()


def proposed(work=None):
    work = work or WorkingMemory()
    work.turn = 1
    args = {"order_id": HEADPHONES, "reason": "broken"}
    policy.guarded_run("start_return", args, work)
    work.turn = 2
    return policy.guarded_run("start_return", {**args, "confirmed": True}, work)["request_id"]


def test_an_approved_return_changes_the_shared_store_once(db, fresh_store):
    rid = proposed()
    db.approve(rid, "dana")
    rec = db.resume(rid)

    assert rec["status"] == approvals.COMPLETED
    reopen()                                                       # as another process
    assert store.get(HEADPHONES)["status"] == "return started"
    assert store.refunds()[HEADPHONES]["op_id"] == rid
    assert store.operation(rid) == rec["outcome"]


def test_a_rejected_approval_does_not_touch_the_order(db, fresh_store):
    rid = proposed()
    db.reject(rid, "dana", "no receipt")
    assert store.get(HEADPHONES)["status"] == "delivered"
    assert store.refunds() == {} and store.operation(rid) is None


def test_an_expired_approval_does_not_touch_the_order(db, fresh_store, monkeypatch):
    rid = proposed()
    monkeypatch.setattr(approvals.Approvals, "_now", lambda self: "9999-12-31T00:00:00+00:00")
    with pytest.raises(approvals.Expired):
        db.approve(rid, "dana")
    assert db.sweep()["resumed"] == []
    assert store.get(HEADPHONES)["status"] == "delivered"
    assert store.refunds() == {}


def test_a_price_rise_after_approval_cannot_raise_the_refund(db, fresh_store):
    rid = proposed()
    db.approve(rid, "dana")
    store.update(HEADPHONES, price=900.00)

    rec = db.resume(rid)

    assert rec["status"] == approvals.FAILED
    assert store.refunds() == {}
    assert store.get(HEADPHONES)["status"] == "delivered"


def test_the_store_still_refuses_a_change_the_pre_check_missed(db, fresh_store, monkeypatch):
    """resume() compares the approved facts, then the tool runs. If the
    price changes in between, the store's own check inside its transaction
    is what stops it. Simulated by making the first check see nothing."""
    rid = proposed()
    db.approve(rid, "dana")
    store.update(HEADPHONES, price=900.00)
    monkeypatch.setattr(approvals, "_changed", lambda saved, args: None)

    rec = db.resume(rid)

    assert rec["status"] == approvals.FAILED
    assert "348.0 -> 900.0" in rec["outcome"]["error"]
    assert store.refunds() == {}


def test_a_crash_after_the_store_changed_can_be_reconciled_without_a_second_refund(db, fresh_store):
    """The order changed, then the worker died before writing COMPLETED:
    the request is stuck EXECUTING. The store recorded the operation under
    the request id, so a person can see it happened, and running it again
    under that id only replays it."""
    rid = proposed()
    db.approve(rid, "dana")
    args = db.get(rid)["args"]
    basis = db.get(rid)["basis"]
    with db._tx() as conn:                                          # the claim, then "crash"
        db._move(conn, rid, approvals.APPROVED, approvals.EXECUTING, "worker")
    first = tools.run("start_return", args, op_id=rid, expect=basis)

    assert db.get(rid)["status"] == approvals.EXECUTING
    assert store.operation(rid) == first
    assert tools.run("start_return", args, op_id=rid, expect=basis) == first
    assert len(store.refunds()) == 1 and len(store.returns()) == 1
