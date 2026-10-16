"""A pretend Amazon database, in SQLite.

Real support tools would hit a real API. For class we keep a handful of
orders in a small SQLite file so the tools have something honest to return
— and so that every process (web.py, review.py, a worker, the evals) sees
the SAME orders. The file is state/orders.db, or $AMI_ORDERS_DB.

    python3 -m ami.store                show every order
    python3 -m ami.store --backup FILE  copy the database, safely, while in use
    python3 -m ami.store --reset        back it up, then re-seed the fixtures

The fixture orders are written once, when the file is first created, dated
relative to that day. Opening an existing file never re-seeds it or
overwrites an order; only --reset does, and it keeps a backup.

Changing an order has one door: change(). In ONE write transaction it
reads the order as it is now, runs the tool's guardrail on that, updates
the status only if nobody changed it meanwhile, and writes the refund and
the operation record. So:

  - two processes cancelling or returning the same order: one wins, the
    other gets the guardrail's refusal ("already cancelled")
  - a refund is a row in `refunds`, keyed by order: at most one per order,
    ever, whichever path asked for it
  - an operation id (the approval request id, REQ-...) makes a retry
    harmless: the store's FIRST decision for that id — done, or refused —
    is recorded, and every later call with the id gets it back unchanged.
    So "no record under this id" means nothing was decided yet, in this
    database; identity() names the database, so a different file cannot
    pass for it
  - `expect` (the order facts a reviewer approved) is compared inside the
    same transaction, so an order repriced a moment before cannot slip a
    bigger refund through an old approval

That is local idempotency. A real refund would also call a payment
provider, outside this transaction; exactly-once there needs the provider
to honour the same operation id as an idempotency key.
"""

import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ami import ROOT          # the stage folder

TODAY = date.today()
RETURN_WINDOW_DAYS = 30
PATH = None                   # set to override where the database lives

BASIS = ("price", "status", "delivered_on")     # what an approval decision rests on


def _days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def fixtures():
    """The teaching orders, dated relative to today."""
    return {
        "112-1111111-1111111": {
            "order_id": "112-1111111-1111111",
            "email": "raj@example.com",
            "item": "Sony WH-1000XM5 Headphones",
            "price": 348.00,
            "status": "delivered",
            "ordered_on": _days_ago(9),
            "delivered_on": _days_ago(4),
            "carrier": "UPS",
            "tracking": [
                (_days_ago(8), "Shipped from Newark, NJ"),
                (_days_ago(6), "Arrived at facility, Columbus, OH"),
                (_days_ago(4), "Delivered — left at front door"),
            ],
        },
        "112-2222222-2222222": {
            "order_id": "112-2222222-2222222",
            "email": "raj@example.com",
            "item": "Instant Pot Duo 6qt",
            "price": 89.99,
            "status": "shipped",
            "ordered_on": _days_ago(2),
            "delivered_on": None,
            "eta": (TODAY + timedelta(days=1)).isoformat(),
            "carrier": "Amazon Logistics",
            "tracking": [
                (_days_ago(2), "Order placed"),
                (_days_ago(1), "Shipped from Edison, NJ"),
            ],
        },
        "112-3333333-3333333": {
            "order_id": "112-3333333-3333333",
            "email": "mei@example.com",
            "item": "Kindle Paperwhite 16GB",
            "price": 149.99,
            "status": "preparing",   # not shipped yet — still cancellable
            "ordered_on": _days_ago(0),
            "delivered_on": None,
            "eta": (TODAY + timedelta(days=4)).isoformat(),
            "carrier": None,
            "tracking": [(_days_ago(0), "Order placed")],
        },
        "112-4444444-4444444": {
            "order_id": "112-4444444-4444444",
            "email": "mei@example.com",
            "item": "Logitech MX Master 3S",
            "price": 99.99,
            "status": "delivered",
            "ordered_on": _days_ago(70),   # too old to return
            "delivered_on": _days_ago(64),
            "carrier": "USPS",
            "tracking": [(_days_ago(64), "Delivered — handed to resident")],
        },
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    item         TEXT NOT NULL,
    price        REAL NOT NULL,
    status       TEXT NOT NULL,
    ordered_on   TEXT,
    delivered_on TEXT,
    eta          TEXT,
    carrier      TEXT,
    tracking     TEXT NOT NULL DEFAULT '[]'      -- JSON: [[date, event], ...]
);
CREATE TABLE IF NOT EXISTS returns (
    id        INTEGER PRIMARY KEY AUTOINCREMENT, -- RMA-<1000 + id>
    order_id  TEXT NOT NULL,
    reason    TEXT,
    op_id     TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    order_id  TEXT PRIMARY KEY,                  -- one refund per order, ever
    op_id     TEXT NOT NULL,
    kind      TEXT NOT NULL,                     -- cancel_order | start_return
    amount    REAL NOT NULL,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    op_id     TEXT PRIMARY KEY,                  -- REQ-... for an approved action
    tool      TEXT NOT NULL,
    order_id  TEXT NOT NULL,
    result    TEXT NOT NULL,                     -- JSON: what the tool returned
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_COLUMNS = ("order_id", "email", "item", "price", "status", "ordered_on",
            "delivered_on", "eta", "carrier", "tracking")
_READY = set()


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def path():
    return Path(PATH or os.getenv("AMI_ORDERS_DB") or ROOT / "state" / "orders.db")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect():
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if str(p) not in _READY:
        conn.executescript(_SCHEMA)
        _seed_once(conn)
        _READY.add(str(p))
    return conn


@contextmanager
def _tx():
    """One write transaction. IMMEDIATE takes the write lock up front, so a
    read-check-write inside it cannot interleave with another process."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _seed_once(conn):
    """Write the fixtures into a NEW database. A database that was ever
    seeded (or already holds orders) is left exactly as it is."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        seeded = conn.execute("SELECT 1 FROM meta WHERE key = 'seeded_on'").fetchone()
        empty = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        if not seeded:
            if empty:
                for o in fixtures().values():
                    _insert(conn, o)
            conn.execute("INSERT INTO meta VALUES ('seeded_on', ?)", (_now(),))
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('store_id', ?)",
                     ("ORD-" + uuid.uuid4().hex[:12],))     # names this database, once
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _insert(conn, order, replace=False):
    row = {k: order.get(k) for k in _COLUMNS}
    row["tracking"] = json.dumps(order.get("tracking") or [])
    conn.execute(f"INSERT {'OR REPLACE ' if replace else ''}INTO orders "
                 f"({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
                 tuple(row.values()))


