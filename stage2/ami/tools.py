"""Element 4 of the agent: ACTION — the tools, and the guardrails around them.

Two ideas to notice here:

1. Every tool returns a plain dict. On failure it returns
   {"error": ...} instead of raising, so the model can read the problem
   and explain it to the customer.

2. The guardrails live in the TOOLS, not in the prompt. A prompt rule is
   a suggestion the model can talk itself out of. A check inside
   cancel_order() is a rule it cannot get around.

Stage 2: cancel_order and start_return declare a `confirmed` flag in
their schema, and their descriptions tell the model to preview first.
The tool functions themselves are unchanged — the policy layer strips
the flag and decides whether the call goes through (policy.guarded_run).

The orders live in SQLite (store.py), shared by every process. A tool
that changes an order hands store.change() its guardrail, so the check
and the change happen in one transaction: nobody can change the order in
between. An approved action also passes `op_id` (its request id: a retry
returns the first result) and `expect` (the order as the reviewer saw
it). Those two are for approvals.resume() only; the model cannot set them.
"""

from datetime import date

from ami import knowledge
from ami import observe
from ami import store

# --------------------------------------------------------------------------
# The tools themselves
# --------------------------------------------------------------------------


def find_orders(email):
    """Look up a customer's orders when they don't know the order number."""
    hits = [
        {"order_id": o["order_id"], "item": o["item"],
         "status": o["status"], "ordered_on": o["ordered_on"]}
        for o in store.find(email)
    ]
    if not hits:
        return {"error": f"No orders found for {email}."}
    return {"orders": hits}


def get_order(order_id):
    """Full detail for one order."""
    order = store.get(order_id)
    if not order:
        return {"error": f"No order found with id {order_id}."}
    return {
        "order_id": order["order_id"],
        "item": order["item"],
        "price": order["price"],
        "status": order["status"],
        "ordered_on": order["ordered_on"],
        "delivered_on": order["delivered_on"],
        "eta": order.get("eta"),
    }


def track_package(order_id):
    """The carrier scan history for an order."""
    order = store.get(order_id)
    if not order:
        return {"error": f"No order found with id {order_id}."}
    if not order["tracking"]:
        return {"error": "No tracking events yet for this order."}
    return {
        "carrier": order["carrier"],
        "eta": order.get("eta"),
        "events": [{"date": d, "detail": t} for d, t in order["tracking"]],
    }


def cancel_order(order_id, *, op_id=None, expect=None):
    """Cancel an order — only allowed before it ships. GUARDRAIL."""
    return store.change(
        order_id, tool="cancel_order", check=_cancel_refusal, to="cancelled",
        op_id=op_id, expect=expect,
        result=lambda order, _: {
            "cancelled": True,
            "order_id": order["order_id"],
            "refund_amount": order["price"],
            "refund_eta": "3-5 business days to the original payment method",
        })


def start_return(order_id, reason, *, op_id=None, expect=None):
    """Open a return — only for delivered orders inside the return window. GUARDRAIL."""
    return store.change(
        order_id, tool="start_return", check=_return_refusal, to="return started",
        op_id=op_id, expect=expect, return_reason=reason,
        result=lambda order, rma: {
            "rma": rma,
            "order_id": order["order_id"],
            "refund_amount": order["price"],
            "instructions": "Drop off at any UPS Store with the QR code emailed "
                            "to you. Refund issues once we scan the item.",
        })


def _cancel_refusal(order_id, order):
    """Why cancel_order would refuse this order, or None."""
    if not order:
        return f"No order found with id {order_id}."
    if order["status"] in ("shipped", "delivered"):
        return (f"Order {order_id} already {order['status']} and cannot "
                f"be cancelled. It can be returned instead.")
    if order["status"] == "cancelled":
        return f"Order {order_id} is already cancelled."
    return None


def _return_refusal(order_id, order):
    """Why start_return would refuse this order, or None."""
    if not order:
        return f"No order found with id {order_id}."
    if order["status"] != "delivered":
        return (f"Order {order_id} is '{order['status']}', not delivered "
                f"yet, so it can't be returned.")
    days = (date.today() - date.fromisoformat(order["delivered_on"])).days
    if days > store.RETURN_WINDOW_DAYS:
        return (f"Delivered {days} days ago, past the "
                f"{store.RETURN_WINDOW_DAYS}-day return window. "
                f"A human agent can review an exception.")
    return None


def refusal(name, args):
    """Why the guardrail inside `name` would refuse this call, or None.

    The same checks the tool itself runs, read from the order store and
    changing nothing — so the policy layer can ask before it gates a call
    that could never do anything."""
    order_id = str(args.get("order_id", ""))
    order = store.get(order_id)
    if name == "cancel_order":
        return _cancel_refusal(order_id, order)
    if name == "start_return":
        return _return_refusal(order_id, order)
    return None


def search_knowledge(question):
    """Look up what is written down. The RAG tool.

    Everything else here reads the order database. This one reads the
    filing cabinet: the rules a human support agent would have been
    trained on, which the agent can now quote instead of guessing at.

    One argument, on purpose — see knowledge.search() for why the
    obvious second one (a category to search within) was removed.
    """
    hits = knowledge.search(question, k=3)
    passages = []
    for h in hits:
        p = {"source": h["source"], "category": h["category"],
             "policy": h["heading"], "text": h["text"]}
        # The document's opening rule rides along with any section of it
        # (knowledge.lead), so "the return policy" always arrives with
        # the 30-day window, whichever section ranked first.
        rule = knowledge.lead(h["source"]) if "—" in h["heading"] else None
        if rule:
            p["document_rule"] = rule
        passages.append(p)
    return {"passages": passages}


