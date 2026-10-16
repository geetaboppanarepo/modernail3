# Stage 2: Unit Tests

## Run the tests

```bash
cd stage2
python3 -m pytest tests -q
```

**Note:** pytest must be installed. It is not in `requirements.txt` to keep the project dependencies minimal. If you don't have pytest, install it with:

```bash
pip install pytest
```

## Test coverage by layer

Each test guards against specific bugs or missing features. Tests are organized by module and layer:

| Layer | File | What it pins |
|---|---|---|
| Unit | `test_store.py` | Fixture orders, status transitions, guardrail refusals return dicts not exceptions |
| Unit | `test_tools.py` | Schema required args (Stage 2: cancel_order and start_return have optional confirmed field), tool guardrails |
| Unit | `test_memory.py` | ConversationMemory trimming, WorkingMemory with turn/pending for confirmation gate, LongTermMemory round-trips JSON |
| Unit | `test_planner.py` | `_schemas_with_thought()` adds `thought` as LAST property, ReAct loop processes tool calls and observations |
| Unit | `test_policy.py` (Stage 2) | check_input scrubs PII and flags injection, guarded_run enforces escalate-once and confirmation gate, check_output verifies identifiers |
| Unit | `test_approvals.py` (HITL) | Over-limit refunds persist as PENDING and do not run; approve runs once, reject/expiry never; survives a new process; bad ids and transitions refused; under-limit unchanged; a call the tool would refuse (shipped cancel, undelivered or late return) is not gated, a deliverable one still is; an approval whose order changed before it ran (price, status, delivery date) runs nothing |
| Unit | `test_hitl_eval.py` (HITL) | TP/FP/FN/TN and both rates; zero denominators are undefined, never 1.00; `evals.py --hitl --scripted` prints both metrics, per-case rows, skipped and errored cases |
| HTTP | `test_web.py` (HITL) | Real server on an ephemeral port: reviewer token required, list/read/approve/reject, a second decision is 409 and nothing runs twice, a customer cannot see another customer's request, `/chat` reports the database's approval status apart from the ESC ticket |
| Unit | `test_order_store.py` | The SQLite order store: path config, seed once and never overwrite, persists across reopen, backup/reset; one transaction per change, one refund per order, retries replay, concurrent workers make one change, approved facts re-checked inside the transaction; rejected/expired approvals change nothing |
| Unit | `test_recovery.py` | A worker crashing after the order store committed, or before it got there: `reconcile` finishes from the store's record without a second refund, or retries under the same id; a different store, mismatched records or missing approved facts are flagged `unresolved` and need `resolve` by a named person; concurrent recoveries, and a recovery racing a live worker, agree; requests in any other state are left untouched |
| Process | `test_cross_process.py` (HITL) | Propose, approve and execute in separate interpreters sharing `approvals.db` and `orders.db`; the worker's change is visible to later processes; at most one execution; an order repriced by another process is not refunded; two processes racing to return one order make one return; a worker process that really exits mid-execution is recovered once by two racing `reconcile` processes; the `review.py reconcile`/`resolve` commands |
| Unit | `test_pricing.py` | rates(), cost(), usd() |
| Unit | `test_observe.py` | Events are valid JSON with required fields, stats() aggregates, timer works |
| Unit | `test_knowledge.py` | _chunks() splits by `##`, chunk count ≈ 48, metadata carries category |
| Eval | `evals.py` CASES | Agent chose right tool, refused correctly, store + customers.json changed as expected |
| Golden | `golden.py` + `golden.json` | Agent said the right thing (graded by LLM judge) |

## Style

- Plain `assert`, one behaviour per test
- Test names read as sentences: `test_<thing>_<does_what>_<when>`
- Small, visible test data in the test itself, not loaded from files
- Docstring at top of each file saying what it pins
- Students read these tests as documentation

## Fixtures

All tests use pytest fixtures defined in `conftest.py`:

- **`fresh_store`**: Resets the fake order database before each test, so tests never interfere
- **`tmp_state`**: Points `state/` at a temp directory so tests never read/write real disk files
- **`fake_llm`**: A scripted stand-in for `llm.complete()` so the planner tests run without the network; the `Reply` and `tool_call` helpers it plays back are in `fakes.py`

`tmp_state` is always on: no test can reach the real `state/` or `.cache/` folders.

**Exit code.** `conftest.py` ends the run with `os._exit(<pytest's status>)`
once pytest has reported. Without it, about one full run in four aborted
*after* every test had passed (exit 134, `libc++abi: ... recursive_mutex
lock failed`). The cause is onnxruntime 1.30's telemetry uploader thread
(`import onnxruntime` and a 10 s sleep does it too), and it is not caused by
any test. The workaround skips atexit handlers; set `AMI_TEST_NORMAL_EXIT=1`
for tools that need them, such as coverage.

**Subprocesses.** Tests that start Python children pass no `cwd` and
`close_fds=False`, so CPython uses `posix_spawn` instead of forking a test
process that has native threads running.

## Guardrails audit

Every guardrail has a test. A guardrail is a check inside a tool that returns `{"error": ...}` instead of letting a bad call through. Stage 2 adds policy layer guardrails that span tools and conversations.

**Tool guardrails (store and tools modules):**
- `test_store.py::TestCancelGuardrail` → cancel_order refuses shipped/delivered
- `test_store.py::TestReturnGuardrail` → start_return refuses non-delivered and past-window
- `test_tools.py::TestFindOrders` → unknown email returns error
- `test_tools.py::TestGetOrder` → unknown order returns error
- `test_tools.py::TestTrackPackage` → unknown order returns error

**Policy guardrails (Stage 2, policy module):**
- `test_policy.py::TestCheckInputScrubbing` → card numbers removed, order numbers preserved
- `test_policy.py::TestCheckInputInjection` → injection attempts flagged
- `test_policy.py::TestGuardedRunEscalateOnce` → one escalation per conversation
- `test_policy.py::TestGuardedRunConfirmation` → cancel_order/start_return need separate turn confirmation
- `test_policy.py::TestCheckOutputVerification` → unverified identifiers in replies are redacted
- `test_approvals.py` → refunds at or over `AMI_APPROVAL_LIMIT` wait for a human reviewer; fail closed on timeout

Each guardrail also appears in `evals.py` CASES so the full agent flow is tested too.
