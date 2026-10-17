"""Model routing: which model drafts this turn, and why.

Two tiers:

    cheap    lookups, order status, tracking, FAQs and policy questions
             the knowledge base answers, handing over to a human
    strong   anything that moves money or changes an account (including
             the customer's "yes" to one), exceptions to policy, fraud,
             security, legal and payment disputes, ambiguous or multi-step
             requests, an upset customer, input the policy layer flagged

The decision is made in plain code, from the customer's words, working
memory and the input policy's note: no model call is spent deciding which
model to call. classify() scores a few features (WEIGHTS) and a score at
or over the threshold goes to the strong tier. Every decision carries its
reason and the features behind it, and is logged and saved with the
request (approvals.db, column `routing`).

During the turn the route can ESCALATE, cheap -> strong, on signals the
loop observes rather than on the model's opinion of itself:

    - a tool call whose arguments are not valid JSON
    - a call the tool rejected as malformed (the `retry` flag)
    - the cheap model asking for a state-changing tool (cancel, return)
    - an answer that is empty, too short, or quotes an order number, RMA
      or ticket no tool returned (the output policy's own check)
    - the step budget running out
    - and, as one signal among these, an answer that says it is unsure

Escalation goes one way and at most AMI_ROUTE_MAX_ESCALATIONS times per
turn, so there is no loop: a strong model cannot be escalated, and a
second inadequate answer is returned as it is (the output policy still
redacts it). The fixed strategies, cheap-only and strong-only, never
escalate; they are the baselines routing is measured against.

Routing chooses who WRITES. It does not decide what is allowed. Every
tool call still goes through policy.guarded_run, so a cheap model meets
the same confirmation gate and the same staff-approval limit as a strong
one, and no model can approve anything: approval is a person, through
approvals.py.

Provider failures: each model is retried on a transient error (timeout,
connection, 5xx) up to AMI_LLM_RETRIES times, then the next model in the
fallback list is tried (by default the other tier). When every model
fails, ModelUnavailable is raised and the planner answers with
unavailable_reply(), which says only what is true: what this turn already
did, and that nothing else changed.

Settings, read on every call (so a test or a running eval can change them):

    AMI_ROUTING                 route (default) | cheap | strong
    AMI_MODEL_CHEAP             default: $MODEL, else gpt-4o-mini
    AMI_MODEL_STRONG            default: gpt-5.6-terra
    AMI_MODEL_FALLBACK          comma list tried after a model fails;
                                default: the other tier; "none": no fallback
    AMI_ROUTE_THRESHOLD         score at or over which a turn is strong (2)
    AMI_ROUTE_WEIGHTS           JSON overriding WEIGHTS, e.g. {"upset": 1}
    AMI_ROUTE_UNDETERMINED      tier when classifying fails: strong (default) | cheap
    AMI_ROUTE_MAX_ESCALATIONS   cheap -> strong escalations per turn (1)
    AMI_ROUTE_MIN_REPLY_CHARS   a shorter answer is inadequate (15)
    AMI_LLM_RETRIES             retries per model on a transient error (1)
    AMI_LLM_TIMEOUT             seconds per model call (60; read by llm.py)
"""

import json
import os
import re
import threading
from dataclasses import dataclass, field

from ami import observe

CHEAP, STRONG = "cheap", "strong"
STRATEGIES = ("route", "cheap", "strong")

WEIGHTS = {
    "risk": 2,             # moves money or changes an account
    "pending": 2,          # the customer is answering a cancel/return preview
    "inherited": 2,        # a short reply ("yes", "go ahead") to a strong turn
    "exception": 2,        # override, exception, authority claim, a second refund
    "sensitive": 2,        # fraud, security, legal, a payment dispute
    "policy_flag": 2,      # the input policy flagged an injection or a card number
    "upset": 2,            # an upset customer
    "multi_order": 2,      # more than one order in one request
    "multi_intent": 1,     # several things asked at once
    "ambiguous": 1,        # refers to an order nothing identifies
    "vague": 1,            # a problem with no detail to act on
    "unknown_intent": 1,   # matches no support intent (novel or out of scope)
    "long": 1,             # a long, context-heavy message
    "prior_refusal": 1,    # something was already refused in this conversation
    "reviewer_news": 1,    # a reviewer decided something to explain this turn
}

