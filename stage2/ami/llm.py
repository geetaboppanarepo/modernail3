"""The single place where we talk to the model.

Reads OPENAI_API_KEY and OPENAI_BASE_URL from .env. The base URL points at
the class LLM proxy, which speaks the OpenAI chat-completions API.

Which model a call goes to is the router's decision (router.py): the
planners call complete() without naming one, and the current turn's route
picks the tier's model, retries a transient failure, and falls back to the
next model when one is down. A caller that names a model (golden.py's
judge) gets exactly that model, with retries and no fallback.

AMI_LLM_TIMEOUT (seconds, default 60) bounds each call. The client's own
retries are off: router.Route.call does the retrying (AMI_LLM_RETRIES), so
a stalled model costs one timeout per retry, not the client's default of
600 seconds three times over.
"""

import os
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

from ami import observe
from ami import pricing
from ami import router

load_dotenv()

MODEL = os.getenv("MODEL", "gpt-4o-mini")

try:
    TIMEOUT = float(os.getenv("AMI_LLM_TIMEOUT", "60"))
except ValueError:
    TIMEOUT = 60.0

_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
    timeout=TIMEOUT,
    max_retries=0,
)


RATE_LIMIT_TRIES = 6


def _call(kwargs):
    """One API call, waiting out the proxy when we ask too fast.

    The class proxy allows 60 requests a minute. That is generous for a
    person typing and nowhere near enough for an eval suite firing turns
    back to back, which is how a 429 first showed up: as fifteen agent
    "failures" that were nothing of the sort. A rate limit is not an error
    to report, it is a queue to join.
    """
    for attempt in range(RATE_LIMIT_TRIES):
        try:
            return _client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            # Two different things arrive as a 429, and only one is worth
            # waiting out. "Slow down" clears in under a minute. "This
            # key's budget is used up" never clears, and retrying it buys
            # a minute of silence before the identical failure — which is
            # exactly how a finished eval run looks like a hung one.
            if "insufficient_quota" in str(e) or "budget" in str(e):
                raise
            if attempt == RATE_LIMIT_TRIES - 1:
                raise
            wait = 2 ** (attempt + 1)      # 2, 4, 8, 16, 32 — a minute in all,
                                           # which is the window being enforced
            observe.log("llm", model=kwargs.get("model"), ms=0,
                        error=f"rate limited, waiting {wait}s")
            time.sleep(wait)


def complete(messages, tools=None, model=None, temperature=0.3):
    """One model call, timed and logged. Everything goes through here.

    model=None: the current route's model (router.begin), or MODEL when no
    turn is being routed. Raises router.ModelUnavailable when every model
    the route may use has failed."""
    route = router.current() if model is None else None
    if route is None:
        route = router.Route.for_model(model or MODEL)
    tier = None if route.fixed else route.tier
    return route.call(lambda m: _once(messages, tools, m, temperature, tier))


def _once(messages, tools, model, temperature, tier=None):
    """Exactly one call to `model`. Returns (response, the logged event)."""
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        kwargs["tools"] = tools

    t = observe.timer().__enter__()
    try:
        response = _call(kwargs)
    except Exception as e:
        t.__exit__()
        observe.log("llm", model=model, tier=tier, ms=t.ms,
                    error=router._redact(f"{type(e).__name__}: {e}"))
        raise
    t.__exit__()

    message = response.choices[0].message
    usage = getattr(response, "usage", None)
    served = getattr(response, "model", None) or model

    # Input and output are billed at different rates, so record them apart.
    # total_tokens alone cannot be priced. No usage at all means the counts
    # are UNKNOWN (None), not zero, and so is the cost.
    if usage is None:
        tokens_in = tokens_out = cached = total = None
    else:
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        total = getattr(usage, "total_tokens", None)
    cost, source = pricing.price(served, tokens_in, tokens_out, cached,
                                 provider_cost=getattr(usage, "cost", None))

    # Did the provider answer with the model we asked for? A dated suffix
    # ("gpt-x" -> "gpt-x-2026-07-09") is the same model; anything else is a
    # substitution, and a cost or quality comparison between "models" that
    # were silently the same model measures nothing about the models.
    substituted = not str(served).lower().startswith(str(model).lower())
    event = observe.log("llm",
                        model=model,
                        tier=tier,
                        ms=t.ms,
                        messages=len(messages),
                        tokens=total,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cached=cached,
                        cost=cost,
                        cost_source=source,
                        served_by=served,
                        substituted=substituted,
                        finish=getattr(response.choices[0], "finish_reason", None),
                        tool_calls=len(getattr(message, "tool_calls", None) or []))
    return response, event


def chat(messages, model=MODEL, temperature=0.3):
    """Send a list of {"role", "content"} messages, get back the reply text."""
    return complete(messages, model=model,
                    temperature=temperature).choices[0].message.content
