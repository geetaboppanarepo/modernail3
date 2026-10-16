# Stage 2 — the same agent, plus a policy layer, long-term memory and a second planner

A complete copy of Stage 1 with three things added, so it runs on its own.
The reasoning and the file-by-file map are in `../README.md` under
**Stage 2**. Every file that differs from its Stage 1 twin says so at the
top of its docstring.

    New:        ami/policy.py  ami/plan_execute.py
                ami/approvals.py  ami/hitl_eval.py  review.py        (human-in-the-loop approval)
    Changed:    ami/planner.py  ami/tools.py  ami/agent_profile.py  ami/memory.py
                ami/store.py (now SQLite: state/orders.db)
                main.py  web.py  ui/chat.html  evals.py  golden.py  golden.json
    Unchanged:  everything else

    python3 web.py                            # http://localhost:8000 — planner switch in the header
    python3 main.py --plan                    # plan-and-execute in the terminal
    python3 evals.py --runs 2                 # score ReAct
    python3 evals.py --planner plan --runs 2  # score plan-and-execute
    python3 golden.py --audit                 # grade the judge itself, first
    python3 golden.py                         # 28 rows, including the policy layer

`.env` is one folder up. `state/` (sessions, trace, customer records) and
`.cache/` are generated on first run; `results/` is where the eval scripts
write, and holds sample runs.

## Human-in-the-loop approval

Above a refund limit, the customer's "yes" is not enough: a staff reviewer
must sign off, and Ami does not wait for them. The confirmed call is saved
to `state/approvals.db` as `PENDING_APPROVAL`, the customer gets a request
number, and the turn ends. A reviewer decides later, from any process.

    RECEIVED -> DRAFTED -> COMPLETED                      (no sign-off needed)
    RECEIVED -> DRAFTED -> PENDING_APPROVAL
        -> APPROVED -> EXECUTING -> COMPLETED | FAILED     reviewer said yes
        -> REJECTED -> COMPLETED                           reviewer said no; nothing runs
        -> EXPIRED                                         nobody decided in time; nothing runs

| | |
|---|---|
| `ami/approvals.py` | the store (stdlib SQLite), the state machine, `resume`, the sweeper, the reviewer API |
| `ami/policy.py` | `approval_reason()`: `cancel_order`/`start_return` with a refund at or over `AMI_APPROVAL_LIMIT`, priced from the store and not from the model. A call the tool's own guardrail would refuse (cancel a shipped order, return one not delivered or past the window) issues no refund and is not gated: `tools.refusal()` runs the tool's checks without changing anything. `guarded_run` asks it after the customer confirms |
| `web.py`, `main.py` | each turn is a request (`begin`/`finish`); decisions are folded into working memory at the next turn (`sync`) |
| `review.py` | the reviewer's command line |

Safeguards: every status change is a conditional `UPDATE ... WHERE status=?`
inside an `IMMEDIATE` transaction, with an audit row in the same
transaction. An action runs only after `resume()` wins the
`APPROVED -> EXECUTING` claim, so duplicate approvals and concurrent workers
run it at most once. A decision at or after the deadline is refused, and
the request is marked `EXPIRED` (fail closed).

| Setting | Default | |
|---|---|---|
| `AMI_APPROVAL_LIMIT` | `200` | dollars; at or over it needs a reviewer |
| `AMI_APPROVAL_TIMEOUT_MIN` | `240` | minutes a reviewer has to decide |
| `AMI_SWEEP_SECONDS` | `30` | how often `web.py` expires and resumes |
| `AMI_REVIEWER_TOKEN` | unset | required in `X-Reviewer-Token` by `/approvals`; unset = endpoints off |
| `AMI_APPROVALS_DB` | `state/approvals.db` | where requests live |
| `AMI_ORDERS_DB` | `state/orders.db` | the order store (below) |

With the seed data, cancelling the $149.99 Kindle (`112-3333333-3333333`)
still runs right after the customer confirms. Returning the $348 headphones
(`112-1111111-1111111`) goes to a reviewer.

**Demo.** After pulling these changes, restart any `web.py` that is already
running: the order store, approval checks and recovery are read at start-up.
Terminal 1:

    AMI_REVIEWER_TOKEN=s3cret python3 web.py