def escalate(summary):
    """Hand off to a human. The honest answer when no other tool fits."""
    return {
        "escalated": True,
        "ticket": "ESC-4417",
        "message": "A human agent will email you within 24 hours.",
        "summary": summary,
    }


# --------------------------------------------------------------------------
# Descriptions the model reads to decide which tool to call
# --------------------------------------------------------------------------

def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


SCHEMAS = [
    _tool("find_orders",
          "Find a customer's orders by email address. Use when the customer "
          "does not know their order number.",
          {"email": {"type": "string", "description": "Customer email address"}},
          ["email"]),

    _tool("get_order",
          "Get the status and details of one order by its order number.",
          {"order_id": {"type": "string",
                        "description": "Order number, e.g. 112-1111111-1111111"}},
          ["order_id"]),

    _tool("track_package",
          "Get carrier tracking events and delivery estimate for an order.",
          {"order_id": {"type": "string", "description": "Order number"}},
          ["order_id"]),

    _tool("cancel_order",
          "Cancel an order that has not shipped yet and refund it. The first "
          "call returns what would happen; ask the customer, then call again "
          "with confirmed=true once they have said yes.",
          {"order_id": {"type": "string", "description": "Order number"},
           "confirmed": {"type": "boolean",
                         "description": "true only after the customer confirmed"}},
          ["order_id"]),

    _tool("start_return",
          "Start a return for a delivered order and issue an RMA number. The "
          "first call returns what would happen; ask the customer, then call "
          "again with confirmed=true once they have said yes.",
          {"order_id": {"type": "string", "description": "Order number"},
           "reason": {"type": "string",
                      "description": "Why the customer is returning it, in their words"},
           "confirmed": {"type": "boolean",
                         "description": "true only after the customer confirmed"}},
          ["order_id", "reason"]),

    _tool("search_knowledge",
          "Look up what is written down, and quote it back rather than "
          "guessing. Covers four kinds of knowledge: what the customer is "
          "entitled to, what you are allowed to do, how to phrase something "
          "difficult, and the law a policy rests on. Use it for any question "
          "about the rules themselves.",
          {"question": {"type": "string",
                        "description": "The question, in plain words"}},
          ["question"]),

    _tool("escalate",
          "Hand the conversation to a human agent. Use when the customer asks "
          "for a human, is very upset, or the request is outside these tools.",
          {"summary": {"type": "string",
                       "description": "One-line summary of the issue for the human agent"}},
          ["summary"]),
]

REGISTRY = {
    "find_orders": find_orders,
    "get_order": get_order,
    "track_package": track_package,
    "cancel_order": cancel_order,
    "start_return": start_return,
    "search_knowledge": search_knowledge,
    "escalate": escalate,
}


INTERNAL = {"op_id", "expect"}      # set by approvals.resume, never by the model

_BY_NAME = {s["function"]["name"]: s["function"]["parameters"] for s in SCHEMAS}
_TYPES = {"string": str, "boolean": bool, "integer": int, "number": (int, float),
          "object": dict, "array": list}


def validate(name, args):
    """Why this call does not match the tool's schema, or None.

    The schema the model was shown is the contract: a known tool, only the
    properties it declares, every required one present and non-empty, each
    of the declared JSON type. The internal arguments of an approved run
    (op_id, expect) are refused by name — the model can never supply them."""
    params = _BY_NAME.get(name)
    if params is None:
        return f"no such tool {name!r}"
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    internal = sorted(INTERNAL & set(args))
    if internal:
        return (f"{', '.join(internal)} cannot be set by the model: they are "
                f"set only by the approval workflow")
    extra = sorted(set(args) - set(params["properties"]))
    if extra:
        return f"unexpected arguments {extra}"
    missing = [k for k in params["required"] if args.get(k) in (None, "")]
    if missing:
        return f"missing required arguments {missing}"
    for key, value in args.items():
        want = _TYPES.get(params["properties"][key].get("type"))
        bad = want is not None and (not isinstance(value, want) or
                                    (want is not bool and isinstance(value, bool)))
        if bad:
            return f"{key} must be a {params['properties'][key]['type']}"
    return None


def run(name, args, **internal):
    """Execute a tool the model asked for. Never raises — errors come back as data."""
    with observe.timer() as t:
        result = _dispatch(name, args, internal)
    observe.log("tool", tool=name, args=args, ms=t.ms,
                ok="error" not in result,
                error=result.get("error"),
                retryable=bool(result.get("retry")))
    return result


def _dispatch(name, args, internal):
    fn = REGISTRY.get(name)
    if not fn:
        return {"error": f"No such tool: {name}"}
    if INTERNAL & set(args) or set(internal) - INTERNAL:
        return {"error": f"Bad call to {name}: unexpected arguments", "retry": True}
    try:
        return fn(**args, **internal)
    except TypeError as e:
        # The agent called its own tool wrongly. That is not a policy
        # refusal — it is a mistake it can fix, so say so explicitly.
        return {"error": f"Bad call to {name}: {e}", "retry": True}