def _order(row):
    if row is None:
        return None
    order = dict(row)
    order["tracking"] = [tuple(e) for e in json.loads(order["tracking"])]
    return order


def _with(fn):
    conn = _connect()
    try:
        return fn(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def get(order_id):
    """One order as it is now, or None."""
    oid = str(order_id or "").strip()
    return _with(lambda c: _order(c.execute(
        "SELECT * FROM orders WHERE order_id = ?", (oid,)).fetchone()))


def find(email):
    """Every order for this email address."""
    e = str(email or "").strip().lower()
    return _with(lambda c: [_order(r) for r in c.execute(
        "SELECT * FROM orders WHERE lower(email) = ? ORDER BY order_id", (e,))])


def orders():
    """Every order, by id."""
    return _with(lambda c: {r["order_id"]: _order(r) for r in c.execute(
        "SELECT * FROM orders ORDER BY order_id")})


def returns():
    """Every return, by RMA number."""
    return _with(lambda c: {f"RMA-{1000 + r['id']}": dict(r) for r in c.execute(
        "SELECT * FROM returns ORDER BY id")})


def refunds():
    """The local refund ledger, by order id."""
    return _with(lambda c: {r["order_id"]: dict(r) for r in c.execute(
        "SELECT * FROM refunds")})


def operation(op_id):
    """What an operation returned, if it ran; else None."""
    row = _with(lambda c: c.execute(
        "SELECT result FROM operations WHERE op_id = ?", (op_id,)).fetchone())
    return json.loads(row["result"]) if row else None


def identity():
    """This database's id, made when it was first opened. A copy made with
    backup() keeps it; a re-seeded or different file has another."""
    return _with(lambda c: c.execute(
        "SELECT value FROM meta WHERE key = 'store_id'").fetchone()["value"])


def operation_record(op_id):
    """Everything the store holds under an operation id, or None if it
    never decided one: the tool, the order, what it returned, and the
    refund written under the same id (None for a refusal)."""
    def read(c):
        op = c.execute("SELECT * FROM operations WHERE op_id = ?", (op_id,)).fetchone()
        if op is None:
            return None
        refund = c.execute("SELECT * FROM refunds WHERE op_id = ?", (op_id,)).fetchone()
        return {**dict(op), "result": json.loads(op["result"]),
                "refund": dict(refund) if refund else None}
    return _with(read)


def basis(order):
    """The facts an approval decision rests on, or None for no order."""
    return {k: order.get(k) for k in BASIS} if order else None


# --------------------------------------------------------------------------
# changing
# --------------------------------------------------------------------------

def change(order_id, *, tool, check, to, result, op_id=None, expect=None,
           return_reason=None):
    """The one way an order changes. All in one write transaction:

        op_id seen before      -> that call's result again; nothing changes
        expect given, differs  -> refused (the order is not what was approved)
        check(id, order) says  -> refused (the tool's own guardrail)
        otherwise              -> status := to, one refund row, a return row
                                  if return_reason, the operation recorded,
                                  and result(order_before, rma) returned

    With an op_id, a refusal is recorded too: the first decision is final.
    The refund amount is the stored price, read inside the transaction."""
    oid = str(order_id or "").strip()
    given = op_id

    def refuse(conn, message):
        out = {"error": message}
        if given:
            conn.execute("INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, ?)",
                         (given, tool, oid, json.dumps(out), _now()))
        return out

    try:
        with _tx() as conn:
            if op_id:
                done = conn.execute("SELECT result FROM operations WHERE op_id = ?",
                                    (op_id,)).fetchone()
                if done:
                    return json.loads(done["result"])
            order = _order(conn.execute("SELECT * FROM orders WHERE order_id = ?",
                                        (oid,)).fetchone())
            if expect is not None and order is not None and basis(order) != expect:
                diffs = ", ".join(f"{k} {expect.get(k)!r} -> {order.get(k)!r}"
                                  for k in BASIS if expect.get(k) != order.get(k))
                return refuse(conn, f"Order {oid} changed since this was approved "
                                    f"({diffs}); nothing was done.")
            refused = check(order_id, order)
            if refused:
                return refuse(conn, refused)

            cur = conn.execute("UPDATE orders SET status = ? WHERE order_id = ? AND status = ?",
                               (to, oid, order["status"]))
            if cur.rowcount != 1:                        # cannot happen under IMMEDIATE
                raise sqlite3.IntegrityError(f"order {oid} changed underneath")
            op_id = op_id or "OP-" + uuid.uuid4().hex[:12]
            now = _now()
            conn.execute("INSERT INTO refunds VALUES (?, ?, ?, ?, ?)",
                         (oid, op_id, tool, order["price"], now))
            rma = None
            if return_reason is not None:
                cur = conn.execute("INSERT INTO returns (order_id, reason, op_id, at) "
                                   "VALUES (?, ?, ?, ?)", (oid, return_reason, op_id, now))
                rma = f"RMA-{1000 + cur.lastrowid}"
            out = result(order, rma)
            conn.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?)",
                         (op_id, tool, oid, json.dumps(out), now))
            return out
    except sqlite3.IntegrityError:                       # rolled back; record why
        with _tx() as conn:
            return refuse(conn, f"Order {oid} has already been refunded; nothing was done.")