In the browser: *"Return the headphones on 112-1111111-1111111, they're broken"*,
then *"yes"*. Ami replies with a `REQ-…` number and the deadline; nothing
has been returned. Terminal 2, as the reviewer:

    H='X-Reviewer-Token: s3cret'
    curl -H "$H" localhost:8000/approvals                                  # pending
    curl -H "$H" localhost:8000/approvals/REQ-...                          # + audit trail
    curl -H "$H" -X POST -d '{"reviewer":"dana","reason":"ok"}' localhost:8000/approvals/REQ-.../approve
    curl -H "$H" -X POST -d '{"reviewer":"dana","reason":"used"}' localhost:8000/approvals/REQ-.../reject

Approve runs the return at once and answers with the RMA. The customer's
next message sees it in working memory. Stop and restart `web.py` while a
request is pending and it is still listed. Or use the CLI, which needs no
server:

    python3 review.py list
    python3 review.py show REQ-...
    python3 review.py approve REQ-... --reviewer dana [--defer]
    python3 review.py reject  REQ-... --reviewer dana --reason "used item"
    python3 review.py sweep

To see the timeout, start the server with `AMI_APPROVAL_TIMEOUT_MIN=0.5
AMI_SWEEP_SECONDS=5` and leave a request alone for 30 seconds: it becomes
`EXPIRED`, and approving it then answers 409.

**What the customer sees.** `/chat`, `/state` and `/reset` return
`approvals`: this browser session's approval requests only, with `id`
(`REQ-…`), `status`, a plain `label`, `action`, `order_id`, and `deadline`
while it is open. No tool arguments, policy reason, reviewer or reviewer's
reason are included. This is separate from `escalation`, the support ticket
(`ESC-…`) that the escalate tool opens. The chat page draws each request as
a blue entry (ESC tickets are orange), from those server fields and not from
the model's words. While one is waiting, the page re-reads `/state` every 15
seconds. `GET /requests/<id>` returns the same view, and 404 for anyone
else's request. Reviewers never use this page: their queue is
`/approvals` with the token, or `review.py`.

**Measuring it: HITL recall and escalation precision.** `evals.py` has a
`HITL_CASES` table. Each case has a hand-written `needs_approval` label
(over, at and under the limit, cancel and return, read-only lookups, an
ESC escalation, an unconfirmed request, an order that has already
shipped). A case counts as "requested" if an approval request reached that
case's approvals database.

    python3 evals.py --hitl --scripted   # deterministic: the model replaced by each case's tool calls
    python3 evals.py --hitl              # the real model chooses (proxy budget)
    python3 evals.py                     # the full table; the HITL block is printed too

A live run gives each model call `--timeout` seconds (default 120). A call
that runs out makes its case error; the case is still counted and named.

The report gives TP, FP, FN, TN, both rates with their fractions, one row
per case (expected against observed, REQ ids, ESC ticket), and any skipped
or errored cases. A rate with a zero denominator prints as `undefined`. The
scripted run tests the policy layer; the live run tests the model and the
policy together. Both describe this set of cases only.

## The order database

The pretend orders live in SQLite, `state/orders.db` (or `$AMI_ORDERS_DB`),
so `web.py`, `review.py`, the sweeper and any other process see the same
orders. A return approved in `review.py` shows up in the running server.

- **Seeding.** The four fixture orders are written once, when the file is
  first created, dated relative to that day. Opening an existing file never
  re-seeds it or overwrites an order, even if every order was deleted. As
  the file ages the dates do too (the headphones drift out of the 30-day
  window after about four weeks); `--reset` starts again.
- **One door for changes.** `cancel_order` and `start_return` go through
  `store.change()`. One `IMMEDIATE` transaction reads the order, runs the
  tool's guardrail on what it read, updates the status only if it is
  unchanged, and writes the refund, the RMA and an operation record. Two
  processes cancelling one order: one succeeds, the other is refused.
- **One refund per order.** `refunds` is keyed by order id; a second refund
  for an order rolls the whole change back, whatever path asked for it.
- **Retries are replays.** An approved action runs with its request id as
  the operation id. Running it again returns the first result and changes
  nothing. The approved facts (`basis`) are compared again inside the same
  transaction, so a price raised after approval cannot raise the refund.
