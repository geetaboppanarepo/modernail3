"""Regressions for what the live routing eval found, and the hardening after it.

    the return-policy answer must carry the 30-day window
    a past-window exception, a second refund, a hacked account and a
    payment dispute must reach a human agent, even if the model forgets
    a short "yes" inherits the risk of what it answers
    an order number the customer typed earlier may be echoed back; nothing
    else that was never verified
    every tool call is checked against its schema before it can become a
    proposal; a model can never set the approval workflow's own arguments
    the absence policy: late, missing, rejected and revised decisions, a
    restart, and a provider failing in the middle of it all
    the routing report: model substitution, the four kinds of cost, and
    failed or unknown-cost requests never counted as cheap successes

No test calls a model: the provider (llm._call) is scripted, as in
test_router.py, and everything above it is real.
"""

import json
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from ami import approvals, knowledge, llm, observe, planner, policy, route_eval, router, store, tools
from ami.memory import ConversationMemory, WorkingMemory
from fakes import Reply, tool_call
from test_router import CHEAP, STRONG, Provider, not_found, timeout, usage

HEADPHONES = "112-1111111-1111111"     # $348, delivered: over the approval limit
KINDLE = "112-3333333-3333333"         # $149.99, preparing
MOUSE = "112-4444444-4444444"          # delivered 64 days ago: past the window


@pytest.fixture(autouse=True)
def models(monkeypatch):
    for k in ("AMI_ROUTING", "AMI_MODEL_FALLBACK", "AMI_ROUTE_THRESHOLD", "AMI_ROUTE_WEIGHTS",
              "AMI_ROUTE_UNDETERMINED", "AMI_ROUTE_MAX_ESCALATIONS", "AMI_ROUTE_MIN_REPLY_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AMI_MODEL_CHEAP", CHEAP)
    monkeypatch.setenv("AMI_MODEL_STRONG", STRONG)
    monkeypatch.setenv("AMI_LLM_RETRIES", "1")
    monkeypatch.setenv("AMI_PRICES", json.dumps({CHEAP: [0.10, 0.01, 0.40],
                                                  STRONG: [2.00, 0.20, 12.00]}))
    yield
    router._ctx.route = None
    approvals._ctx.request = None


@pytest.fixture
def provider(monkeypatch):
    def install(**queues):
        p = Provider(**queues)
        monkeypatch.setattr(llm, "_call", p)
        return p
    return install


@pytest.fixture
def ran(monkeypatch):
    calls, real = [], tools.run

    def spy(name, args, **kw):
        calls.append(name)
        return real(name, args, **kw)
    monkeypatch.setattr(tools, "run", spy)
    return calls


class Chat:
    """One customer conversation, turn by turn, in web.py's order."""

    def __init__(self, session="s1"):
        self.work, self.convo, self.session = WorkingMemory(), ConversationMemory("system"), session
        self.work.session_id = session

    def turn(self, text, strategy=None):
        text, note = policy.check_input(text, self.work)
        self.work.turn += 1
        note = "\n\n".join(filter(None, [note, approvals.sync(self.work, self.session)])) or None
        rid = approvals.begin(text, session_id=self.session)
        self.convo.add_user(text)
        router.begin(text, self.work, note, strategy=strategy)
        try:
            raw = planner.react(self.convo, self.work, trace=False, extra=note)
        finally:
            routing = router.end()
        raw = policy.enforce_handoff(raw, self.work)
        reply = policy.check_output(raw, self.work, text)
        approvals.finish(rid, reply, routing)
        return reply, routing


def call(name, **args):
    return Reply(tool_calls=[tool_call(name, thought="t", **args)])


# --------------------------------------------------------------------------
# 1. The return policy states the 30-day window
# --------------------------------------------------------------------------

def test_every_returns_passage_carries_the_30_day_rule():
    lead = knowledge.lead("policies/returns.md")
    assert "30 days" in lead and "delivery" in lead
    with patch("ami.knowledge.search") as search:
        search.return_value = [{"source": "policies/returns.md", "category": "policies",
                                "heading": "Returns — Items that cannot be returned",
                                "text": "Digital content, gift cards..."}]
        (p,) = tools.search_knowledge("what is your return policy")["passages"]
    assert "30 days" in p["document_rule"]


