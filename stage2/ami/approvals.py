"""Human-in-the-loop approval: the agent proposes, a person decides, later.

The confirmation gate in policy.py asks the CUSTOMER to say yes. This asks
a STAFF REVIEWER, and the reviewer is not in the conversation: they may
answer in ten minutes or tomorrow, from another process. So the agent must
not wait. It writes the proposal down, tells the customer it is under
review, and its turn ends. Whoever decides later finds the proposal in the
database, not in a Python variable.

Every request a customer makes moves through one lifecycle:

    RECEIVED -> DRAFTED -> COMPLETED                       no sign-off needed
    RECEIVED -> DRAFTED -> PENDING_APPROVAL
        -> APPROVED -> EXECUTING -> COMPLETED | FAILED      reviewer said yes
        -> REJECTED -> COMPLETED                            reviewer said no
        -> EXPIRED                                          nobody answered

Three rules hold no matter who calls what, or from which process:

  - a protected action runs only from APPROVED, and only after it has been
    CLAIMED (APPROVED -> EXECUTING) by a conditional update. Two workers
    resuming the same request: one wins the claim, the other runs nothing.
  - every transition is `UPDATE ... WHERE status = <expected>` inside a
    transaction, with an audit row written in the same transaction. A
    transition from the wrong state changes nothing and raises.
  - one action, one execution, across every request and every process.
    The idempotency key (tool, order_id) is checked here: when proposing,
    and again inside the claim transaction before anything runs
    (duplicate_of()). The order store (store.py, its own SQLite file)
    checks again: the request id is the store's operation id, so a repeat
    returns the first result, and it keeps at most one refund per order.
  - the reviewer approves an action on an order AS IT WAS. The order
    facts the decision rests on (price, status, delivery date) are saved
    with the proposal; the worker re-reads them from ITS order store just
    before running and, if they differ, runs nothing (FAILED, needs a
    fresh review). The store compares them once more inside the very
    transaction that would change the order.
  - a crash is recovered from the order store's record, never guessed.
    A worker that dies after claiming leaves the request EXECUTING, and
    nothing re-runs it on its own. reconcile() asks the order store what
    it decided under the request id (the store records its first decision,
    done or refused, and replays it): a decision finishes the request from
    that record; no decision, in the very store the claim used, is retried
    under the same id, which can only ever decide once. Anything it cannot
    prove — another store, a record that does not match the request — is
    flagged `unresolved` and waits for a named person (resolve()).
  - fail closed. Past its deadline a request cannot be approved, through
    the normal path or any other; it becomes EXPIRED and the action never
    runs. A crash between EXECUTING and COMPLETED leaves it EXECUTING —
    never re-run automatically, because "ran twice" is worse than "ran
    once and lost the receipt".

ESCALATION, LATER. EXPIRED is terminal today. To hand an unanswered
request to a second reviewer instead, add EXPIRED -> ESCALATED ->
PENDING_APPROVAL with a new deadline and a `reviewer_pool` column, and have
approve() check the reviewer against that pool. The executing path does not
change: it still only runs from APPROVED, so escalating can never execute
anything by itself.

Storage is SQLite from the standard library: state/approvals.db, or
$AMI_APPROVALS_DB. Settings, read at import:

    AMI_APPROVAL_TIMEOUT_MIN   minutes a reviewer has to decide (default 240)
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ami import ROOT          # the stage folder
from ami import observe
from ami import store
from ami import tools

RECEIVED = "RECEIVED"
DRAFTED = "DRAFTED"
PENDING = "PENDING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXECUTING = "EXECUTING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"

# Every legal move. Anything not listed here is refused.
TRANSITIONS = {
    RECEIVED: {DRAFTED},
    DRAFTED: {PENDING, COMPLETED},
    PENDING: {APPROVED, REJECTED, EXPIRED},
    APPROVED: {EXECUTING},
    EXECUTING: {COMPLETED, FAILED},
    REJECTED: {COMPLETED},
}
OPEN = (DRAFTED, PENDING, APPROVED, EXECUTING)        # a proposal still in flight

TIMEOUT = timedelta(minutes=float(os.getenv("AMI_APPROVAL_TIMEOUT_MIN", "240")))
PATH = None                   # set to override where the database lives

_ID = re.compile(r"^REQ-[0-9a-f]{12}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id               TEXT PRIMARY KEY,
    session_id       TEXT,
    customer_email   TEXT,
    request          TEXT NOT NULL,     -- the customer's message this turn
    context          TEXT,              -- JSON: their earlier messages in the chat
    status           TEXT NOT NULL,
    tool             TEXT,              -- the proposed action ...
    args             TEXT,              -- ... and its arguments, JSON
    policy_reason    TEXT,              -- why policy wanted a human
    basis            TEXT,              -- JSON: the order facts it was proposed on
    orders_store     TEXT,              -- store.identity() of the store it ran against
    resolution       TEXT,              -- what the agent told the customer
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    deadline         TEXT,
    reviewer         TEXT,
    decision         TEXT,              -- approved | rejected
    decision_reason  TEXT,
    decided_at       TEXT,
    execution_status TEXT NOT NULL,     -- not_required | waiting | running
                                        -- | succeeded | failed | not_executed
    outcome          TEXT,              -- JSON: what the tool returned
    notified         INTEGER NOT NULL DEFAULT 0   -- outcome told to the session
);
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    at          TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id);
"""


