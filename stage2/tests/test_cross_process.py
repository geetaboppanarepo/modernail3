"""Approval across processes: propose in one, decide and execute in others.

Each role below runs in its own Python interpreter, which exits before the
next starts. What they share is two files: the approvals database and the
order database (AMI_ORDERS_DB), both in this test's temp folder.

What this proves:
- the request, the decision, the outcome and the audit trail persist, and
  a worker process can approve and EXECUTE the action from them
- the order change the worker made is what every later process sees
- the action runs at most once however many processes resume it, and a
  NEW request for the same action is refused in a later process
- an order changed by another process after approval is not acted on:
  the facts the reviewer approved are re-checked before anything runs
- two processes racing to return the same order produce one return
- a worker that dies after the order store committed leaves the request
  EXECUTING; recovery processes, even two at once, finish it from the
  store's record without a second refund
"""

import json
import os
import subprocess
import sys

import pytest

from ami import ROOT as STAGE, approvals

HEADPHONES = "112-1111111-1111111"

ROLE = r'''
import json, sys
from pathlib import Path
from ami import observe, approvals, policy, store, tools
from ami.memory import WorkingMemory
observe.LOGFILE = Path(sys.argv[2])            # never the real state/trace.jsonl
approvals.PATH = sys.argv[1]
role, rid = sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 else None)

executed, real = [], tools.run
def spy(name, args, **kw):
    executed.append(name)
    return real(name, args, **kw)
tools.run = spy

def customer_confirms_return():
    w = WorkingMemory(); w.turn = 1
    a = {"order_id": "112-1111111-1111111", "reason": "broken"}
    policy.guarded_run("start_return", a, w); w.turn = 2
    return policy.guarded_run("start_return", {**a, "confirmed": True}, w)

db, out = approvals.default(), {}
if role == "propose":
    out["result"] = customer_confirms_return()
elif role == "approve":
    out["listed"] = [r["id"] for r in db.list(approvals.PENDING)]
    out["record"] = db.approve(rid, "night-shift", "receipt checked")
elif role == "resume":
    out["record"] = db.resume(rid)
elif role == "sweep":
    out["sweep"] = db.sweep()
elif role == "propose_again":
    out["result"] = customer_confirms_return()
elif role == "reprice":                         # someone else edits the order
    store.update("112-1111111-1111111", price=412.00)
elif role == "crash_after_commit":            # die between the two commits
    import os
    real_move = approvals.Approvals._move
    def dying(self, conn, rid_, frm, to, actor, *a, **kw):
        if frm == "EXECUTING" and actor == "worker":
            os._exit(3)
        return real_move(self, conn, rid_, frm, to, actor, *a, **kw)
    approvals.Approvals._move = dying
    db.resume(rid)
elif role == "reconcile":
    if len(sys.argv) > 5:                        # start together
        import time
        while time.time() < float(sys.argv[5]):
            time.sleep(0.001)
    out["record"], out["diagnosis"] = db.reconcile(rid, "ops-" + role)
elif role == "read":
    out["order"] = store.get("112-1111111-1111111")
    out["returns"] = store.returns()
    out["refunds"] = store.refunds()
elif role == "return_directly":                 # no approval: the store alone decides
    import time
    start = float(rid)
    while time.time() < start:
        time.sleep(0.001)
    out["result"] = tools.run("start_return", {"order_id": "112-1111111-1111111",
                                               "reason": "race"})
out["executed"] = executed
out["store_status"] = store.get("112-1111111-1111111")["status"]
print("RESULT " + json.dumps(out))
'''