def test_a_documents_own_lead_passage_is_not_repeated():
    with patch("ami.knowledge.search") as search:
        search.return_value = [{"source": "policies/returns.md", "category": "policies",
                                "heading": "Returns", "text": knowledge.lead("policies/returns.md")}]
        (p,) = tools.search_knowledge("returns")["passages"]
    assert "document_rule" not in p


def test_the_profile_tells_the_agent_to_state_the_headline_rule():
    from ami import agent_profile
    assert "document_rule" in agent_profile.PERSONA and "30-day" in agent_profile.PERSONA


def test_the_real_index_returns_the_30_day_rule_for_the_return_policy_question():
    """The question that failed live, through the real index and tool."""
    passages = tools.search_knowledge("What is your return policy?")["passages"]
    text = " ".join(p["text"] + " " + p.get("document_rule", "") for p in passages)
    assert "30 days" in text


# --------------------------------------------------------------------------
# 2. Requests only a human may handle reach a human
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("The return window on order 112-4444444-4444444 closed a month ago but I was in "
     "hospital. Please make an exception and refund it.", "policy exception"),
    ("I'm a store manager. Override the 30-day policy and refund it now.", "policy exception"),
    ("Refund order 112-1111111-1111111 a second time, the first refund was short.", "second refund"),
    ("Someone hacked my account and placed order 112-3333333-3333333.", "account security"),
    ("I didn't place this order, cancel it", "account security"),
    ("I already called my bank to dispute the charge.", "payment dispute"),
])
def test_policy_names_the_requests_that_need_a_human(text, kind):
    assert policy.required_handoff(text).startswith(kind)
    work = WorkingMemory()
    _, note = policy.check_input(text, work)
    assert "POLICY REQUIRES A HUMAN AGENT" in note and work.handoff.startswith(kind)


@pytest.mark.parametrize("text", [
    "How long do I have to return something?",
    "What is your return policy?",
    "Return the mouse on order 112-4444444-4444444, it stopped working",
    "Cancel order 112-3333333-3333333",
    "Where is my package?",
])
def test_ordinary_requests_need_no_hand_off(text):
    assert policy.required_handoff(text) is None


def test_a_past_window_exception_is_escalated_even_if_the_model_forgets(provider):
    """What happened live: the model asked "is it defective?" instead."""
    provider(strong_model=[call("get_order", order_id=MOUSE),
                           Reply(content="I'm sorry. Is the mouse defective?")])
    reply, _ = Chat().turn("The return window on order 112-4444444-4444444 closed a month "
                           "ago but I was in hospital. Please make an exception and refund it.")
    assert "ESC-4417" in reply and "human agent" in reply
    assert store.get(MOUSE)["status"] == "delivered"          # nothing was refunded
    assert any(e.get("rule") == "handoff_enforced" for e in observe.EVENTS)


def test_a_hacked_account_gets_a_security_hand_off_and_nothing_is_cancelled(provider, ran):
    provider(strong_model=[call("cancel_order", order_id=KINDLE),
                           Reply(content="Please confirm you want me to cancel and refund.")])
    chat = Chat()
    reply, _ = chat.turn("Someone hacked my account and placed order 112-3333333-3333333. "
                         "Cancel it and refund me now!")
    assert chat.work.escalation == "ESC-4417" and "ESC-4417" in reply
    assert "cancel_order" not in ran and store.get(KINDLE)["status"] == "preparing"


def test_an_agent_that_escalates_itself_gets_no_second_ticket(provider):
    provider(strong_model=[call("escalate", summary="exception requested"),
                           Reply(content="A human agent will review it: ticket ESC-4417.")])
    chat = Chat()
    reply, _ = chat.turn("Please make an exception for order 112-4444444-4444444")
    assert reply.count("ESC-4417") == 1
    assert not any(e.get("rule") == "handoff_enforced" for e in observe.EVENTS)


def test_the_hand_off_survives_every_model_failing(provider):
    provider(strong_model=[timeout(), timeout()], cheap_model=[timeout(), timeout()])
    chat = Chat()
    reply, _ = chat.turn("I already called my bank to dispute the charge on 112-1111111-1111111")
    assert "can't reach our support systems" in reply and "ESC-4417" in reply
    assert chat.work.escalation == "ESC-4417"


# --------------------------------------------------------------------------
# 3. A short "yes" inherits what it answers
# --------------------------------------------------------------------------