class ApprovalError(Exception):
    """Base class. `code` is what an HTTP layer should answer with."""
    code = 400


class NotFound(ApprovalError):
    code = 404


class InvalidTransition(ApprovalError):
    code = 409


class Expired(InvalidTransition):
    pass


def _utc():
    return datetime.now(timezone.utc)


BASIS = store.BASIS                             # what the approval decision rests on


def order_basis(args):
    """The order as the store has it now, reduced to BASIS.
    None when there is no such order."""
    return store.basis(store.get((args or {}).get("order_id", "")))


def _changed(saved, args):
    """How the order differs from what the reviewer approved, or None.
    No saved basis (a request older than the column) counts as changed:
    nothing runs on facts nobody recorded."""
    then, now = json.loads(saved or "null"), order_basis(args)
    if then is None:
        return "no record of the order as it was proposed"
    if now is None:
        return "no such order here"
    diffs = [f"{k} {then.get(k)!r} -> {now.get(k)!r}" for k in BASIS if then.get(k) != now.get(k)]
    return ", ".join(diffs) or None


class Approvals:
    """The approval store. Cheap to construct: every call opens its own
    connection, so web threads, the sweeper and a CLI in another process
    can all use the same file safely."""

    def __init__(self, path=None, clock=_utc, timeout=None):
        self.path = Path(path or os.getenv("AMI_APPROVALS_DB")
                         or ROOT / "state" / "approvals.db")
        self.clock = clock                   # injectable, so tests never sleep
        self.timeout = timeout or TIMEOUT
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
            for col in ("basis", "orders_store"):    # a database made before the column
                if col not in cols:
                    conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT")

    # -- plumbing ----------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self):
        """One write transaction. IMMEDIATE takes the write lock up front, so
        a read-check-write inside it cannot interleave with another process."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _now(self):
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _row(self, conn, rid):
        if not isinstance(rid, str) or not _ID.match(rid):
            raise ApprovalError(f"Not a request id: {rid!r}")
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise NotFound(f"No request {rid}")
        return row

    def _move(self, conn, rid, frm, to, actor, detail=None, **fields):
        """The only way a status changes: conditional on the current one."""
        if to not in TRANSITIONS.get(frm, ()):
            raise InvalidTransition(f"{rid}: {frm} -> {to} is not allowed")
        now = self._now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(
            f"UPDATE requests SET status = ?, updated_at = ?{', ' + sets if sets else ''} "
            f"WHERE id = ? AND status = ?",
            (to, now, *fields.values(), rid, frm))
        if cur.rowcount != 1:
            raise InvalidTransition(f"{rid} is no longer {frm}")
        conn.execute("INSERT INTO events (request_id, at, from_status, to_status, actor, detail) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (rid, now, frm, to, actor, detail))
        observe.log("policy", stage="approval", request=rid, transition=f"{frm}->{to}",
                    actor=actor)

    @staticmethod
    def _dict(row):
        d = dict(row)
        for k in ("args", "outcome", "context", "basis"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    # -- the lifecycle -----------------------------------------------------

    def create(self, request, session_id=None, context=None):
        """A customer request arrives. Returns its id, in RECEIVED."""
        rid = "REQ-" + uuid.uuid4().hex[:12]
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO requests (id, session_id, request, context, status, "
                "created_at, updated_at, execution_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, session_id, request, json.dumps(context or []), RECEIVED,
                 now, now, "not_required"))
            conn.execute("INSERT INTO events (request_id, at, to_status, actor) "
                         "VALUES (?, ?, ?, ?)", (rid, now, RECEIVED, "customer"))
        return rid

    def submit(self, rid, tool, args, reason, email=None):
        """The agent drafted an action policy will not run unsupervised:
        record it and open the approval window. Nothing executes here."""
        deadline = (self.clock() + self.timeout).astimezone(timezone.utc) \
            .isoformat(timespec="seconds")
        with self._tx() as conn:
            row = self._row(conn, rid)
            self._move(conn, rid, row["status"], DRAFTED, "agent",
                       f"proposed {tool}", tool=tool, args=json.dumps(args),
                       policy_reason=reason, customer_email=email,
                       basis=json.dumps(order_basis(args)))
            self._move(conn, rid, DRAFTED, PENDING, "policy", reason,
                       deadline=deadline, execution_status="waiting")
        return self.get(rid)

    def finish(self, rid, resolution):
        """End of the agent's turn. A request that proposed nothing protected
        is done; one waiting on a reviewer just keeps the reply for the audit."""
        with self._tx() as conn:
            row = self._row(conn, rid)
            conn.execute("UPDATE requests SET resolution = ? WHERE id = ?", (resolution, rid))
            if row["status"] == RECEIVED:
                self._move(conn, rid, RECEIVED, DRAFTED, "agent")
                self._move(conn, rid, DRAFTED, COMPLETED, "policy", "no approval required")
        return self.get(rid)

    def approve(self, rid, reviewer, reason=None):
        """Record a yes. Does NOT run the action — resume() does, so that a
        decision and an execution are two separate, separately safe steps."""
        return self._decide(rid, reviewer, reason, APPROVED, "approved")

    def reject(self, rid, reviewer, reason=None):
        """Record a no, and close the request without running anything."""
        self._decide(rid, reviewer, reason, REJECTED, "rejected")
        with self._tx() as conn:
            self._move(conn, rid, REJECTED, COMPLETED, "system", "rejected; not executed",
                       execution_status="not_executed")
        return self.get(rid)

    def _decide(self, rid, reviewer, reason, to, decision):
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ApprovalError("A decision needs the reviewer's name.")
        expired = False
        with self._tx() as conn:
            row = self._row(conn, rid)
            if row["status"] != PENDING:
                raise InvalidTransition(f"{rid} is {row['status']}, not {PENDING}")
            if self._now() >= row["deadline"]:
                # Fail closed: a late answer is no answer. Record that it
                # expired, in this same transaction, and refuse.
                self._move(conn, rid, PENDING, EXPIRED, "system",
                           f"deadline passed; late {decision} by {reviewer.strip()} refused",
                           execution_status="not_executed")
                expired = True
            else:
                self._move(conn, rid, PENDING, to, reviewer.strip(), reason,
                           reviewer=reviewer.strip(), decision=decision,
                           decision_reason=reason, decided_at=self._now())
        if expired:
            raise Expired(f"{rid} expired at {row['deadline']}; it cannot be {decision}")
        return self.get(rid)

    def resume(self, rid):
        """Run an approved action, exactly once. Safe to call repeatedly,
        from any number of workers: whoever loses the claim runs nothing
        and gets the record back as it stands."""
        orders_store = store.identity()                  # which store this runs against
        with self._tx() as conn:
            row = self._row(conn, rid)
            if row["status"] in (EXECUTING, COMPLETED, FAILED) and row["decision"] == "approved":
                return self._dict(row)                    # already claimed or done
            self._move(conn, rid, row["status"], EXECUTING, "worker",
                       execution_status="running",       # raises unless APPROVED
                       orders_store=orders_store)
            # Same transaction as the claim, so two requests for one action
            # cannot both pass this check: the second sees the first EXECUTING.
            dup = self._duplicate_of(conn, rid, row["tool"], json.loads(row["args"]))
        tool, args = row["tool"], json.loads(row["args"])
        stale = None if dup else _changed(row["basis"], args)
        if dup:
            result = {"error": f"Not run: {tool} on order {args.get('order_id')} was "
                               f"already executed under {dup}."}
        elif stale:
            result = {"error": f"Not run: order {args.get('order_id')} is not as it was "
                               f"when this was approved ({stale}). It needs a new "
                               f"request and a fresh review."}
        else:
            try:
                # The request id is the operation id: if this ever runs again
                # the store answers with the first result and changes nothing.
                # `expect` re-checks the approved facts inside the store's own
                # transaction, closing the gap since _changed() looked.
                result = tools.run(tool, args, op_id=rid, expect=json.loads(row["basis"]))
            except Exception as e:                       # tools.run should not raise
                result = {"error": f"{type(e).__name__}: {e}"}
            # What the store recorded under this id is what happened, even
            # if something failed on the way back from it.
            recorded = store.operation(rid)
            if recorded is not None:
                result = recorded
        ok = "error" not in result
        try:
            with self._tx() as conn:
                self._move(conn, rid, EXECUTING, COMPLETED if ok else FAILED, "worker",
                           None if ok else result["error"],
                           execution_status="succeeded" if ok else
                           ("not_executed" if dup or stale else "failed"),
                           outcome=json.dumps(result))
        except InvalidTransition:
            pass                     # reconcile() finished it, from the same store record
        return self.get(rid)

    # -- recovering from a crash mid-execution -----------------------------

    def diagnose(self, rid):
        """What the order store can prove about a request left EXECUTING.
        Reads only. Returns {"state", "why", "operation"}, state one of:

            not_executing  nothing to recover (not EXECUTING)
            committed      the store did it under this id: here is the result
            refused        the store refused it under this id: nothing changed
            not_committed  this store has no decision under this id yet
            unresolved     the facts do not establish what happened
        """
        r = self.get(rid)
        if r["status"] != EXECUTING:
            return {"state": "not_executing", "operation": None,
                    "why": f"{rid} is {r['status']}; there is nothing to recover"}

        def unresolved(why):
            return {"state": "unresolved", "why": why, "operation": None}

        here = store.identity()
        if not r.get("orders_store"):
            return unresolved("claimed before the order store it ran against was "
                              "recorded; cannot tell which store to ask")
        if r["orders_store"] != here:
            return unresolved(f"it ran against order store {r['orders_store']}, but "
                              f"{store.path()} is {here}: a different database")
        op = store.operation_record(rid)
        if op is None:
            if not r.get("basis"):
                return unresolved("no decision recorded, and no approved order facts "
                                  "to re-check a retry against")
            return {"state": "not_committed", "operation": None,
                    "why": "the order store has made no decision under this id"}
        order_id = str((r["args"] or {}).get("order_id", "")).strip()
        if (op["tool"], op["order_id"]) != (r["tool"], order_id):
            return unresolved(f"the store's operation {rid} is {op['tool']} on "
                              f"{op['order_id']}, not {r['tool']} on {order_id}")
        done = "error" not in op["result"]
        if done != (op["refund"] is not None):
            return unresolved("the store's result and its refund ledger disagree "
                              f"(result {'done' if done else 'refused'}, refund "
                              f"{'present' if op['refund'] else 'absent'})")
        return {"state": "committed" if done else "refused", "operation": op,
                "why": f"the order store {'did' if done else 'refused'} it under "
                       f"{rid} at {op['at']}"}

    def reconcile(self, rid, operator):
        """Finish a request a crashed worker left EXECUTING, from the order
        store's record. Returns (request, diagnosis). Safe to run any number
        of times, from any number of processes:

          committed / refused  finished from the store's record; not run again
          not_committed        run under the same id: the store decides once,
                               and anyone racing us gets that same decision
          unresolved           flagged, left EXECUTING, for resolve()

        Every finisher reads the same store record, so two of them cannot
        finish one request differently; the loser's transition just fails."""
        if not isinstance(operator, str) or not operator.strip():
            raise ApprovalError("Reconciling needs the operator's name.")
        actor = f"recovery:{operator.strip()}"
        d = self.diagnose(rid)
        retried = False
        if d["state"] == "not_committed":
            r = self.get(rid)
            try:
                tools.run(r["tool"], r["args"], op_id=rid, expect=r["basis"])
            except Exception:                         # the record below decides
                pass
            retried, d = True, self.diagnose(rid)
            if d["state"] == "not_committed":         # it never reached the store
                d = {**d, "state": "unresolved",
                     "why": "retried, and the store still has no decision under this id"}
        if d["state"] == "unresolved":
            self._flag(rid, actor, d["why"])
        elif d["state"] in ("committed", "refused"):
            result, ok = d["operation"]["result"], d["state"] == "committed"
            how = "retried under the same id" if retried else "found in the order store; not run again"
            try:
                with self._tx() as conn:
                    self._move(conn, rid, EXECUTING, COMPLETED if ok else FAILED, actor,
                               f"recovered: {how}" + ("" if ok else f": {result['error']}"),
                               execution_status="succeeded" if ok else "failed",
                               outcome=json.dumps(result))
            except InvalidTransition:
                pass                                  # someone else finished it first
        return self.get(rid), d

    def _flag(self, rid, actor, why):
        """Mark an EXECUTING request unresolved, once, with the reason in
        the audit trail. Its status does not change."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE requests SET execution_status = 'unresolved', "
                               "updated_at = ? WHERE id = ? AND status = ? AND "
                               "execution_status != 'unresolved'", (self._now(), rid, EXECUTING))
            if cur.rowcount == 1:
                conn.execute("INSERT INTO events (request_id, at, from_status, to_status, "
                             "actor, detail) VALUES (?, ?, ?, ?, ?, ?)",
                             (rid, self._now(), EXECUTING, EXECUTING, actor,
                              f"unresolved: {why}"))

    def resolve(self, rid, operator, outcome, reason):
        """A person closes an `unresolved` request after checking for
        themselves whether the refund happened. Nothing runs."""
        if outcome not in ("completed", "failed"):
            raise ApprovalError("outcome must be 'completed' or 'failed'")
        if not isinstance(operator, str) or not operator.strip():
            raise ApprovalError("Resolving needs the operator's name.")
        if not isinstance(reason, str) or not reason.strip():
            raise ApprovalError("Resolving needs a reason.")
        with self._tx() as conn:
            row = self._row(conn, rid)
            if row["status"] != EXECUTING or row["execution_status"] != "unresolved":
                raise InvalidTransition(f"{rid} is {row['status']}/{row['execution_status']}; "
                                        f"only an unresolved EXECUTING request can be resolved "
                                        f"by hand (run reconcile first)")
            ok = outcome == "completed"
            self._move(conn, rid, EXECUTING, COMPLETED if ok else FAILED,
                       f"operator:{operator.strip()}", f"resolved by hand as {outcome}: {reason}",
                       execution_status="succeeded" if ok else "failed",
                       outcome=json.dumps({"resolved_by": operator.strip(), "as": outcome,
                                           "reason": reason} if ok else
                                          {"error": f"resolved by hand as failed: {reason}",
                                           "resolved_by": operator.strip()}))
        return self.get(rid)

    def stuck(self):
        """Every request in EXECUTING, oldest first."""
        return sorted(self.list(EXECUTING), key=lambda r: r["updated_at"])

    @staticmethod
    def _duplicate_of(conn, rid, tool, args):
        """Another request that already ran, or is running, this same action."""
        for r in conn.execute(
                "SELECT id, args FROM requests WHERE id != ? AND tool = ? AND "
                "(status = ? OR execution_status = 'succeeded')", (rid, tool, EXECUTING)):
            if json.loads(r["args"]).get("order_id") == args.get("order_id"):
                return r["id"]
        return None

    def duplicate_of(self, tool, order_id):
        conn = self._connect()
        try:
            return self._duplicate_of(conn, "", tool, {"order_id": order_id})
        finally:
            conn.close()

    def expire_overdue(self):
        """PENDING past its deadline -> EXPIRED. Returns the ids it expired."""
        now = self._now()
        with self._tx() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM requests WHERE status = ? AND deadline <= ?", (PENDING, now))]
            for rid in ids:
                self._move(conn, rid, PENDING, EXPIRED, "system",
                           "no reviewer decision before the deadline",
                           execution_status="not_executed")
        return ids

    def sweep(self):
        """The background job: expire what is overdue, run what is approved."""
        expired = self.expire_overdue()
        resumed = []
        for r in self.list(APPROVED):
            try:
                resumed.append(self.resume(r["id"])["id"])
            except InvalidTransition:
                pass                                  # another worker got it first
        return {"expired": expired, "resumed": resumed}

    # -- reading -----------------------------------------------------------

    def get(self, rid):
        conn = self._connect()
        try:
            return self._dict(self._row(conn, rid))
        finally:
            conn.close()

    def events(self, rid):
        conn = self._connect()
        try:
            self._row(conn, rid)
            return [dict(e) for e in conn.execute(
                "SELECT at, from_status, to_status, actor, detail FROM events "
                "WHERE request_id = ? ORDER BY seq", (rid,))]
        finally:
            conn.close()

    def list(self, status=None, session_id=None, proposals_only=True):
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if proposals_only:
            where.append("tool IS NOT NULL")
        sql = "SELECT * FROM requests" + (" WHERE " + " AND ".join(where) if where else "") \
              + " ORDER BY created_at, id"
        conn = self._connect()
        try:
            rows = [self._dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()
        now = self._now()
        for r in rows:
            r["overdue"] = r["status"] == PENDING and now >= r["deadline"]
        return rows

    def find_open(self, tool, order_id):
        """A proposal for this same action that is still in flight, if any."""
        for r in self.list():
            if r["status"] in OPEN and r["tool"] == tool \
                    and (r["args"] or {}).get("order_id") == order_id:
                return r
        return None

    def mark_notified(self, rid):
        with self._tx() as conn:
            conn.execute("UPDATE requests SET notified = 1 WHERE id = ?", (rid,))


def default():
    """The store at state/approvals.db (or PATH / $AMI_APPROVALS_DB)."""
    return Approvals(PATH)


# --------------------------------------------------------------------------
# The agent side: one request per customer turn
# --------------------------------------------------------------------------

# Which request the current thread's turn belongs to. Same trick as
# observe.context: the policy layer needs it without every function between
# web.py and guarded_run growing a new argument.
_ctx = threading.local()


def begin(text, session_id=None, context=None):
    """A turn starts: record the request durably, before the agent runs."""
    rid = default().create(text, session_id, context)
    _ctx.request, _ctx.text, _ctx.session, _ctx.context = rid, text, session_id, context
    return rid


def finish(rid, resolution):
    """A turn ends. Returns the request as it now stands."""
    _ctx.request = None
    return default().finish(rid, resolution)


def propose(tool, args, reason, work):
    """Called by policy.guarded_run instead of running the tool. Persists the
    proposal and returns at once with what the model should tell the customer.

    Outside a turn (a unit test, an eval) there is no request yet, so one is
    made here — the gate must fail closed even when nobody called begin()."""
    db = default()
    done = db.duplicate_of(tool, args.get("order_id"))
    if done:
        # A policy refusal, not a retry: the model tells the customer.
        observe.log("policy", stage="approval", rule="already_executed", request=done)
        return {"error": f"This {tool} on order {args.get('order_id')} was already "
                         f"approved and carried out (request {done}). It cannot be "
                         f"done a second time."}
    open_ = db.find_open(tool, args.get("order_id"))
    if open_:
        return {"pending_approval": True, "request_id": open_["id"],
                "status": open_["status"],
                "message": f"This exact {tool} is already with a human reviewer as "
                           f"{open_['id']} ({open_['status']}). Do not call it again; "
                           f"tell the customer it is still under review."}

    rid = getattr(_ctx, "request", None)
    if rid is None or db.get(rid)["status"] != RECEIVED:
        rid = db.create(getattr(_ctx, "text", None) or f"(agent) {tool} {json.dumps(args)}",
                        getattr(_ctx, "session", None) or getattr(work, "session_id", None),
                        getattr(_ctx, "context", None))
    rec = db.submit(rid, tool, args, reason, email=getattr(work, "customer_email", None))
    return {"pending_approval": True, "request_id": rid, "status": rec["status"],
            "deadline": rec["deadline"],
            "message": f"Not done yet: a human reviewer must approve this first "
                       f"({reason}). It is saved as request {rid}; a reviewer will "
                       f"decide by {rec['deadline']} UTC. Tell the customer exactly "
                       f"that, give them the request number, and do NOT say it is "
                       f"done. Do not call {tool} again for this order."}


def sync(work, session_id):
    """Before a turn: fold anything a reviewer decided since the last turn
    into working memory, and return a note so the agent can tell the
    customer. Executed actions go through work.record() like any other
    tool observation, so check_output trusts the RMA it produced."""
    if not session_id:
        return None
    db = default()
    lines = []
    for r in db.list(session_id=session_id):
        what = f"{r['tool']} on order {(r['args'] or {}).get('order_id', '?')}"
        if r["status"] in OPEN:
            lines.append(f"- {r['id']}: {what} is {r['status']} (decision due by "
                         f"{r['deadline']} UTC). Nothing has changed yet.")
            continue
        if r["notified"]:
            continue
        if r["execution_status"] in ("succeeded", "failed"):
            work.record(r["tool"], r["args"], r["outcome"])
        if r["execution_status"] == "succeeded":
            lines.append(f"- {r['id']}: APPROVED by a reviewer and done: {json.dumps(r['outcome'])}")
        elif r["status"] == FAILED:
            lines.append(f"- {r['id']}: approved, but the action was refused when it "
                         f"ran: {r['outcome'].get('error')}")
        elif r["decision"] == "rejected":
            lines.append(f"- {r['id']}: REJECTED by a reviewer"
                         f"{': ' + r['decision_reason'] if r['decision_reason'] else ''}. "
                         f"{what} was NOT done.")
        elif r["status"] == EXPIRED:
            lines.append(f"- {r['id']}: no reviewer decided in time, so {what} was NOT "
                         f"done. Offer to escalate to a human.")
        db.mark_notified(r["id"])
    if not lines:
        return None
    return "REQUESTS SENT FOR HUMAN APPROVAL IN THIS CONVERSATION:\n" + "\n".join(lines)


# --------------------------------------------------------------------------
# The customer's view: their own requests, nothing internal
# --------------------------------------------------------------------------

_ACTION = {"cancel_order": "Cancellation", "start_return": "Return"}


def customer_view(r):
    """What a customer may see of their own request. Not the tool arguments,
    the policy reason, the reviewer, or the reviewer's reason."""
    if r["status"] in (DRAFTED, PENDING):
        label = "Waiting for staff approval"
    elif r["status"] in (APPROVED, EXECUTING):
        label = "Approved, being processed"
    elif r["status"] == FAILED:
        label = "Approved, but it could not be completed"
    elif r["status"] == EXPIRED:
        label = "Not reviewed in time, so nothing was changed"
    elif r["decision"] == "rejected":
        label = "Not approved, so nothing was changed"
    elif r["execution_status"] == "succeeded":
        label = "Approved and done"
    else:
        label = r["status"].replace("_", " ").capitalize()
    return {"id": r["id"], "status": r["status"], "label": label,
            "action": _ACTION.get(r["tool"], "Request"),
            "order_id": (r["args"] or {}).get("order_id"),
            "deadline": r["deadline"] if r["status"] in (DRAFTED, PENDING) else None}