def put(order):
    """Add or replace one order. For fixtures, evals and tests — not tools."""
    with _tx() as conn:
        _insert(conn, order, replace=True)


def update(order_id, **fields):
    """Set fields on one order directly. For tests and admin fixes only;
    tools change orders through change()."""
    bad = set(fields) - set(_COLUMNS) | ({"order_id"} & set(fields))
    if bad:
        raise ValueError(f"cannot set {sorted(bad)}")
    if "tracking" in fields:
        fields["tracking"] = json.dumps(fields["tracking"])
    with _tx() as conn:
        cur = conn.execute(f"UPDATE orders SET {', '.join(f'{k} = ?' for k in fields)} "
                           f"WHERE order_id = ?", (*fields.values(), str(order_id).strip()))
        if cur.rowcount != 1:
            raise KeyError(order_id)


# --------------------------------------------------------------------------
# keeping it safe
# --------------------------------------------------------------------------

def backup(dest):
    """A consistent copy, even while web.py is writing (SQLite backup API)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, out = _connect(), sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest


def reset():
    """Back the database up, then start again from the fixtures. Never
    called by the app; only by `python3 -m ami.store --reset`."""
    p, saved = path(), None
    if p.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved = backup(p.with_name(f"{p.stem}-{stamp}.bak{p.suffix}"))
        p.unlink()
    _READY.discard(str(p))
    _with(lambda c: None)                            # creates and seeds it
    return saved


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--reset"]:
        saved = reset()
        print(f"re-seeded {path()}" + (f"; the old one is at {saved}" if saved else ""))
    elif args[:1] == ["--backup"] and len(args) == 2:
        print(f"backed up {path()} to {backup(args[1])}")
    elif not args:
        print(path())
        for o in orders().values():
            print(f"  {o['order_id']}  {o['status']:<15} ${o['price']:>8.2f}  "
                  f"{o['email']:<18} {o['item']}")
        print(f"  {len(returns())} return(s), {len(refunds())} refund(s)")
    else:
        sys.exit("usage: python3 -m ami.store [--backup FILE | --reset]")
