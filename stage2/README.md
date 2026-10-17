# Stage 2 — the same agent, plus a policy layer, long-term memory and a second planner

A complete copy of Stage 1 with three things added, so it runs on its own.
The reasoning and the file-by-file map are in `../README.md` under
**Stage 2**. Every file that differs from its Stage 1 twin says so at the
top of its docstring.

    New:        ami/policy.py  ami/plan_execute.py
                ami/approvals.py  ami/hitl_eval.py  review.py        (human-in-the-loop approval)
                ami/router.py  ami/route_eval.py                     (model routing, below)
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
        -> REJECTED -> COMPLETED                           reviewer said no, or asked for
                                                           changes; nothing runs
        -> EXPIRED                                         nobody decided in time; nothing runs

"Request changes" is a no with a note (decision `changes_requested`): the
proposal closes unexecuted, the agent tells the customer what the reviewer
wants at their next message, and a revised action is a new proposal with a
new review. A draft is never edited and then run on the old approval.

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
    curl -H "$H" -X POST -d '{"reviewer":"dana","reason":"offer an exchange"}' localhost:8000/approvals/REQ-.../request-changes

Approve runs the return at once and answers with the RMA. The customer's
next message sees it in working memory. Stop and restart `web.py` while a
request is pending and it is still listed. Or use the CLI, which needs no
server:

    python3 review.py list
    python3 review.py show REQ-...
    python3 review.py approve REQ-... --reviewer dana [--defer]
    python3 review.py reject  REQ-... --reviewer dana --reason "used item"
    python3 review.py revise  REQ-... --reviewer dana --reason "offer an exchange"
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

## Model routing

