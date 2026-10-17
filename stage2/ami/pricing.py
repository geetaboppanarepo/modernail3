"""What a call costs.

Prices are dollars per MILLION tokens. Input and output are billed at
different rates — output is usually 5-10x input — so the trace has to
record the two separately or the cost number is meaningless.

VERIFY THESE BEFORE QUOTING THEM. Published third-party figures for
gpt-5.6-terra disagree ($2/$12 vs $2.50/$15), OpenAI's own pricing page
refused to load, and the class proxy may bill differently from list price
or not at all. Treat every dollar figure here as an estimate whose job is
to show RELATIVE cost — which turns are expensive, and why.

AMI_PRICES adds or corrects rows without editing this file, as JSON:
    AMI_PRICES='{"my-model": [0.40, 0.04, 1.60]}'     # input, cached-in, output

rates() falls back to DEFAULT for a model it does not know, which is fine
for a rough dashboard. price() does not: the routing evals compare
strategies by cost, and a made-up price would decide that comparison. It
returns the provider's own figure when the response carries one, the
configured rate when the model is listed, and otherwise None, "unknown".
"""

import json
import os

PRICES = {
    #  model                     input   cached-in   output
    "gpt-5.6-terra":            (2.00,     0.20,     12.00),
    "gpt-5.6-sol":              (5.00,     0.50,     30.00),
    "gpt-5.6-luna":             (0.20,     0.02,      1.20),
    "gpt-4o-mini":              (0.15,     0.075,     0.60),
}

DEFAULT = (2.00, 0.20, 12.00)      # used when a model name is unknown


def _table():
    table = dict(PRICES)
    try:
        extra = json.loads(os.getenv("AMI_PRICES") or "{}")
        table.update({k.lower(): tuple(float(x) for x in v) for k, v in extra.items()
                      if isinstance(v, (list, tuple)) and len(v) == 3})
    except (ValueError, TypeError, AttributeError):
        pass                                   # a bad override is ignored, not fatal
    return table


def lookup(model):
    """The price row for a model, ignoring any date suffix, or None.
    The longest matching name wins, so "gpt-4o-mini" is not priced as "gpt-4o"."""
    if not model:
        return None
    name = model.lower()
    hits = [k for k in _table() if name.startswith(k)]
    return _table()[max(hits, key=len)] if hits else None


def rates(model):
    """Find the price row for a model, ignoring any date suffix."""
    return lookup(model) or DEFAULT


def price(model, input_tokens, output_tokens, cached_tokens=0, provider_cost=None):
    """(dollars, source) for one call, never invented:
    ("provider") the response's own cost, ("configured") a listed rate
    times the tokens, or (None, "unknown") with no usage or no listed rate."""
    if provider_cost is not None:
        try:
            return float(provider_cost), "provider"
        except (TypeError, ValueError):
            pass
    if input_tokens is None or output_tokens is None or lookup(model) is None:
        return None, "unknown"
    return cost(model, input_tokens, output_tokens, cached_tokens), "configured"


def cost(model, input_tokens=0, output_tokens=0, cached_tokens=0):
    """Dollars for one call. Cached input is billed at the cheaper rate."""
    rate_in, rate_cached, rate_out = rates(model)
    fresh = max(0, (input_tokens or 0) - (cached_tokens or 0))
    return (fresh * rate_in
            + (cached_tokens or 0) * rate_cached
            + (output_tokens or 0) * rate_out) / 1_000_000


def usd(amount):
    """Format small dollar amounts so they stay readable."""
    if amount >= 1:
        return f"${amount:,.2f}"
    if amount >= 0.01:
        return f"${amount:.3f}"
    return f"${amount:.5f}"