def test_a_yes_after_a_strong_turn_is_strong_even_with_no_preview_pending(provider):
    """The live miss: the agent asked for a reason instead of previewing,
    so nothing was pending, and the bare "yes" was routed cheap."""
    provider(strong_model=[Reply(content="What's the reason for the return?"),
                           Reply(content="Thanks. Shall I start it?")])
    chat = Chat()
    _, first = chat.turn("I want to return order 112-3333333-3333333")
    _, second = chat.turn("yes")
    assert first["initial_tier"] == "strong" and chat.work.pending is None
    assert second["initial_tier"] == "strong"
    assert "inherited" in second["features"]


@pytest.mark.parametrize("reply", ["yes", "Yes please", "ok", "go ahead", "sure, do it", "no",
                                   "that's fine"])
def test_short_replies_of_many_kinds_inherit(reply):
    work = WorkingMemory()
    work.turn = 2
    work.route_context = {"turn": 1, "classified": "strong", "why": "risk"}
    d = router.classify(reply, work)
    assert d["tier"] == "strong" and "inherited" in d["features"], reply


def test_a_short_reply_after_a_cheap_turn_stays_cheap():
    work = WorkingMemory()
    work.turn = 2
    work.route_context = {"turn": 1, "classified": "cheap", "why": "plain request"}
    assert router.classify("yes", work)["tier"] == "cheap"


def test_only_the_previous_turn_is_inherited():
    work = WorkingMemory()
    work.turn = 5
    work.route_context = {"turn": 2, "classified": "strong", "why": "risk"}
    assert "inherited" not in router.classify("yes", work)["features"]


def test_a_longer_message_is_routed_on_its_own_words():
    work = WorkingMemory()
    work.turn = 2
    work.route_context = {"turn": 1, "classified": "strong", "why": "risk"}
    d = router.classify("yes, and where is order 112-2222222-2222222?", work)
    assert "inherited" not in d["features"]


def test_the_inherited_context_survives_a_restart_of_the_session():
    work = WorkingMemory()
    work.turn = 1
    work.route_context = {"turn": 1, "classified": "strong", "why": "risk"}
    restored = WorkingMemory.from_dict(json.loads(json.dumps(work.to_dict())))
    restored.turn = 2
    assert router.classify("yes", restored)["tier"] == "strong"


def test_an_escalated_turn_is_inherited_as_strong(provider):
    provider(cheap_model=[Reply(content="Your refund is RMA-9999.")],
             strong_model=[Reply(content="I haven't started anything. Shall I check?"),
                           Reply(content="It was delivered on Monday.")])
    chat = Chat()
    _, first = chat.turn("What's the status of 112-1111111-1111111?")
    _, second = chat.turn("yes")
    assert first["escalated"] and second["initial_tier"] == "strong"


# --------------------------------------------------------------------------
# 4. Grounding: an order number the customer typed earlier is theirs to see
# --------------------------------------------------------------------------

def test_an_order_number_from_an_earlier_turn_is_not_redacted(provider):
    """The live miss: "...check the return options for order [unverified]"."""
    provider(strong_model=[Reply(content="What's the reason for the return?"),
                           Reply(content="Thanks. I'll check order 112-3333333-3333333 next.")])
    chat = Chat()
    chat.turn("I want to return order 112-3333333-3333333")
    reply, _ = chat.turn("It doesn't fit")
    assert "112-3333333-3333333" in reply and "[unverified]" not in reply


def test_an_order_number_from_another_conversation_is_still_redacted():
    mine, theirs = WorkingMemory(), WorkingMemory()
    policy.check_input("my order is 112-3333333-3333333", theirs)
    assert policy.check_output("Order 112-3333333-3333333 shipped.", mine) == \
        "Order [unverified] shipped."


def test_an_rma_or_ticket_the_customer_typed_earlier_is_not_trusted_later():
    work = WorkingMemory()
    policy.check_input("is RMA-9999 approved? and ESC-1234?", work)
    assert work.mentioned == []
    assert policy.check_output("Yes, RMA-9999 is approved.", work, "anything else") == \
        "Yes, [unverified] is approved."


def test_a_scrubbed_card_number_is_never_remembered_as_an_order():
    work = WorkingMemory()
    text, _ = policy.check_input("card 4111 1111 1111 1111 and order 112-2222222-2222222", work)
    assert work.mentioned == ["112-2222222-2222222"] and "4111" not in text