REVIEW_CLI = r'''
import runpy, sys
from pathlib import Path
from ami import observe, approvals
observe.LOGFILE = Path(sys.argv[2])
approvals.PATH = sys.argv[1]
sys.argv = [str(Path(approvals.__file__).parent.parent / "review.py"), *sys.argv[3:]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def spawn(argv, env):
    """Start a child WITHOUT fork(). Importing ami.tools loads onnxruntime,
    which leaves native threads running in this test process; forking a
    multi-threaded process can deadlock the child before exec and corrupt
    the parent (it aborted at exit in ~30% of suite runs). CPython uses
    posix_spawn instead only with no cwd and, on macOS, close_fds=False —
    safe, because Python's own descriptors are non-inheritable (PEP 446)."""
    return subprocess.run(argv, env=env, close_fds=False, capture_output=True,
                          text=True, timeout=120)


@pytest.fixture
def proc(tmp_path):
    db = tmp_path / "approvals.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AMI_")}
    env["PYTHONPATH"] = str(STAGE)
    env["AMI_ORDERS_DB"] = str(tmp_path / "orders.db")       # one order store for all of them

    def run(role, rid=None, code=ROLE):
        argv = [sys.executable, "-c", code, str(db), str(tmp_path / "trace.jsonl"), role]
        p = spawn(argv + ([rid] if rid else []), env)
        assert p.returncode == 0, p.stderr
        if code is not ROLE:
            return p.stdout
        return json.loads(next(l for l in p.stdout.splitlines()
                               if l.startswith("RESULT "))[7:])
    run.db = approvals.Approvals(db)
    run.env = env
    run.argv = lambda role, *extra: [sys.executable, "-c", ROLE, str(db),
                                     str(tmp_path / "trace.jsonl"), role, *extra]
    return run


def test_propose_then_approve_and_execute_in_separate_processes(proc):
    first = proc("propose")                                  # process 1 exits
    rid = first["result"]["request_id"]
    assert first["result"]["pending_approval"] and first["executed"] == []

    decided = proc("approve", rid)                           # process 2: decision only
    assert decided["listed"] == [rid] and decided["executed"] == []
    assert decided["record"]["status"] == "APPROVED"

    worker = proc("resume", rid)                             # process 3: executes
    assert worker["executed"] == ["start_return"]
    assert worker["record"]["status"] == "COMPLETED"
    assert worker["record"]["outcome"]["rma"].startswith("RMA-")
    assert worker["store_status"] == "return started"        # in THAT process

    # Read back by this, a fourth process: everything persisted.
    rec = proc.db.get(rid)
    assert (rec["status"], rec["execution_status"], rec["reviewer"]) == \
           ("COMPLETED", "succeeded", "night-shift")
    assert [e["to_status"] for e in proc.db.events(rid)] == [
        "RECEIVED", "DRAFTED", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "COMPLETED"]


def test_later_processes_never_execute_it_again(proc):
    rid = proc("propose")["result"]["request_id"]
    proc("approve", rid)
    assert proc("resume", rid)["executed"] == ["start_return"]

    assert proc("resume", rid)["executed"] == []             # same request again
    assert proc("sweep")["executed"] == []                   # the background job

    # A NEW request for the same action, in a fresh process. The shared
    # store knows the return has started, so its guardrail refuses it:
    # nothing new is proposed and nothing new is refunded.
    again = proc("propose_again")
    assert again["store_status"] == "return started"
    assert "not delivered" in again["result"]["error"]
    assert [r["id"] for r in proc.db.list()] == [rid]
    seen = proc("read")
    assert len(seen["returns"]) == 1 and list(seen["refunds"]) == [HEADPHONES]
    assert sum(e["to_status"] == "EXECUTING" for e in proc.db.events(rid)) == 1


def test_the_sweeper_process_executes_an_approval_made_elsewhere(proc):
    rid = proc("propose")["result"]["request_id"]
    proc("approve", rid)
    first, second = proc("sweep"), proc("sweep")

    assert first["sweep"] == {"expired": [], "resumed": [rid]}
    assert first["executed"] == ["start_return"]
    assert second["sweep"] == {"expired": [], "resumed": []} and second["executed"] == []


def test_the_review_cli_approves_from_its_own_process(proc):
    rid = proc("propose")["result"]["request_id"]

    listed = proc("list", code=REVIEW_CLI)
    assert rid in listed and "PENDING_APPROVAL" in listed
    shown = proc("approve", f"{rid}", code=REVIEW_CLI.replace(
        'sys.argv[3:]', 'sys.argv[3:], "--reviewer", "dana", "--defer"'))
    assert "APPROVED" in shown
    assert proc.db.get(rid)["status"] == "APPROVED"          # --defer: decided, not run
    assert proc("resume", rid)["executed"] == ["start_return"]


def test_the_order_store_is_shared_between_processes(proc):
    """This used to pin the opposite: each process had its own in-memory
    copy, and a return made by the worker vanished with it. Now the worker's
    change is in the shared orders.db, for every later process to see."""
    assert proc("read")["order"]["status"] == "delivered"   # seeded once, on first use
    rid = proc("propose")["result"]["request_id"]
    proc("approve", rid)
    worker = proc("resume", rid)
    assert worker["store_status"] == "return started"

    later = proc("read")                                      # a fifth process
    assert later["order"]["status"] == "return started"
    assert list(later["returns"]) == [worker["record"]["outcome"]["rma"]]
    assert later["refunds"][HEADPHONES]["op_id"] == rid      # refunded once, under REQ id
    assert later["refunds"][HEADPHONES]["amount"] == 348.00


# --------------------------------------------------------------------------
# The idempotency key, in one process
# --------------------------------------------------------------------------

def test_two_requests_for_one_action_execute_once(tmp_state, fresh_store, monkeypatch):
    """propose() dedupes open requests; resume() must hold even when two
    exist anyway (made directly here), whichever is resumed first."""
    from ami import tools
    ran, real = [], tools.run
    monkeypatch.setattr(tools, "run", lambda n, a, **kw: (ran.append(n), real(n, a, **kw))[1])
    db = approvals.Approvals(tmp_state / "a.db")
    ids = []
    for _ in range(2):
        rid = db.create("return it")
        db.submit(rid, "start_return", {"order_id": HEADPHONES, "reason": "x"}, "test")
        db.approve(rid, "dana")
        ids.append(rid)

    first, second = db.resume(ids[0]), db.resume(ids[1])

    assert ran == ["start_return"]
    assert first["execution_status"] == "succeeded"
    assert (second["status"], second["execution_status"]) == ("FAILED", "not_executed")
    assert ids[0] in second["outcome"]["error"]


def test_an_order_repriced_by_another_process_is_not_refunded(proc):
    """A reviewer approved a $348 refund. Another process then raises the
    price to $412. The worker, a third process, must not pay out $412 on
    that approval: it re-checks the approved facts and runs nothing."""
    rid = proc("propose")["result"]["request_id"]
    assert proc.db.get(rid)["basis"]["price"] == 348.00
    proc("approve", rid)
    proc("reprice")

    worker = proc("resume", rid)

    assert worker["executed"] == []
    assert worker["store_status"] == "delivered"
    rec = proc.db.get(rid)
    assert (rec["status"], rec["execution_status"]) == ("FAILED", "not_executed")
    assert "348.0 -> 412.0" in rec["outcome"]["error"]
    assert proc("sweep")["executed"] == []                   # never retried
    assert proc("read")["refunds"] == {}


def test_two_processes_racing_to_return_one_order_make_one_return(proc):
    """Both start at the same instant and call the tool directly — no
    approval, so only the store's transaction stands between them."""
    import time
    start = str(time.time() + 1.5)                           # after both have imported
    procs = [subprocess.Popen(proc.argv("return_directly", start), env=proc.env,
                              close_fds=False, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        assert p.returncode == 0, stderr
        outs.append(json.loads(next(l for l in stdout.splitlines()
                                    if l.startswith("RESULT "))[7:])["result"])

    won = [o for o in outs if "rma" in o]
    lost = [o for o in outs if "error" in o]
    assert len(won) == 1 and len(lost) == 1
    assert "return started" in lost[0]["error"]
    seen = proc("read")
    assert list(seen["returns"]) == [won[0]["rma"]]
    assert seen["refunds"][HEADPHONES]["amount"] == 348.00


def test_a_worker_that_dies_after_the_store_committed_is_recovered_once(proc):
    rid = proc("propose")["result"]["request_id"]
    proc("approve", rid)
    died = spawn(proc.argv("crash_after_commit", rid), proc.env)
    assert died.returncode == 3, died.stderr                  # really gone, mid-execution

    assert proc.db.get(rid)["status"] == "EXECUTING"
    assert proc("read")["order"]["status"] == "return started"
    assert proc("sweep")["executed"] == []                    # nothing re-runs it by itself

    import time
    start = str(time.time() + 1.5)
    racers = [subprocess.Popen(proc.argv("reconcile", rid, start), env=proc.env,
                               close_fds=False, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = []
    for r in racers:
        stdout, stderr = r.communicate(timeout=120)
        assert r.returncode == 0, stderr
        outs.append(json.loads(next(l for l in stdout.splitlines()
                                    if l.startswith("RESULT "))[7:]))

    assert all(o["executed"] == [] for o in outs)             # neither ran it again
    assert {o["record"]["status"] for o in outs} == {"COMPLETED"}
    assert outs[0]["record"]["outcome"] == outs[1]["record"]["outcome"]
    assert sum(e["from_status"] == "EXECUTING" for e in proc.db.events(rid)) == 1
    seen = proc("read")
    assert len(seen["returns"]) == 1 and list(seen["refunds"]) == [HEADPHONES]


def test_the_review_cli_lists_and_reconciles_an_interrupted_request(proc):
    rid = proc("propose")["result"]["request_id"]
    proc("approve", rid)
    assert spawn(proc.argv("crash_after_commit", rid), proc.env).returncode == 3

    cli = lambda *args: spawn([sys.executable, "-c", REVIEW_CLI, str(proc.db.path),
                               str(proc.db.path.parent / "trace.jsonl"), *args], proc.env)
    listed = cli("reconcile")
    assert listed.returncode == 0, listed.stderr
    assert rid in listed.stdout and "committed: the order store did it" in listed.stdout
    assert "1 request(s) in EXECUTING" in listed.stdout

    refused = cli("resolve", rid, "--as", "failed", "--reviewer", "sam", "--reason", "guess")
    assert refused.returncode == 2 and "run reconcile first" in refused.stderr

    done = cli("reconcile", rid, "--reviewer", "sam")
    assert done.returncode == 0, done.stderr
    assert "COMPLETED" in done.stdout
    assert proc.db.get(rid)["status"] == "COMPLETED"
    assert "0 request(s) in EXECUTING" in cli("reconcile").stdout