# -- request features ------------------------------------------------------

_ORDER_ID = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_ACTION = re.compile(r"\b(cancel\w*|return(s|ing|ed)?|refund\w*|send (it|this|them|that) back|"
                     r"money back|replace\w*|exchange\w*|charge ?back)\b", re.I)
# "Can I return it?" asks about the rule; "can you return it" asks for the act.
_POLICY_Q = re.compile(r"^\s*(how|what|when|where|which|why|do|does|is|are|am|"
                       r"(can|could|may|should) (i|we))\b", re.I)
_EXCEPTION = re.compile(r"\b(override|exception|waive|bend the rules?|even though|"
                        r"past the (\d+[- ]day )?(return )?window|window (closed|has passed|is over)|"
                        r"too late|a second time|twice|refund\w* again|extra refund|"
                        r"compensat\w*|goodwill|manager|supervisor)\b", re.I)
_SENSITIVE = re.compile(r"\b(fraud\w*|scam\w*|hack\w*|stolen|unauthori[sz]ed|identity|"
                        r"didn'?t (order|place|make|buy)|dispute\w*|chargeback|my bank|"
                        r"lawyer|legal|sue|court|police|gdpr|delete my (data|account)|"
                        r"personal data)\b", re.I)
_UPSET = re.compile(r"(!{2,}|\b(ridiculous|unacceptable|worst|terrible|furious|angry|fed up|"
                    r"useless|disgusting|third time|nobody helps|no one helps|sick of)\b)", re.I)
_REFERENCE = re.compile(r"\b(it|this|that|they|them|this one|that one|my order|my package|"
                        r"the order|the package|my parcel|my stuff|the item)\b", re.I)
_VAGUE = re.compile(r"\b(problem|issue|something('s| is)? wrong|not right|messed up|"
                    r"hasn'?t (come|arrived)|never (came|arrived)|not (come|arrived)|missing|weird)\b", re.I)
_SMALL_TALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|great|bye|goodbye|cheers)\b", re.I)
_INTENTS = {
    "status": re.compile(r"\b(status|where('s| is)|track\w*|arriv\w*|deliver\w*|eta|shipped|"
                         r"on its way|when will)\b", re.I),
    "action": _ACTION,
    "policy": re.compile(r"\b(policy|policies|how long|window|rules?|allowed|eligible|"
                         r"what happens|damaged|refund\w*|returns?)\b", re.I),
    "account": re.compile(r"\b(account|password|email|address|payment|card|billing|"
                          r"don'?t know (my|the) order)\b", re.I),
    "human": re.compile(r"\b(human|person|agent|someone|representative|manager)\b", re.I),
    "lookup": _ORDER_ID,
}
_CONNECTOR = re.compile(r"\b(and|also|then|plus|after that)\b|[,;]", re.I)
# A short reply carries no risk in its own words: "yes" is as dangerous as
# whatever it answers. Matched as the whole message (a few words at most).
_SHORT_REPLY = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|please|please do|go ahead|"
                          r"do it|confirm(ed)?|correct|that'?s (right|correct|fine)|right|"
                          r"no|nope|don'?t|stop|wait|fine|absolutely|definitely|of course)"
                          r"([ ,.!]+(yes|please|do it|go ahead|thanks|thank you|that'?s fine))*"
                          r"[ .!]*$", re.I)
_HEDGE = re.compile(r"\b(i'?m not sure|i am not sure|i don'?t know|i do not know|"
                    r"i can'?t (determine|tell)|unable to determine)\b", re.I)


@dataclass
class Settings:
    strategy: str
    cheap: str
    strong: str
    fallback: list
    threshold: float
    weights: dict
    undetermined: str
    max_escalations: int
    min_reply_chars: int
    retries: int

    def model(self, tier):
        return self.strong if tier == STRONG else self.cheap


def _number(name, default, cast=float):
    try:
        return cast(os.getenv(name, default))
    except ValueError:
        return cast(default)