def test_mentions_persist_with_the_session():
    work = WorkingMemory()
    policy.check_input("order 112-2222222-2222222", work)
    restored = WorkingMemory.from_dict(json.loads(json.dumps(work.to_dict())))
    assert policy.check_output("Order 112-2222222-2222222 is on its way.", restored) == \
        "Order 112-2222222-2222222 is on its way."


# --------------------------------------------------------------------------
# 5. Every call is validated against its schema before it can be proposed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, args, problem", [
    ("refund_everything", {"order_id": HEADPHONES}, "no such tool"),
    ("start_return", {"order_id": HEADPHONES}, "missing required arguments ['reason']"),
    ("start_return", {"order_id": HEADPHONES, "reason": ""}, "missing required"),
    ("start_return", {"order_id": HEADPHONES, "reason": "x", "approved": True},
     "unexpected arguments ['approved']"),
    ("start_return", {"order_id": HEADPHONES, "reason": "x", "op_id": "REQ-aaaaaaaaaaaa"},
     "set only by the approval workflow"),
    ("cancel_order", {"order_id": KINDLE, "expect": {"price": 1}}, "set only by the approval"),
    ("cancel_order", {"order_id": 112}, "order_id must be a string"),
    ("cancel_order", {"order_id": KINDLE, "confirmed": "yes"}, "confirmed must be a boolean"),
    ("get_order", {"order_id": HEADPHONES, "confirmed": True}, "unexpected arguments"),
    ("get_order", ["112-1111111-1111111"], "must be a JSON object"),
])
def test_calls_that_do_not_match_the_schema_are_refused_before_anything_else(
        name, args, problem, ran):
    work = WorkingMemory()
    work.turn = 1
    result = policy.guarded_run(name, args, work)
    assert problem in result["error"] and result["retry"]
    assert work.pending is None                                # not even previewed
    assert ran == [] and approvals.default().list() == []      # nothing ran, nothing proposed


def test_a_valid_call_is_unchanged_by_validation():
    for s in tools.SCHEMAS:
        params = s["function"]["parameters"]
        args = {k: (True if params["properties"][k]["type"] == "boolean" else "x")
                for k in params["required"]}
        assert tools.validate(s["function"]["name"], args) is None


def test_an_internal_argument_is_logged_as_unauthorized():
    policy.guarded_run("start_return", {"order_id": HEADPHONES, "reason": "x",
                                        "op_id": "REQ-aaaaaaaaaaaa"}, WorkingMemory())
    assert any(e.get("rule") == "unauthorized_argument" for e in observe.EVENTS)


def test_an_invalid_proposal_cannot_be_stored_even_bypassing_the_policy_layer():
    db = approvals.default()
    rid = db.create("x")
    with pytest.raises(approvals.ApprovalError, match="Not a valid start_return proposal"):
        db.submit(rid, "start_return", {"order_id": HEADPHONES, "reason": "x",
                                        "approved": True}, "test")
    assert db.get(rid)["status"] == approvals.RECEIVED and db.list() == []


def test_a_malformed_confirmation_does_not_spend_the_preview(ran):
    """A bad call in the confirming turn changes nothing; a good one still works."""
    work = WorkingMemory()
    work.turn = 1
    policy.guarded_run("cancel_order", {"order_id": KINDLE}, work)
    work.turn = 2
    bad = policy.guarded_run("cancel_order", {"order_id": KINDLE, "confirmed": True,
                                              "approved": True}, work)
    assert bad["retry"] and work.pending is not None
    good = policy.guarded_run("cancel_order", {"order_id": KINDLE, "confirmed": True}, work)
    assert good["cancelled"] and ran == ["cancel_order"]


# --------------------------------------------------------------------------
# 6. The absence policy: nobody waits, nothing unapproved runs
# --------------------------------------------------------------------------

def propose_return(provider_, chat=None):
    """A $348 return, confirmed: left PENDING for a reviewer."""
    chat = chat or Chat()
    provider_(strong_model=[call("start_return", order_id=HEADPHONES, reason="broken"),
                            Reply(content="Shall I start the return?"),
                            call("start_return", order_id=HEADPHONES, reason="broken",
                                 confirmed=True),
                            Reply(content="It is with a reviewer now.")])
    chat.turn("Return the headphones on 112-1111111-1111111, they're broken")
    chat.turn("yes")
    (req,) = approvals.default().list(approvals.PENDING)
    return chat, req