Each customer turn is drafted by one of two models: a **cheap** one for
lookups, tracking, FAQs and policy questions the knowledge base answers,
and a **strong** one for anything that moves money or changes an account
(including the customer's "yes" to one), policy exceptions and authority
claims, fraud, security, legal and payment disputes, ambiguous or
multi-step requests, upset customers, and input the policy layer flagged.

| | |
|---|---|
| `ami/router.py` | classifies the turn in plain code (no model call to decide), keeps the turn's route, escalates cheap -> strong on observed signals, retries and falls back on provider failures |
| `ami/llm.py` | `complete()` calls the current route's model; records tokens, cost and where the cost came from (`provider`, `configured`, or `unknown`) |
| `ami/planner.py`, `ami/plan_execute.py` | report signals to the route; redraft an inadequate cheap answer once; answer truthfully when no model is reachable |
| `ami/approvals.py` | saves the routing record with every request (column `routing`); `request_changes()` |
| `ami/route_eval.py`, `evals.py --compare` | cheap-only vs strong-only vs routed on the same cases: the cost-vs-quality report |

**Policy rules that do not depend on the model.** Added after the live
eval showed the model skipping them; they hold under every strategy:
- *Required hand-offs* (`policy.required_handoff`): a policy exception
  (past the window, "make an exception", override), a second refund, an
  account-security complaint (hacked, "I didn't place this order") or a
  payment dispute. `check_input` tells the model to look the rule up and
  escalate; if it does not, `enforce_handoff` opens the ticket itself at
  the end of the turn (through `guarded_run`, so escalate-once holds) and
  says so in the reply. This also runs when no model is reachable.
- *Schema validation* (`tools.validate`): every tool call is checked
  against the schema the model was shown, before the confirmation gate
  and before anything can be proposed. An unknown tool, or unknown,
  missing, empty or mistyped arguments, are refused as a bad call
  (`retry`). The approval workflow's own arguments (`op_id`, `expect`)
  are refused by name (`unauthorized_argument` in the trace).
  `approvals.submit` validates again, so nothing malformed is ever stored
  for a reviewer.
- *Grounding across turns*: an order number the customer typed earlier in
  the same conversation may be echoed back (`work.mentioned`). Only order
  numbers carry over between turns. An RMA or ticket number the customer
  typed is a claim, not a fact, and nothing from another conversation is
  trusted.
- *The 30-day rule*: each retrieved passage carries its document's
  headline rule (`document_rule`, from `knowledge.lead`), so a returns
  answer always has the window, whichever section ranked first.

**How a turn is routed.** `router.classify()` scores features of the
customer's words and of working memory (`router.WEIGHTS`): `risk`
(cancel/return/refund as a request, not as a question about the rule),
`pending` (a preview awaits the customer's answer), `exception`,
`sensitive`, `policy_flag`, `upset`, `multi_order` (2 each), and
`multi_intent`, `ambiguous`, `vague`, `unknown_intent`, `long`,
`prior_refusal`, `reviewer_news` (1 each), and `inherited` (2): a short
reply ("yes", "go ahead", "no") to a turn that was strong is strong too,
whatever its own words. The last turn's routing is kept in working memory
(`route_context`, saved with the session), so a bare "yes" is routed by
what it answers, not by its literal wording. A score at or over the threshold
(2) goes to the strong model. The decision, its score and every feature
behind it are logged (`kind: route` in `state/trace.jsonl`), shown in the
web trace panel, printed by `main.py`, and saved with the request
(`review.py show` prints "drafted by"). A request that cannot be
classified goes to `AMI_ROUTE_UNDETERMINED` (strong).

**Escalation, bounded.** A routed cheap turn moves to the strong model for
the rest of the turn when the loop observes: tool arguments that are not
JSON, a call the tool rejected as malformed (`retry`), a request for a
state-changing tool, an answer that is empty, too short, or quotes an order
number, RMA or ticket no tool returned (the output policy's own check), a
used-up step budget, or, as one signal among those, an answer that says it
is unsure. An inadequate cheap answer is dropped and redrafted by the strong
model. Escalation is one-way and at most `AMI_ROUTE_MAX_ESCALATIONS` (1) per
turn: a second bad answer is returned as it is, and the output policy still
redacts it. Cheap-only and strong-only never escalate; they are the
baselines.

**Routing never decides what is allowed.** Every tool call still goes
through `policy.guarded_run`: the same confirmation gate (a later turn's
yes), the same approval limit priced from the order store, the same
reviewer. There is no tool a model can call to approve anything, and the
internal `op_id`/`expect` arguments are refused from the model. The tests
run an over-limit refund under all three strategies: it waits for a person
in every one.

**Failures.** Each model call has `AMI_LLM_TIMEOUT` seconds. A transient
failure (timeout, connection, 5xx) is retried `AMI_LLM_RETRIES` times, then
the fallback model is tried (default: the other tier), and the fallback is
logged and recorded. When every model fails the customer is told only what
is true: what this turn already did (an action, or a request now waiting
for a reviewer), otherwise that nothing changed, and to try again. A
pending approval is untouched by any of this: it is in `approvals.db`, the
worker never waits on it, and the absence policy above (deadline ->
`EXPIRED`, nothing runs) applies whatever model drafted it.

| Setting | Default | |
|---|---|---|
| `AMI_ROUTING` | `route` | `route`, or `cheap` / `strong` to use one model for every turn |
| `AMI_MODEL_CHEAP` | `$MODEL`, else `gpt-4o-mini` | the cheap tier |
| `AMI_MODEL_STRONG` | `gpt-5.6-terra` | the strong tier |
| `AMI_MODEL_FALLBACK` | the other tier | comma list tried after a model fails; `none` for no fallback |
| `AMI_ROUTE_THRESHOLD` | `2` | score at or over which a turn is strong |
| `AMI_ROUTE_WEIGHTS` | | JSON overriding feature weights, e.g. `{"upset": 1}` |
| `AMI_ROUTE_UNDETERMINED` | `strong` | tier when a request cannot be classified |
| `AMI_ROUTE_MAX_ESCALATIONS` | `1` | cheap -> strong escalations per turn (`0`: never) |
| `AMI_ROUTE_MIN_REPLY_CHARS` | `15` | a shorter cheap answer is redrafted |
| `AMI_LLM_TIMEOUT` | `60` | seconds per model call |
| `AMI_LLM_RETRIES` | `1` | retries per model on a transient error |
| `AMI_PRICES` | | JSON price rows, $/million tokens `[input, cached, output]`, added to `ami/pricing.py` |

`OPENAI_API_KEY` and `OPENAI_BASE_URL` (in `../.env`) are the provider
credentials for both tiers. The key is never logged: error text is
redacted before it reaches the trace. `../.env.example` lists every setting.

**Measuring cost against quality.**

    python3 evals.py --compare --scripted     # deterministic, free: routing, escalation, HITL safety
    python3 evals.py --compare                # against the provider; --max-cost 4 caps the spend
    python3 evals.py --strategy cheap         # one strategy over the normal table

`--compare` runs every case three ways, interleaved case by case so a
budget stop leaves all three strategies with the same cases, and writes
`results/routing_report.md` and `results/routing_compare.json` (a scripted
run writes `routing_report_scripted.md` and `routing_compare_scripted.json`,
so it never overwrites a live report). The cases
are `CASES + HITL_CASES + ROUTING_CASES` (51; scripted runs use the 31 with
scripts). Each has hand-written labels: `category`, `route` (the tier each
turn should start on), `human` (should it be handed to a human agent, per
`knowledge/policies/escalation.md`), and for the approval cases
`needs_approval`. Four routing cases play the reviewer afterwards (approve,
reject, request changes, never answer) and check the order and what the
agent is told next.

Per request the report gives the request ids, category, the tiers used and
why, escalation, tokens, cost, latency, the quality score and its parts
(correct, complete, policy, grounded), whether approval was needed and
requested, and the reviewer's decision. Per strategy: total, average and
per-resolved-request cost, average latency, quality, task success, strong
escalation rate, HITL recall (must be 100% in every strategy, or the run
exits 1), escalation precision, and the human hand-off precision.

What the numbers are, and are not:
- Quality is the share of each case's own checks that passed. It is not an
  LLM judge (`golden.py` is), and it describes this set of cases only.
- Cost is the provider's figure when the response has one, else the
  configured list price (`ami/pricing.py`, marked unverified there) times
  the measured tokens, else `n/a`. An unknown cost is never counted as zero.
- A scripted run reports no usage, so its cost is `n/a` throughout.
- Four kinds of cost are reported apart, never mixed: **billed** (what the
  provider reported in its usage; the class proxy reports none),
  **estimated** (configured list price of the model that served the call ×
  its tokens), **projected** (the same tokens at the requested model's list
  price) and **unknown** (counted, never averaged in as zero). A failed
  request is never a success, whatever it cost.
- Every call's served model is checked against the requested one
  (`substituted` in the trace). The report states a **comparison type**
  in its first lines: `simulated`, `model-substitution` (some call
  was served by a different model than requested, or cheap-only and
  strong-only were served by the same model), or `distinct-models`. Only a
  `distinct-models` run prints a measured saving. Any other type prints
  "not measured".
- **The class proxy serves `gpt-5.6-terra` whatever model is asked for**
  (checked: `gpt-4o-mini`, `gpt-5.6-luna`, `gpt-5.6-sol` and `gpt-5.6-terra`
  all came back as `gpt-5.6-terra-2026-07-09`, with no cost in the usage).
  Through it, the three strategies run the same model, so a live
  comparison measures routing behaviour, tokens, latency and HITL safety,
  but not a real cheap-vs-strong difference in quality or price. The
  report detects this and says so. Its *projected cost* column prices the
  measured tokens at each requested model's listed price; that is a
  projection, not a bill. **Live model-specific savings have not been
  validated:** that needs provider responses that show the requested
  (different) model ids, and a `distinct-models` comparison on them.
  For a real comparison, point `OPENAI_BASE_URL`
  at a provider that honours model names and set `AMI_MODEL_CHEAP` /
  `AMI_MODEL_STRONG` (and `AMI_PRICES` if they are not listed).

**Reports in `results/`.**
- `routing_report_scripted.md` comes from `evals.py --compare --scripted`.
  It is deterministic and has no cost.
- `routing_report.md` is the full live run of the 49 cases the set had then. It predates the
  fixes above and is re-rendered in the current format (see its note).
- `routing_regressions_live.md` re-runs the cases those fixes target, live:

      python3 evals.py --compare --only "faq: the return policy|policy exception: past the window|high risk: account hacked|guard: return an undelivered order|confirmation inherits|policy backstop|rag: a regulation|rag: a rule about the agent|reviewer approves" --out results/routing_regressions_live.json --report results/routing_regressions_live.md

  All three live reports are `model-substitution` comparisons.