- **Why its own file.** It stands in for the order system, which a real
  approval workflow could never share a transaction with. The request id
  links the two: `approvals.db` says what was decided, `orders.db` records
  what was done under that id. Re-seeding orders never touches the audit
  trail, and the evals give each case its own temp copy of both.

Inspect, back up, reset:

    python3 -m ami.store                         # every order, plus return and refund counts
    python3 -m ami.store --backup state/orders-backup.db   # safe while web.py runs
    python3 -m ami.store --reset                 # backs up to state/orders-<time>.bak.db, then re-seeds
    sqlite3 state/orders.db 'select * from refunds'   # if you have the sqlite3 CLI

`--reset` re-seeds the orders only. `approvals.db` still remembers every
action it ran, by tool and order id, so after a reset the same return on
the headphones is refused as "already approved and carried out" — on
purpose. To run the demo again from scratch, stop `web.py` and move both
`state/orders.db` and `state/approvals.db` aside (or point
`AMI_ORDERS_DB` and `AMI_APPROVALS_DB` at new files).

`approvals.db` can be copied the same way (`sqlite3 ... ".backup FILE"`, or
stop `web.py` and copy the file). Tests and evals never open either real
file: each test gets fresh databases in a temp folder.

### Recovering an interrupted execution

The order store records its first decision under each request id, whether
it made the change or refused it, and returns that same decision to any later call
with the id. A claimed request also records which order database it ran
against (`store.identity()`, kept in that file's `meta` table). From those
two facts `review.py reconcile` can tell:

| State | Means | `reconcile REQ-... --reviewer you` |
|---|---|---|
| `committed` | the store made the change under this id | finishes `COMPLETED` from the store's result; nothing runs again |
| `refused` | the store refused it under this id | finishes `FAILED`; nothing runs |
| `not_committed` | this same store has no decision under this id | runs it under the same id: the store decides once, anyone racing gets that decision, the approved facts are re-checked, the one-refund-per-order ledger still holds |
| `unresolved` | a different or unknown order store, an operation that does not match the request, a result and refund that disagree, or no approved facts | flags it (`execution_status = unresolved`, an audit row) and leaves it `EXECUTING` |

    python3 review.py reconcile                       # what is stuck, and what can be proved
    python3 review.py reconcile REQ-... --reviewer sam
    python3 review.py resolve REQ-... --as completed --reviewer sam --reason "refund seen in orders.db"

`resolve` only accepts a request that `reconcile` flagged as unresolved, and
it needs a name and a reason; it runs nothing. Two people reconciling at
once are safe: both read the same store record, and the second to finish
finds the request already finished. `resume()` also takes the store's
record over its own result, so a failure on the way back from a committed
change is not recorded as `FAILED`. A request claimed before this release
has no recorded store and comes out `unresolved`.

**Known limits.**
- Reconciling is manual. The sweeper never touches `EXECUTING`, because
  without a heartbeat it cannot tell a dead worker from a slow one (a
  reconcile racing a live worker is safe, just noisy).
- The refund is a row in a local ledger. A real refund would also call a
  payment provider, outside this transaction; exactly-once there needs the
  provider to accept the request id as its idempotency key. Nothing here
  claims that.
- `approvals.db` and `orders.db` are separate files, so a request's outcome
  and the order change are two commits. A worker that dies between them
  leaves the request `EXECUTING`, and nothing re-runs it by itself. See
  "Recovering an interrupted execution" above.
- `approvals.db` also keeps its own idempotency key, (tool, order id),
  checked when proposing and again in the claim transaction.
- A reviewer approves an order as it was. The facts the decision rests on
  (price, status, delivery date) are saved with the proposal (`basis`, shown
  by `review.py show`) and compared before running, and again inside the
  store's transaction. If they differ, nothing runs: the request becomes
  `FAILED` / `not_executed`, the difference is in the audit trail, and it
  needs a new request. A request saved before this column existed has no
  basis and is refused the same way. Any difference counts, even a harmless
  one.
- Prices are SQLite `REAL` (floats), as they were floats before.
- `EXPIRED` is terminal. The docstring of `ami/approvals.py` sketches
  escalating to a second reviewer without opening a path to unapproved
  execution.
- Reviewers are named, not authenticated individually: one shared token
  guards the HTTP API.