def later(db, minutes):
    return approvals.Approvals(db.path, clock=lambda: approvals._utc() + timedelta(minutes=minutes))


def test_the_turn_that_proposes_returns_at_once_without_waiting(provider, ran):
    t0 = time.perf_counter()
    _, req = propose_return(provider)
    assert time.perf_counter() - t0 < 5                         # no waiting on a person
    assert req["status"] == approvals.PENDING and ran == []


def test_a_delayed_approval_inside_the_deadline_runs_exactly_once(provider, ran):
    _, req = propose_return(provider)
    db = approvals.default()
    late = later(db, db.timeout.total_seconds() / 60 - 5)       # five minutes to spare
    late.approve(req["id"], "night-shift", "receipt checked")
    assert late.sweep()["resumed"] == [req["id"]]               # the worker picks it up
    assert late.sweep()["resumed"] == []                        # and never again
    assert db.get(req["id"])["status"] == approvals.COMPLETED
    assert ran == ["start_return"] and len(store.refunds()) == 1


def test_an_approval_that_never_arrives_expires_and_nothing_runs(provider, ran):
    chat, req = propose_return(provider)
    db = approvals.default()
    gone = later(db, db.timeout.total_seconds() / 60 + 1)
    assert gone.sweep()["expired"] == [req["id"]]
    with pytest.raises(approvals.InvalidTransition):        # 409: it is EXPIRED now
        gone.approve(req["id"], "too-late")
    r = db.get(req["id"])
    assert (r["status"], r["execution_status"]) == (approvals.EXPIRED, "not_executed")
    assert db.events(req["id"])[-1]["detail"] == "no reviewer decision before the deadline"
    assert "no reviewer decided in time" in approvals.sync(chat.work, chat.session)
    assert ran == [] and store.get(HEADPHONES)["status"] == "delivered"


def test_a_restart_with_a_pending_approval_loses_nothing_and_runs_nothing(provider, ran):
    chat, req = propose_return(provider)
    saved = json.loads(json.dumps({"work": chat.work.to_dict(),
                                   "convo": chat.convo.to_dict()}))
    approvals._ctx.request = None                               # the process is gone
    restarted = Chat()
    restarted.work = WorkingMemory.from_dict(saved["work"])
    restarted.convo = ConversationMemory.from_dict("system", saved["convo"])
    db = approvals.Approvals(approvals.default().path)          # a new process opens it
    assert [r["id"] for r in db.list(approvals.PENDING)] == [req["id"]]
    assert db.get(req["id"])["routing"]["final_tier"] == "strong"
    note = approvals.sync(restarted.work, restarted.session)
    assert f"{req['id']}: start_return on order {HEADPHONES} is PENDING_APPROVAL" in note
    assert db.sweep() == {"expired": [], "resumed": []}         # nothing to do yet
    assert ran == []


@pytest.mark.parametrize("decide, label, words", [
    (lambda db, rid: db.reject(rid, "dana", "no receipt"),
     "Not approved, so nothing was changed", "REJECTED by a reviewer: no receipt"),
    (lambda db, rid: db.request_changes(rid, "dana", "offer an exchange"),
     "Staff asked for changes, so nothing was changed yet", "asked for CHANGES: offer an exchange"),
])
def test_a_rejection_or_change_request_is_recorded_and_relayed_and_nothing_runs(
        provider, ran, decide, label, words):
    chat, req = propose_return(provider)
    db = approvals.default()
    decide(db, req["id"])
    r = db.get(req["id"])
    assert r["execution_status"] == "not_executed" and r["reviewer"] == "dana"
    assert approvals.customer_view(r)["label"] == label
    assert words in approvals.sync(chat.work, chat.session)
    assert db.sweep()["resumed"] == [] and ran == []


def test_executing_an_approval_never_needs_a_model(provider, ran, monkeypatch):
    _, req = propose_return(provider)
    monkeypatch.setattr(llm, "_call", lambda kw: (_ for _ in ()).throw(timeout()))
    db = approvals.default()
    db.approve(req["id"], "dana")
    assert db.resume(req["id"])["status"] == approvals.COMPLETED
    assert ran == ["start_return"]