def settings(strategy=None):
    """The routing configuration, from the environment, right now."""
    cheap = os.getenv("AMI_MODEL_CHEAP") or os.getenv("MODEL") or "gpt-4o-mini"
    strong = os.getenv("AMI_MODEL_STRONG") or "gpt-5.6-terra"
    strategy = (strategy or os.getenv("AMI_ROUTING") or "route").strip().lower()
    if strategy not in STRATEGIES:
        strategy = "route"
    raw = (os.getenv("AMI_MODEL_FALLBACK") or "").strip()
    fallback = [] if raw.lower() == "none" else [m.strip() for m in raw.split(",") if m.strip()]
    weights = dict(WEIGHTS)
    try:
        weights.update({k: float(v) for k, v in
                        json.loads(os.getenv("AMI_ROUTE_WEIGHTS") or "{}").items() if k in WEIGHTS})
    except (ValueError, AttributeError, TypeError):
        pass                                  # a bad override is ignored, not fatal
    undetermined = (os.getenv("AMI_ROUTE_UNDETERMINED") or STRONG).lower()
    return Settings(
        strategy=strategy, cheap=cheap, strong=strong,
        fallback=fallback if raw else None,   # None: "the other tier"
        threshold=_number("AMI_ROUTE_THRESHOLD", "2"),
        weights=weights,
        undetermined=undetermined if undetermined in (CHEAP, STRONG) else STRONG,
        max_escalations=max(0, _number("AMI_ROUTE_MAX_ESCALATIONS", "1", int)),
        min_reply_chars=max(0, _number("AMI_ROUTE_MIN_REPLY_CHARS", "15", int)),
        retries=max(0, _number("AMI_LLM_RETRIES", "1", int)),
    )