def for_customer(session_id):
    """Every approval request this session made, customer-safe. An empty
    session id matches nothing — never everyone's."""
    if not session_id:
        return []
    return [customer_view(r) for r in default().list(session_id=session_id)]


# --------------------------------------------------------------------------
# The reviewer interface, transport-agnostic. web.py maps HTTP onto it and
# review.py maps a command line onto the Approvals methods directly.
# --------------------------------------------------------------------------

def api(method, path, body=None, query=None, db=None):
    """Returns (http_status, json_body). Authentication is the caller's job.

        GET  /approvals[?status=...]        list (default: PENDING_APPROVAL)
        GET  /approvals/<id>                one request, with its audit trail
        POST /approvals/<id>/approve        {"reviewer", "reason"?} then resume
        POST /approvals/<id>/reject         {"reviewer", "reason"?}
        POST /approvals/<id>/resume         run an approved action (idempotent)
        POST /approvals/sweep               expire overdue, resume approved
    """
    db = db or default()
    body, query = body or {}, query or {}
    parts = [p for p in path.split("/") if p]
    try:
        if method == "GET" and parts == ["approvals"]:
            status = query.get("status", PENDING)
            if status != "all" and status not in (RECEIVED, DRAFTED, PENDING, APPROVED,
                                                   REJECTED, EXECUTING, COMPLETED,
                                                   FAILED, EXPIRED):
                raise ApprovalError(f"Unknown status {status!r}")
            return 200, {"requests": db.list(None if status == "all" else status)}
        if method == "POST" and parts == ["approvals", "sweep"]:
            return 200, db.sweep()
        if len(parts) == 2 and parts[0] == "approvals" and method == "GET":
            return 200, {**db.get(parts[1]), "events": db.events(parts[1])}
        if len(parts) == 3 and parts[0] == "approvals" and method == "POST":
            rid, action = parts[1], parts[2]
            if action == "approve":
                db.approve(rid, body.get("reviewer"), body.get("reason"))
                return 200, db.resume(rid)
            if action == "reject":
                return 200, db.reject(rid, body.get("reviewer"), body.get("reason"))
            if action == "resume":
                return 200, db.resume(rid)
        return 404, {"error": f"No route {method} {path}"}
    except ApprovalError as e:
        return e.code, {"error": str(e)}