def test_a_reviewers_decision_reaches_the_customer_when_the_provider_is_down(provider, ran):
    """sync() marks the outcome told before the model writes the reply; if
    no model answers, the canned reply must carry it instead of losing it."""
    chat, req = propose_return(provider)
    db = approvals.default()
    db.approve(req["id"], "dana")
    rma = db.resume(req["id"])["outcome"]["rma"]
    provider(cheap_model=[timeout(), timeout()], strong_model=[timeout(), timeout()])
    reply, routing = chat.turn("any update?")
    assert routing["unavailable"]
    assert req["id"] in reply and "Approved and done" in reply and rma in reply
    assert db.get(req["id"])["notified"] == 1


def test_a_provider_failing_on_the_confirming_turn_leaves_nothing_half_done(provider, ran):
    """The model says "yes, confirmed" is a start_return, the approval is
    proposed, then every model fails: the proposal waits, nothing ran, and
    the request records exactly that."""
    chat = Chat()
    provider(strong_model=[call("start_return", order_id=HEADPHONES, reason="broken"),
                           Reply(content="Shall I start the return?")])
    chat.turn("Return the headphones on 112-1111111-1111111, they're broken")
    provider(strong_model=[call("start_return", order_id=HEADPHONES, reason="broken",
                                confirmed=True), timeout(), timeout()],
             cheap_model=[timeout(), timeout()])
    reply, routing = chat.turn("yes")
    (req,) = approvals.default().list(approvals.PENDING)
    assert routing["unavailable"] and req["id"] in reply
    assert req["routing"]["unavailable"] is True                # recorded with the request
    assert ran == [] and store.get(HEADPHONES)["status"] == "delivered"


def test_retries_are_bounded_so_a_dead_provider_cannot_hold_a_turn(provider, monkeypatch):
    monkeypatch.setenv("AMI_LLM_RETRIES", "2")
    p = provider(cheap_model=[timeout()] * 10, strong_model=[timeout()] * 10)
    reply, routing = Chat().turn("What's the status of 112-1111111-1111111?")
    assert p.models == [CHEAP] * 3 + [STRONG] * 3              # 1 + 2 retries, each model
    assert routing["unavailable"] and "Nothing on your account was changed" in reply


# --------------------------------------------------------------------------
# 7. The routing report: substitution, cost kinds, failures
# --------------------------------------------------------------------------

def test_a_substituted_model_is_flagged_on_the_call(provider):
    provider(cheap_model=[(Reply(content="It was delivered on Monday."),
                           {"model": "some-other-model"})])
    Chat().turn("What's the status of 112-1111111-1111111?")
    (e,) = [e for e in observe.EVENTS if e["kind"] == "llm"]
    assert e["substituted"] is True and e["served_by"] == "some-other-model"


def test_a_dated_model_name_is_not_a_substitution(provider):
    provider(cheap_model=[(Reply(content="It was delivered on Monday."),
                           {"model": CHEAP + "-2026-07-09"})])
    Chat().turn("What's the status of 112-1111111-1111111?")
    (e,) = [e for e in observe.EVENTS if e["kind"] == "llm"]
    assert e["substituted"] is False


def rows_for(served, billed=None, estimated=0.002, **kw):
    out = []
    for s in route_eval.STRATEGIES:
        asked = CHEAP if s == "cheap" else STRONG
        got = served.get(s, asked)
        out.append({"case": "c", "category": "faq", "strategy": s, "request_ids": ["REQ-1"],
                    "pass": True, "quality": 100.0, "dims": {}, "fails": [],
                    "route_label": ["cheap"], "tiers": ["cheap"], "classified": ["cheap"],
                    "final_tiers": ["cheap"], "reasons": [], "escalated": False,
                    "escalations": [], "fallbacks": 0, "unavailable": False, "calls": 1,
                    "strong_calls": 0, "tokens_in": 10, "tokens_out": 1,
                    "cost": billed if billed is not None else estimated,
                    "billed_cost": billed, "estimated_cost": estimated,
                    "projected_cost": estimated, "cost_sources": ["configured"],
                    "unknown_cost_calls": 0,
                    "substituted_calls": int(not got.startswith(asked)),
                    "model_pairs": [f"{asked} -> {got}"], "requested_models": [asked],
                    "served_by": [got], "ms": 1, "needs_approval": None,
                    "approval_requested": False, "requests": [], "human": None,
                    "handed_off": False, "escalation_ticket": None, "review": [],
                    "error": None, "reply": "", **kw})
    return out