def features(text, work=None, note=None):
    """What the request looks like, as {feature: why}. Plain regexes over
    the customer's words, plus what working memory already holds."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no request text to classify")
    f = {}
    ids = set(_ORDER_ID.findall(text))
    known = bool(ids or _EMAIL.search(text) or
                 (work is not None and (work.orders or work.customer_email)))
    question = bool(_POLICY_Q.match(text)) and not ids

    m = _ACTION.search(text)
    if m and not question:
        f["risk"] = f"asks to {m.group(0).lower()}"
    pending = getattr(work, "pending", None)
    if pending:
        f["pending"] = f"a {pending['key'][0]} preview on {pending['key'][1]} awaits the customer's answer"
    before = getattr(work, "route_context", None)
    if _SHORT_REPLY.match(text) and before and before.get("classified") == STRONG \
            and before.get("turn") == getattr(work, "turn", None) - 1:
        f["inherited"] = f"a short reply to the previous turn, which was strong ({before.get('why')})"
    for name, rx in (("exception", _EXCEPTION), ("sensitive", _SENSITIVE), ("upset", _UPSET)):
        m = rx.search(text)
        if m:
            f[name] = repr(m.group(0).lower())
    letters = [c for c in text if c.isalpha()]
    if "upset" not in f and len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        f["upset"] = "written in capitals"
    if note and ("card number" in note or "override your instructions" in note):
        f["policy_flag"] = "the input policy flagged this message"
    if note and "REQUESTS SENT FOR HUMAN APPROVAL" in note:
        f["reviewer_news"] = "a reviewer's decision is to be explained"
    if len(ids) >= 2:
        f["multi_order"] = f"{len(ids)} orders"
    intents = [k for k, rx in _INTENTS.items() if rx.search(text)]
    if len(intents) >= 2 and _CONNECTOR.search(text) and len(text.split()) > 12:
        f["multi_intent"] = "+".join(intents)
    if not known and _REFERENCE.search(text):
        f["ambiguous"] = f"refers to {_REFERENCE.search(text).group(0).lower()!r} with no order identified"
    if not ids and _VAGUE.search(text):
        f["vague"] = repr(_VAGUE.search(text).group(0).lower())
    if not intents and not _SMALL_TALK.match(text):
        f["unknown_intent"] = "no known support intent"
    if len(text.split()) > 50:
        f["long"] = f"{len(text.split())} words"
    if work is not None and getattr(work, "failures", None):
        f["prior_refusal"] = f"{len(work.failures)} refusal(s) earlier"
    return f


def classify(text, work=None, note=None, strategy=None, cfg=None):
    """The routing decision for one turn, as a dict. Never raises: a
    request that cannot be classified goes to AMI_ROUTE_UNDETERMINED."""
    cfg = cfg or settings(strategy)
    try:
        f = features(text, work, note)
        score = sum(cfg.weights.get(k, 0) for k in f)
        classified = STRONG if score >= cfg.threshold else CHEAP
        why = ", ".join(f"{k} (+{cfg.weights.get(k, 0):g}: {v})" for k, v in f.items()) \
            or "a plain request"
        reason = f"{classified}: score {score:g} vs threshold {cfg.threshold:g}; {why}"
        undetermined = False
    except Exception as e:                    # fail safe, never fail the turn
        f, score, classified, undetermined = {}, None, cfg.undetermined, True
        reason = (f"{classified}: routing undetermined ({type(e).__name__}: {e}); "
                  f"AMI_ROUTE_UNDETERMINED={cfg.undetermined}")
    tier = classified if cfg.strategy == "route" else cfg.strategy
    if cfg.strategy != "route":
        reason = f"{tier}: strategy={cfg.strategy} (fixed); the router would have chosen {reason}"
    return {"strategy": cfg.strategy, "tier": tier, "classified": classified,
            "model": cfg.model(tier), "reason": reason, "score": score,
            "threshold": cfg.threshold, "features": f, "undetermined": undetermined}


# -- one turn's route --------------------------------------------------------

class ModelUnavailable(Exception):
    """Every model in the route failed for this call."""

    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("; ".join(f"{a['model']}: {a['error']}" for a in attempts)
                         or "no model to call")


def _redact(text):
    """Error text can echo request details; never let a key reach a log."""
    text = str(text)
    key = os.getenv("OPENAI_API_KEY") or ""
    if len(key) >= 8:
        text = text.replace(key, "[redacted]")
    return re.sub(r"\b(sk|mai|key)[-_][A-Za-z0-9_\-]{6,}", "[redacted]", text)


def _transient(e):
    """Worth retrying the same model: the request may well work next time."""
    try:
        import openai
        kinds = (openai.APIConnectionError, openai.InternalServerError)  # timeouts included
    except ImportError:                                  # pragma: no cover
        kinds = ()
    return isinstance(e, kinds + (TimeoutError, ConnectionError))


@dataclass
class Route:
    """The model choice for one turn, and everything that happened to it."""
    decision: dict
    cfg: Settings
    tier: str = CHEAP
    escalations: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    unavailable: bool = False
    actions_before: int = 0
    proposals: list = field(default_factory=list)
    work: object = None                    # whose turn: router.end() notes it there
    note: str = None                       # the turn's policy/reviewer note
    fixed: bool = False                    # one named model: no routing, no fallback

    @classmethod
    def for_model(cls, model):
        cfg = settings()
        cfg.cheap = cfg.strong = model
        cfg.fallback = []
        return cls(decision={"strategy": "fixed", "tier": CHEAP, "model": model,
                             "reason": f"model {model} named by the caller"},
                   cfg=cfg, tier=CHEAP, fixed=True)

    def model(self):
        return self.cfg.model(self.tier)

    def fallbacks(self, primary):
        if self.cfg.fallback is None:                    # default: the other tier
            other = self.cfg.cheap if self.tier == STRONG else self.cfg.strong
            chain = [other]
        else:
            chain = self.cfg.fallback
        return [m for i, m in enumerate(chain) if m != primary and m not in chain[:i]]

    def call(self, do):
        """Run do(model) -> (response, llm_event) against the route's model,
        retrying transient failures, then each fallback. Returns the response."""
        primary = self.model()
        attempts = []
        for model in [primary] + self.fallbacks(primary):
            for attempt in range(1 + self.cfg.retries):
                try:
                    response, event = do(model)
                except Exception as e:
                    err = _redact(f"{type(e).__name__}: {e}")[:300]
                    transient = _transient(e)
                    attempts.append({"model": model, "error": err, "transient": transient})
                    observe.log("route", event="model_failed", model=model, tier=self.tier,
                                attempt=attempt + 1, transient=transient, error=err)
                    if transient and attempt < self.cfg.retries:
                        continue
                    break
                self.calls.append({"tier": self.tier, "requested": model,
                                   "served_by": event.get("served_by"),
                                   "substituted": event.get("substituted"),
                                   "fallback_from": primary if model != primary else None,
                                   **{k: event.get(k) for k in
                                      ("ms", "tokens_in", "tokens_out", "cached",
                                       "cost", "cost_source")}})
                if model != primary:
                    observe.log("route", event="fallback", tier=self.tier,
                                requested=primary, used=model)
                self.failures += attempts
                return response
        self.failures += attempts
        self.unavailable = True
        observe.log("route", event="all_models_failed", tier=self.tier,
                    tried=[a["model"] for a in attempts])
        raise ModelUnavailable(attempts)

    def escalate(self, why):
        """cheap -> strong for the rest of the turn. True if it happened."""
        if self.fixed or self.decision.get("strategy") != "route" or self.tier == STRONG:
            return False
        if len(self.escalations) >= self.cfg.max_escalations:
            observe.log("route", event="escalation_refused", why=why,
                        limit=self.cfg.max_escalations)
            return False
        self.escalations.append({"from": self.tier, "to": STRONG, "why": why,
                                 "after_calls": len(self.calls)})
        self.tier = STRONG
        observe.log("route", event="escalated", why=why, model=self.cfg.strong)
        return True

    def summary(self):
        """What is logged, saved with the request, and scored by the evals.
        A token count or cost that any call lacked is None: unknown, not 0."""
        def total(key):
            vals = [c.get(key) for c in self.calls]
            return None if not vals or any(v is None for v in vals) else sum(vals)
        return {
            "strategy": self.decision.get("strategy"),
            "initial_tier": self.decision.get("tier"),
            "classified": self.decision.get("classified"),
            "final_tier": self.tier,
            "model": self.decision.get("model"),
            "models_used": sorted({c["requested"] for c in self.calls}),
            "served_by": sorted({c["served_by"] for c in self.calls if c.get("served_by")}),
            "reason": self.decision.get("reason"),
            "score": self.decision.get("score"),
            "features": self.decision.get("features"),
            "undetermined": self.decision.get("undetermined", False),
            "escalated": bool(self.escalations),
            "escalations": self.escalations,
            "calls": len(self.calls),
            "strong_calls": sum(c["tier"] == STRONG for c in self.calls),
            "tokens_in": total("tokens_in"),
            "tokens_out": total("tokens_out"),
            "cost": total("cost"),
            "cost_sources": sorted({c.get("cost_source") or "unknown" for c in self.calls}),
            "fallbacks": [c for c in self.calls if c["fallback_from"]],
            "failures": self.failures,
            "unavailable": self.unavailable,
        }


# The current thread's route, like observe.context and approvals' request:
# the planners and llm.complete reach it without new arguments everywhere.
_ctx = threading.local()


def begin(text, work=None, note=None, strategy=None):
    """A turn starts: classify it and make it the current route."""
    cfg = settings(strategy)
    decision = classify(text, work, note, cfg=cfg)
    route = Route(decision=decision, cfg=cfg, tier=decision["tier"], work=work, note=note,
                  actions_before=len(getattr(work, "actions", []) or []))
    _ctx.route = route
    observe.log("route", event="decision", tier=decision["tier"], model=decision["model"],
                strategy=cfg.strategy, score=decision["score"], reason=decision["reason"])
    return route


def end():
    """A turn ends: returns its summary (or None) and clears the route."""
    route = getattr(_ctx, "route", None)
    _ctx.route = None
    if route is None:
        return None
    s = route.summary()
    if route.work is not None and not route.fixed:
        # What the next turn inherits if it is only "yes": this turn's own
        # classification (not a fixed strategy's tier), and why.
        feats = route.decision.get("features") or {}
        route.work.route_context = {
            "turn": getattr(route.work, "turn", None),
            "classified": STRONG if (route.decision.get("classified") == STRONG
                                     or route.escalations) else CHEAP,
            "why": ", ".join(k for k in feats if k != "inherited")
                   or ("escalated" if route.escalations else "plain request")}
    observe.log("route", event="summary", **{k: s[k] for k in
                ("initial_tier", "final_tier", "escalated", "calls", "strong_calls",
                 "tokens_in", "tokens_out", "cost", "unavailable")})
    return s


def current():
    return getattr(_ctx, "route", None)


def snapshot():
    """The current route's summary so far, or None — saved with a proposal."""
    route = current()
    return route.summary() if route else None


