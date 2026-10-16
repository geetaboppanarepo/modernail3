"""The reviewer's side of human-in-the-loop approval, from a terminal.

A separate process from the agent on purpose: the agent proposed, saved
and moved on; this reads what it saved, possibly hours later.

    python3 review.py list                         what is waiting (--all: everything)
    python3 review.py show REQ-...                 one request and its audit trail
    python3 review.py approve REQ-... --reviewer you [--reason ...] [--defer]
    python3 review.py reject  REQ-... --reviewer you [--reason ...]
    python3 review.py resume  REQ-...              run an approved action (idempotent)
    python3 review.py sweep                        expire overdue, run approved
    python3 review.py reconcile                    requests a crash left EXECUTING, and
                                                   what the order store can prove about each
    python3 review.py reconcile REQ-... --reviewer you
                                                   finish one from the order store's record
    python3 review.py resolve REQ-... --as completed|failed --reviewer you --reason ...
                                                   close an `unresolved` one by hand, after
                                                   checking the order yourself

approve records the decision and then runs the action here. With --defer
it only records it, and the next sweep runs it — use that while web.py is
running, so the action executes in the process that owns the order store.
"""

import argparse
import json
import sys

from ami import approvals, store


def _print(r):
    print(f"{r['id']}  {r['status']:<17} {r['tool'] or '-'} "
          f"{json.dumps(r['args']) if r['args'] else ''}")
    print(f"    customer: {r['customer_email'] or '?'}   session: {r['session_id'] or '-'}")
    print(f"    request:  {r['request']}")
    if r["policy_reason"]:
        print(f"    why:      {r['policy_reason']}")
    if r.get("basis"):
        print(f"    order as proposed: {json.dumps(r['basis'])}")
    if r["deadline"]:
        print(f"    deadline: {r['deadline']}" + ("   (OVERDUE)" if r.get("overdue") else ""))
    if r["decision"]:
        print(f"    decision: {r['decision']} by {r['reviewer']}"
              + (f" — {r['decision_reason']}" if r["decision_reason"] else ""))
    print(f"    execution: {r['execution_status']}"
          + (f" -> {json.dumps(r['outcome'])}" if r["outcome"] else ""))
    done = store.operation(r["id"])
    if done is not None:
        print(f"    order store: decided under {r['id']} -> {json.dumps(done)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    for name in ("show", "resume"):
        sub.add_parser(name).add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--reviewer", required=True)
        p.add_argument("--reason")
        if name == "approve":
            p.add_argument("--defer", action="store_true")
    sub.add_parser("sweep")
    rc = sub.add_parser("reconcile")
    rc.add_argument("id", nargs="?")
    rc.add_argument("--reviewer")
    rs = sub.add_parser("resolve")
    rs.add_argument("id")
    rs.add_argument("--as", dest="outcome", required=True, choices=("completed", "failed"))
    rs.add_argument("--reviewer", required=True)
    rs.add_argument("--reason", required=True)
    a = ap.parse_args()

    db = approvals.default()
    try:
        if a.cmd == "list":
            rows = db.list(None if a.all else approvals.PENDING)
            for r in rows:
                _print(r)
            print(f"{len(rows)} request(s)")
        elif a.cmd == "show":
            _print(db.get(a.id))
            for e in db.events(a.id):
                print(f"    {e['at']}  {e['from_status'] or '':>16} -> {e['to_status']:<16} "
                      f"{e['actor']}{'  ' + e['detail'] if e['detail'] else ''}")
        elif a.cmd == "approve":
            r = db.approve(a.id, a.reviewer, a.reason)
            if not a.defer:
                r = db.resume(a.id)
            _print(r)
        elif a.cmd == "reject":
            _print(db.reject(a.id, a.reviewer, a.reason))
        elif a.cmd == "resume":
            _print(db.resume(a.id))
        elif a.cmd == "sweep":
            print(json.dumps(db.sweep()))
        elif a.cmd == "reconcile" and not a.id:
            rows = db.stuck()
            for r in rows:
                d = db.diagnose(r["id"])
                print(f"{r['id']}  since {r['updated_at']}  {r['tool']} "
                      f"{json.dumps(r['args'])}  [{r['execution_status']}]")
                print(f"    {d['state']}: {d['why']}")
            print(f"{len(rows)} request(s) in EXECUTING")
        elif a.cmd == "reconcile":
            if not a.reviewer:
                sys.exit("reconcile REQ-... needs --reviewer")
            r, d = db.reconcile(a.id, a.reviewer)
            print(f"{d['state']}: {d['why']}")
            _print(r)
        elif a.cmd == "resolve":
            _print(db.resolve(a.id, a.reviewer, a.outcome, a.reason))
    except approvals.ApprovalError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