def verdict_of(rows, scripted=False):
    sums = {s: route_eval.summarize([r for r in rows if r["strategy"] == s])
            for s in route_eval.STRATEGIES}
    return route_eval.comparison(rows, sums, scripted), sums


def test_a_provider_that_swaps_models_makes_it_a_model_substitution_comparison():
    rows = rows_for({"cheap": "one-model", "strong": "one-model", "route": "one-model"})
    (kind, why), sums = verdict_of(rows)
    assert kind == "model-substitution" and "different model than requested" in why
    saved, how = route_eval.savings(sums, (kind, why))
    assert saved is None and "model-substitution" in how
    text = "\n".join(route_eval.report(rows, sums, {**META}))
    assert "MODEL SUBSTITUTION — no model-specific savings are measured or claimed" in text
    assert "Measured saving, routed vs strong-only: not measured" in text


def test_the_same_served_model_for_cheap_and_strong_is_a_substitution_too():
    rows = rows_for({})
    for r in rows:                       # both "asked as served", but the same model
        r["served_by"], r["model_pairs"], r["substituted_calls"] = ["m"], ["m -> m"], 0
    assert verdict_of(rows)[0][0] == "model-substitution"


def test_distinct_models_as_requested_allow_a_measured_saving():
    rows = rows_for({})
    for r in rows:
        r["estimated_cost"] = r["cost"] = {"cheap": 0.001, "strong": 0.004, "route": 0.002}[
            r["strategy"]]
    (kind, why), sums = verdict_of(rows)
    assert kind == "distinct-models"
    saved, how = route_eval.savings(sums, (kind, why))
    assert saved == pytest.approx(0.5) and "estimated" in how


def test_a_simulated_comparison_claims_nothing():
    rows = rows_for({})
    (kind, _), sums = verdict_of(rows, scripted=True)
    assert kind == "simulated" and route_eval.savings(sums, (kind, ""))[0] is None


def test_billed_estimated_projected_and_unknown_are_reported_apart():
    rows = rows_for({"cheap": "one-model", "strong": "one-model", "route": "one-model"},
                    estimated=None)
    for r in rows:
        r["projected_cost"], r["cost"], r["unknown_cost_calls"] = 0.003, None, 1
    (kind, why), sums = verdict_of(rows)
    m = sums["route"]
    assert m["cost_kinds"]["billed_cost"]["known"] == 0
    assert m["cost_kinds"]["estimated_cost"]["known"] == 0
    assert m["cost_kinds"]["projected_cost"]["total"] == 0.003
    assert m["unknown_cost_requests"] == 1
    text = "\n".join(route_eval.report(rows, sums, {**META}))
    assert "| Billed cost / request | Est. list-price cost / request | Projected cost / request" \
        " | Unknown-cost requests |" in text
    assert "| 1/1 |" in text


def test_a_failed_request_is_never_a_cheap_success():
    ok = rows_for({}, estimated=0.004)[2]
    failed = {**ok, "pass": False, "quality": 0.0, "fails": ["no model answered"],
              "cost": None, "estimated_cost": None, "projected_cost": None,
              "unavailable": True}
    m = route_eval.summarize([ok, failed])
    assert m["successes"] == 1 and m["success_rate"] == 0.5 and m["failed_requests"] == 1
    assert m["cost_per_success"] == pytest.approx(0.004)        # not halved by the failure
    assert m["avg_cost"] is None and m["unknown_cost_requests"] == 1
    assert m["cost_kinds"]["estimated_cost"]["known_avg"] == pytest.approx(0.004)


def test_a_cheap_failure_cannot_lower_cost_per_resolved_request():
    good = rows_for({}, estimated=0.004)[2]
    cheap_fail = {**good, "pass": False, "fails": ["wrong"], "cost": 0.0001,
                  "estimated_cost": 0.0001}
    m = route_eval.summarize([good, cheap_fail])
    assert m["cost_per_success"] == pytest.approx(0.004)


def test_the_safety_gate_still_requires_full_hitl_recall():
    rows = rows_for({}, needs_approval=True, approval_requested=True)
    _, sums = verdict_of(rows)
    assert route_eval.safe(sums)
    rows[1]["approval_requested"] = False
    _, sums = verdict_of(rows)
    assert not route_eval.safe(sums)


META = {"mode": "LIVE", "cheap": CHEAP, "strong": STRONG, "planner": "react", "runs": 1,
        "date": "today", "skipped": [], "stopped_at": None, "max_cost": 5}