def escalate(why):
    route = current()
    return bool(route and route.escalate(why))


def can_escalate():
    """A routed turn still on the cheap tier. Whether the escalation limit
    allows one is escalate()'s decision, so that a refusal is logged too."""
    route = current()
    return bool(route and not route.fixed and route.decision.get("strategy") == "route"
                and route.tier == CHEAP)


# -- signals the planners report ---------------------------------------------

STATE_CHANGING = {"cancel_order", "start_return"}


def tool_result(name, result):
    """After every tool call: escalate on what it shows about the model."""
    route = current()
    if route is None:
        return
    if isinstance(result, dict) and result.get("pending_approval"):
        route.proposals.append(result.get("request_id"))
    if isinstance(result, dict) and result.get("retry"):
        escalate(f"{name} rejected the call as malformed: {str(result.get('error'))[:120]}")
    elif name in STATE_CHANGING:
        escalate(f"the model asked for {name}, which changes the customer's account")


def inadequate(reply, work=None, user_text="", context="", cfg=None):
    """Why this answer should not go out from the cheap tier, or None.
    Observable checks first; the model's own hedging last."""
    cfg = cfg or settings()
    text = (reply or "").strip()
    if not text:
        return "empty answer"
    if len(text) < cfg.min_reply_chars:
        return f"answer too short ({len(text)} chars)"
    if work is not None:
        from ami import policy           # policy -> approvals -> router: import late
        bad = policy.unverified_identifiers(text, work, user_text, context)
        if bad:
            return f"quotes {', '.join(bad)}, which no tool returned"
    if _HEDGE.search(text):
        return f"the answer says it is unsure ({_HEDGE.search(text).group(0)!r})"
    return None


def review_answer(reply, work=None, convo=None, longterm=None):
    """The planner's check before returning an answer: None unless the
    route could still escalate AND the answer is inadequate."""
    if not can_escalate():
        return None
    user_text = ""
    for m in reversed(getattr(convo, "history", []) or []):
        if m.get("role") == "user":
            user_text = m.get("content") or ""
            break
    context = ""
    if longterm is not None and work is not None:
        try:
            context = longterm.recall(work.customer_email, getattr(work, "session_id", None)) or ""
        except Exception:
            context = ""
    why = inadequate(reply, work, user_text, context)
    return f"cheap answer inadequate: {why}" if why else None


def unavailable_reply(work=None):
    """What to say when no model answered. Only what is true: anything
    this turn already did stays done; nothing else changed."""
    route = current()
    done = list((getattr(work, "actions", None) or [])[route.actions_before if route else 0:])
    parts = ["I'm sorry, I can't reach our support systems right now, so I can't "
             "finish answering this."]
    news = _reviewer_news(route)
    if news:
        parts.append(news)
    if route and route.proposals:
        parts.append(f"Your request {', '.join(p for p in route.proposals if p)} is saved and "
                     f"waiting for a staff member to review it; nothing has changed yet.")
    if done:
        parts.append("Already done: " + "; ".join(done) + ".")
    elif not (route and route.proposals):
        parts.append("Nothing on your account was changed.")
    parts.append("Please try again in a few minutes, or ask for a human agent.")
    return " ".join(parts)


def _reviewer_news(route):
    """A reviewer's decision that approvals.sync folded into this turn's note
    must still reach the customer when no model can write the reply: sync
    has already marked it told. Customer-safe labels only."""
    if not route or not route.note or "REQUESTS SENT FOR HUMAN APPROVAL" not in route.note:
        return None
    from ami import approvals             # approvals -> router: import late
    lines = []
    for rid in dict.fromkeys(re.findall(r"REQ-[0-9a-f]{12}", route.note)):
        try:
            r = approvals.default().get(rid)
        except approvals.ApprovalError:
            continue
        view = approvals.customer_view(r)
        rma = (r.get("outcome") or {}).get("rma") if r["execution_status"] == "succeeded" else None
        lines.append(f"{rid} ({view['action'].lower()} of {view['order_id']}): {view['label']}"
                     + (f", {rma}" if rma else ""))
    return ("Update on your request: " + "; ".join(lines) + ".") if lines else None


def label():
    """One line for a page header: which models, which strategy."""
    cfg = settings()
    if cfg.strategy == "route":
        return f"{cfg.cheap} / {cfg.strong} (routed)"
    return f"{cfg.model(cfg.strategy)} ({cfg.strategy} only)"
