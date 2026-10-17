"""Routing metrics: what each strategy cost, and what it bought.

evals.py --compare runs every case three ways — cheap-only, strong-only,
routed — and hands each run to row(). summarize() turns one strategy's
rows into the numbers below, and report() lays the three side by side.

    Four kinds of cost, never mixed:
    billed cost       what the provider itself reported in the response usage.
                      The class proxy reports none, so this is n/a there
    estimated cost    configured list price (ami/pricing.py, AMI_PRICES) of the
                      model that SERVED each call, times its measured tokens
    projected cost    the same tokens at the REQUESTED model's list price: what
                      the run would cost had the provider honoured the names.
                      A projection, not a measurement
    unknown           a call with no usage, or no listed price: the request's
                      figure is None, and totals are over the known requests
                      only, with the count — never as if the rest were free
    (`cost` is billed where known, else estimated: the budget guard and the
    per-category table use it.)

    The comparison itself gets a verdict (comparison()): "simulated" (no
    model), "model-substitution" (a provider answered with another model
    than the one requested, or cheap and strong were served by the same
    model), or "distinct-models". Only the last may claim a measured
    model-specific saving, and the report says so in its first lines.
    quality           100 x checks passed / checks made, per request (the
                      case's own assertions: evals.checks), averaged
    task success      every check passed
    escalation rate   requests where the route moved cheap -> strong mid-turn
    HITL recall       TP / (TP + FN) over the needs_approval labels
    escalation prec.  TP / (TP + FP), same labels (ami/hitl_eval.py)
    hand-off rates    the same two, counting an ESC ticket as "sent to a
                      human" too, against needs_approval OR the `human` label
    routing accuracy  per turn: the router's classification vs the hand label

A metric with nothing to measure is None and printed as "n/a" — never 0,
never 100%.
"""

import os
from urllib.parse import urlsplit

from ami import hitl_eval
from ami.pricing import usd

STRATEGIES = ("cheap", "strong", "route")
NAMES = {"cheap": "A. cheap model only", "strong": "B. strong model only",
         "route": "C. intelligent routing"}


def provider_host():
    return urlsplit(os.getenv("OPENAI_BASE_URL") or "").netloc or "(unset)"


def _labels(case, n):
    route = case.get("route")
    if route is None:
        return [None] * n
    return [route] * n if isinstance(route, str) else list(route)[:n] + [None] * (n - len(route))


def row(case, r, checks, strategy):
    """One request (one case, one run, one strategy), flattened."""
    turns = r.get("turns") or []
    passed = sum(ok for _, ok, _ in checks)
    dims = {}
    for dim, ok, _ in checks:
        d = dims.setdefault(dim, [0, 0])
        d[0] += ok
        d[1] += 1
    return {
        "case": case["name"], "category": case.get("category"), "strategy": strategy,
        "request_ids": [t["request_id"] for t in turns],
        "pass": bool(checks) and passed == len(checks),
        "quality": round(100 * passed / len(checks), 1) if checks else None,
        "dims": dims,
        "fails": [reason for _, ok, reason in checks if not ok],
        "route_label": _labels(case, len(case["turns"])),
        "tiers": [t["initial_tier"] for t in turns],
        "classified": [t["classified"] for t in turns],
        "final_tiers": [t["final_tier"] for t in turns],
        "reasons": [t["reason"] for t in turns],
        "escalated": any(t["escalated"] for t in turns),
        "escalations": [e["why"] for t in turns for e in t["escalations"]],
        "fallbacks": sum(len(t["fallbacks"]) for t in turns),
        "unavailable": any(t["unavailable"] for t in turns),
        "calls": sum(t["calls"] for t in turns),
        "strong_calls": sum(t["strong_calls"] for t in turns),
        "tokens_in": r.get("tokens_in"), "tokens_out": r.get("tokens_out"),
        "cost": r.get("cost"), "projected_cost": r.get("projected_cost"),
        "cost_sources": r.get("cost_sources") or [],
        "billed_cost": r.get("billed_cost"), "estimated_cost": r.get("estimated_cost"),
        "unknown_cost_calls": r.get("unknown_cost_calls", 0),
        "substituted_calls": r.get("substituted_calls", 0),
        "model_pairs": r.get("model_pairs") or [],
        "requested_models": r.get("requested_models") or [],
        "served_by": r.get("served_by") or [],
        "ms": r.get("ms"),
        "needs_approval": case.get("needs_approval"),
        "approval_requested": r.get("approval_requested"),
        "requests": r.get("approval_requests") or [],
        "human": case.get("human"),
        "handed_off": bool(r.get("escalation")),
        "escalation_ticket": r.get("escalation"),
        "review": [d["decision"] for d in (r.get("review") or [])],
        "error": r.get("error"),
        "reply": r.get("reply"),
    }


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _sum_known(rows, key):
    """(sum over rows where it is known, how many were known)."""
    known = [r.get(key) for r in rows if r.get(key) is not None]
    return (sum(known) if known else None), len(known)


def summarize(rows):
    """One strategy's rows -> its metrics."""
    n = len(rows)
    total, n_cost = _sum_known(rows, "cost")
    proj, n_proj = _sum_known(rows, "projected_cost")
    successes = sum(r["pass"] for r in rows)
    resolved_cost = [r["cost"] for r in rows if r["pass"]]
    calls = sum(r["calls"] for r in rows)
    hitl = [(r["needs_approval"], bool(r["approval_requested"])) for r in rows
            if isinstance(r["needs_approval"], bool)]
    c = hitl_eval.confusion(hitl)
    handoff_rows = [r for r in rows if isinstance(r["needs_approval"], bool)
                    or isinstance(r["human"], bool)]
    h = hitl_eval.confusion(
        (bool(r["needs_approval"]) or bool(r["human"]),
         bool(r["approval_requested"]) or r["handed_off"]) for r in handoff_rows)
    turns = [(lab, cls, r["case"]) for r in rows
             for lab, cls in zip(r["route_label"], r["classified"]) if lab and cls]
    kinds = {}
    for key in ("billed_cost", "estimated_cost", "projected_cost"):
        tot, k = _sum_known(rows, key)
        kinds[key] = {"known": k, "requests": n, "total": tot if k == n and n else None,
                      "known_sum": tot, "known_avg": tot / k if tot is not None and k else None}
    tin, n_tin = _sum_known(rows, "tokens_in")
    tout, _ = _sum_known(rows, "tokens_out")
    dims = {}
    for r in rows:
        for dim, (p, t) in r["dims"].items():
            d = dims.setdefault(dim, [0, 0])
            d[0] += p
            d[1] += t
    return {
        "requests": n,
        "cost_kinds": kinds,
        "unknown_cost_requests": sum(r["cost"] is None for r in rows),
        "failed_requests": n - successes,
        "substituted_calls": sum(r.get("substituted_calls", 0) for r in rows),
        "served_by": sorted({m for r in rows for m in r["served_by"]}),
        "cost_known": n_cost,
        "total_cost": total if n_cost == n and n else None,
        "known_cost_sum": total,
        "known_avg_cost": total / n_cost if total is not None and n_cost else None,
        "known_projected_avg": proj / n_proj if proj is not None and n_proj else None,
        "avg_cost": total / n if total is not None and n_cost == n and n else None,
        "cost_per_success": (sum(resolved_cost) / successes
                             if successes and None not in resolved_cost else None),
        "projected_known": n_proj,
        "projected_total": proj if n_proj == n and n else None,
        "projected_avg": proj / n if proj is not None and n_proj == n and n else None,
        "tokens_in": tin if n_tin == n and n else None,
        "tokens_out": tout if n_tin == n and n else None,
        "avg_ms": _mean(r["ms"] for r in rows),
        "quality": _mean(r["quality"] for r in rows),
        "successes": successes,
        "success_rate": successes / n if n else None,
        "escalation_rate": sum(r["escalated"] for r in rows) / n if n else None,
        "escalated": sum(r["escalated"] for r in rows),
        "calls": calls,
        "strong_share": sum(r["strong_calls"] for r in rows) / calls if calls else None,
        "fallbacks": sum(r["fallbacks"] for r in rows),
        "unavailable": sum(r["unavailable"] for r in rows),
        "hitl": {**c, **hitl_eval.metrics(c)},
        "handoff": {**h, **hitl_eval.metrics(h)},
        "routing_turns": len(turns),
        "routing_accuracy": (sum(lab == cls for lab, cls, _ in turns) / len(turns)
                             if turns else None),
        "under_routed": sorted({case for lab, cls, case in turns
                                if lab == "strong" and cls == "cheap"}),
        "over_routed": sorted({case for lab, cls, case in turns
                               if lab == "cheap" and cls == "strong"}),
        "dims": {d: {"passed": p, "checks": t, "rate": p / t if t else None}
                 for d, (p, t) in dims.items()},
        "errors": [r["case"] for r in rows if r["error"]],
    }


def comparison(rows, summaries, scripted=False):
    """What kind of comparison this is: (verdict, why).

    simulated           no model was called
    model-substitution  the provider answered some call with a different
                        model than requested, or the cheap-only and
                        strong-only runs were served by the same model(s):
                        no model-specific cost or quality difference was measured
    distinct-models     every call was served by the model requested, and
                        cheap and strong were different models
    """
    if scripted:
        return "simulated", "no model was called"
    subs = sum(r.get("substituted_calls", 0) for r in rows)
    def swapped(pair):             # same rule as llm.py: a dated suffix is the same model
        asked, _, got = pair.partition(" -> ")
        return not got.lower().startswith(asked.lower())
    pairs = sorted({p for r in rows for p in r.get("model_pairs", []) if swapped(p)})
    cheap = set(summaries.get("cheap", {}).get("served_by") or [])
    strong = set(summaries.get("strong", {}).get("served_by") or [])
    if subs:
        return "model-substitution", (f"{subs} call(s) were served by a different model than "
                                      f"requested: {', '.join(pairs)}")
    if cheap and cheap == strong:
        return "model-substitution", (f"cheap-only and strong-only were served by the same "
                                      f"model(s): {', '.join(sorted(cheap))}")
    if not cheap or not strong:
        return "model-substitution", "no served model identifier was recorded for a strategy"
    return "distinct-models", (f"cheap served by {', '.join(sorted(cheap))}; strong by "
                               f"{', '.join(sorted(strong))}; every call as requested")


def safe(summaries):
    """The safety gate: every strategy caught every action that needed a
    human (recall defined and 1.0). One miss anywhere fails the run."""
    return all(s["hitl"]["hitl_recall"] == 1.0 for s in summaries.values() if s["requests"])


# -- the report ---------------------------------------------------------------

def _money(x):
    return "n/a" if x is None else usd(x)


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _num(x, fmt="{:.1f}"):
    return "n/a" if x is None else fmt.format(x)


def _partial(m, full, known_avg, known, n):
    """The full figure, or the average over the known requests, labelled."""
    if m[full] is not None:
        return _money(m[full])
    if m[known_avg] is not None:
        return f"{_money(m[known_avg])} ({m[known]}/{n} known)"
    return "n/a"


def _rate(m, key, counts):
    v = m[key]
    num, den = counts
    return f"n/a (0/0)" if v is None else f"{100 * v:.0f}% ({num}/{den})"


def _bar(value, top, width=30):
    if value is None or not top:
        return ""
    return "█" * max(1, round(width * value / top)) if value > 0 else "·"


def _caveats(rows, summaries, meta, verdict):
    kind, why = verdict
    out = []
    if kind == "simulated":
        out.append("**Comparison type: SIMULATED.** No model was called: a scripted provider "
                   "played each case's tool calls, under the real router, policy layer, tools and "
                   "approvals store. It reports no token usage, so **every cost below is unknown "
                   "(n/a)**, and reply-text checks are skipped (a scripted reply is a placeholder). "
                   "What this run does measure: routing decisions and accuracy, which tier each "
                   "call used, the policy checks, HITL recall and escalation precision. "
                   "No savings are measured or claimed.")
    elif kind == "model-substitution":
        served = sorted({m for r in rows for m in r["served_by"]})
        requested = sorted({m for r in rows for m in r["requested_models"]})
        out.append(f"**Comparison type: MODEL SUBSTITUTION — no model-specific savings are "
                   f"measured or claimed.** {why}. Requested: {', '.join(requested) or '?'}; "
                   f"served: {', '.join(served) or '?'}. The provider served one model for "
                   f"every request, so the strategies differ only in routing behaviour "
                   f"(escalations, redrafts), not in the model that answered: quality and "
                   f"latency differences between them are not model differences, and the "
                   f"estimated cost per token is the same for all three. *Projected cost* prices "
                   f"the measured tokens at each requested model's listed price: what this run "
                   f"would have cost if the provider had honoured the model names. A projection, "
                   f"not a bill, and not a measured saving.")
    else:
        out.append(f"**Comparison type: DISTINCT MODELS.** {why}.")
    sources = sorted({s for r in rows for s in r["cost_sources"]})
    if kind != "simulated":
        out.append("Cost kinds are kept apart. *Billed* is what the provider reported in its "
                   "usage data" + (" (none was reported in this run)" if "provider" not in sources
                                   else "") + ". *Estimated* is the configured list price "
                   "(ami/pricing.py, or AMI_PRICES) of the model that served each call times its "
                   "measured tokens; those rates are marked unverified in pricing.py, and the "
                   "class proxy may bill differently or not at all. *Projected* is defined above. "
                   "*Unknown* requests (no usage, or no listed price) are excluded from averages "
                   "and counted, never treated as free.")
    unknown = sum(s["unknown_cost_requests"] for s in summaries.values())
    if unknown and kind != "simulated":
        out.append(f"{unknown} request(s) had a call with unknown cost; totals over known "
                   f"requests only, with the count shown.")
    failed = sum(s["failed_requests"] for s in summaries.values())
    if failed:
        out.append(f"{failed} request(s) failed a check (including any no model answered). A "
                   f"failed request is never a success, whatever it cost: cost per resolved "
                   f"request divides by successes only.")
    out.append("Routing accuracy, answer quality, task success, HITL recall and escalation "
               "precision are separate measurements. Quality is the share of each case's own "
               "checks that passed (tools, store, forbidden actions and claims, required facts "
               "in the reply, grounding); it is not an LLM judge's opinion (golden.py is).")
    out.append("All numbers describe this evaluation set only.")
    return out


def _kind(m, key):
    """A cost kind's average per request: full, or over the known, labelled."""
    k = m["cost_kinds"][key]
    if k["total"] is not None:
        return _money(k["known_avg"])
    if k["known_avg"] is not None:
        return f"{_money(k['known_avg'])} ({k['known']}/{k['requests']} known)"
    return "n/a"


def savings(summaries, verdict):
    """A measured saving of routing vs strong-only, or None with the reason.
    Only for distinct models, with the same cost kind known for both."""
    kind, _ = verdict
    if kind != "distinct-models":
        return None, f"not measured: comparison type is {kind}"
    r, st = summaries.get("route"), summaries.get("strong")
    if not r or not st:
        return None, "not measured: routed or strong-only run missing"
    for key, label in (("billed_cost", "billed"), ("estimated_cost", "estimated")):
        a, b = r["cost_kinds"][key]["total"], st["cost_kinds"][key]["total"]
        if a is not None and b:
            return 1 - a / b, f"{label} cost, every request known"
    return None, "not measured: no cost kind is known for every request of both strategies"


def report(rows, summaries, meta):
    """The comparison as markdown lines."""
    S = [s for s in STRATEGIES if summaries.get(s, {}).get("requests")]
    verdict = meta.get("comparison") or comparison(rows, summaries, meta.get("scripted"))
    saved, how = savings(summaries, verdict)
    L = [f"# Ami model routing: cost vs quality", ""]
    if meta.get("scope"):
        L += [f"**Scope:** {meta['scope']}", ""]
    L += [f"- Comparison type: **{verdict[0]}** — {verdict[1]}",
         f"- Mode: {meta['mode']}",
         f"- Models: cheap = `{meta['cheap']}`, strong = `{meta['strong']}`",
         f"- Planner: {meta['planner']} · runs per case: {meta['runs']} · date: {meta['date']}",
         f"- Requests per strategy: {summaries[S[0]]['requests'] if S else 0}"
         + (f" · stopped at the ${meta['max_cost']:.2f} budget before: {meta['stopped_at']}"
            if meta.get("stopped_at") else ""),
         f"- Measured saving, routed vs strong-only: "
         + (f"{100 * saved:.0f}% ({how})" if saved is not None else how),
         ""]
    if meta.get("rescored"):
        L.insert(-1, f"- Note: {meta['rescored']}")
    L += [f"> {c}" + "\n>" for c in _caveats(rows, summaries, meta, verdict)]
    L += ["", "## Strategies", "",
          "| Strategy | Billed cost / request | Est. list-price cost / request | Projected cost / "
          "request | Unknown-cost requests | Cost / resolved request | Avg latency | Quality | "
          "Task success | Strong escalation rate | Strong-call share | HITL recall | "
          "Escalation precision | Hand-off precision |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in S:
        m = summaries[s]
        h, ho = m["hitl"], m["handoff"]
        L.append(
            f"| {NAMES[s]} | {_kind(m, 'billed_cost')} | {_kind(m, 'estimated_cost')} | "
            f"{_kind(m, 'projected_cost')} | {m['unknown_cost_requests']}/{m['requests']} | "
            f"{_money(m['cost_per_success'])} | "
            f"{_num(m['avg_ms'], '{:.0f} ms')} | {_num(m['quality'])}/100 | "
            f"{_pct(m['success_rate'])} ({m['successes']}/{m['requests']}) | "
            f"{_pct(m['escalation_rate'])} ({m['escalated']}/{m['requests']}) | "
            f"{_pct(m['strong_share'])} | "
            f"{_rate(h, 'hitl_recall', (h['tp'], h['tp'] + h['fn']))} | "
            f"{_rate(h, 'escalation_precision', (h['tp'], h['tp'] + h['fp']))} | "
            f"{_rate(ho, 'escalation_precision', (ho['tp'], ho['tp'] + ho['fp']))} |")

    L += ["", "## Model identity check (requested -> served)", ""]
    for s in S:
        pairs = sorted({p for r in rows if r["strategy"] == s for p in r.get("model_pairs") or []})
        subs = summaries[s]["substituted_calls"]
        L.append(f"- {NAMES[s]}: {', '.join(pairs) or 'no served model recorded'}"
                 f" — {subs} of {summaries[s]['calls']} call(s) substituted")

    # cost vs quality, as bars. With distinct models: the billed or estimated
    # cost. Otherwise the projection (labelled), which is all that differs.
    key = None
    order = ([("billed_cost", "billed"), ("estimated_cost", "est.")]
             if verdict[0] == "distinct-models" else []) + [("projected_cost", "proj.")]
    for k, label in order:
        if all(summaries[s]["cost_kinds"][k]["known_avg"] is not None for s in S):
            key, which = k, label
            break
    top = max([summaries[s]["cost_kinds"][key]["known_avg"] or 0 for s in S] or [0]) if key else 0
    L += ["", "## Cost vs quality", "", "```"]
    for s in S:
        m = summaries[s]
        L.append(f"{NAMES[s]:<24} quality {_num(m['quality']):>5} {_bar(m['quality'], 100)}")
        if key:
            c = m["cost_kinds"][key]["known_avg"]
            L.append(f"{'':<24} {which:<7} {_money(c):>8} {_bar(c, top)}")
    if not key:
        L.append("(cost unknown for this run: no provider usage, so no cost bars)")
    elif key == "projected_cost" and verdict[0] != "distinct-models":
        L.append("(proj. = projection at requested-model list prices; NOT a measured cost)")
    L.append("```")

    L += ["", "## Safety gate: HITL recall must be 100%", ""]
    for s in S:
        h = summaries[s]["hitl"]
        ok = h["hitl_recall"] == 1.0
        status = "PASS" if ok else ("FAIL" if h["hitl_recall"] is not None else
                                    "NOT MEASURED (no case here needs approval)")
        L.append(f"- {NAMES[s]}: **{status}** — TP {h['tp']} FP {h['fp']} "
                 f"FN {h['fn']} TN {h['tn']}; recall "
                 f"{_rate(h, 'hitl_recall', (h['tp'], h['tp'] + h['fn']))}, escalation precision "
                 f"{_rate(h, 'escalation_precision', (h['tp'], h['tp'] + h['fp']))}")
    missed = any(summaries[s]["hitl"]["fn"] for s in S)
    L.append("- Overall: **" + ("PASS" if safe(summaries) else
             "FAIL — an action that needed a human was not sent to one" if missed else
             "FAIL — HITL recall not measured: run cases that need approval (the gate "
             "fails closed)") + "**")

    if "route" in S:
        m = summaries["route"]
        L += ["", "## Routing accuracy (router vs hand labels, per turn)", "",
              f"- Accuracy: {_pct(m['routing_accuracy'])} of {m['routing_turns']} turns",
              f"- Under-routed (labelled strong, routed cheap): "
              f"{', '.join(m['under_routed']) or 'none'}",
              f"- Over-routed (labelled cheap, routed strong): "
              f"{', '.join(m['over_routed']) or 'none'}",
              f"- Mid-turn escalations: {m['escalated']} of {m['requests']} requests; "
              f"fallbacks to another model: {m['fallbacks']}; no model available: {m['unavailable']}"]
        whys = [(r["case"], w) for r in rows if r["strategy"] == "route" for w in r["escalations"]]
        if whys:
            L += ["", "Why the routed strategy escalated:", ""]
            L += [f"- {case}: {why}" for case, why in whys]

    L += ["", "## Quality by dimension", "",
          "| Strategy | correct | complete | policy | grounded |", "|---|---|---|---|---|"]
    for s in S:
        d = summaries[s]["dims"]
        cell = lambda k: (f"{_pct(d[k]['rate'])} ({d[k]['passed']}/{d[k]['checks']})"
                          if k in d else "n/a")
        L.append(f"| {NAMES[s]} | {cell('correct')} | {cell('complete')} | {cell('policy')} | "
                 f"{cell('grounded')} |")

    cats = sorted({r["category"] for r in rows if r["category"]})
    L += ["", "## By category (quality · task success · avg cost)", "",
          "| Category | " + " | ".join(NAMES[s] for s in S) + " |",
          "|---|" + "---|" * len(S)]
    for cat in cats:
        cells = []
        for s in S:
            rs = [r for r in rows if r["strategy"] == s and r["category"] == cat]
            known = [r["cost"] for r in rs if r["cost"] is not None]
            avg = sum(known) / len(rs) if rs and len(known) == len(rs) else None
            cells.append(f"{_num(_mean(r['quality'] for r in rs))} · "
                         f"{sum(r['pass'] for r in rs)}/{len(rs)} · {_money(avg)}")
        L.append(f"| {cat} | " + " | ".join(cells) + " |")

    L += ["", "## Per request", "",
          "| Case | Category | Strategy | Tiers (label → used) | Escalated | Tokens in/out | "
          "Cost | Latency | Quality | Pass | Approval needed / requested | Reviewer | Request ids |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        tiers = ", ".join(f"{(lab or '?')[0]}→{used[0]}" for lab, used in
                          zip(r["route_label"], r["final_tiers"]))
        appr = ("-" if r["needs_approval"] is None else
                f"{'yes' if r['needs_approval'] else 'no'} / "
                f"{'yes' if r['approval_requested'] else 'no'}")
        toks = ("n/a" if r["tokens_in"] is None else f"{r['tokens_in']}/{r['tokens_out']}")
        L.append(f"| {r['case']} | {r['category']} | {r['strategy']} | {tiers} | "
                 f"{'yes' if r['escalated'] else 'no'} | {toks} | {_money(r['cost'])} | "
                 f"{_num(r['ms'], '{:.0f} ms')} | {_num(r['quality'])} | "
                 f"{'yes' if r['pass'] else 'NO'} | {appr} | {', '.join(r['review']) or '-'} | "
                 f"{', '.join(r['request_ids']) or '-'} |")
    fails = [r for r in rows if r["fails"]]
    if fails:
        L += ["", "## Failed checks", ""]
        L += [f"- {r['case']} ({r['strategy']}): {'; '.join(r['fails'])}" for r in fails]
    if meta.get("skipped"):
        L += ["", f"Skipped (not run, not counted): {', '.join(meta['skipped'])}"]
    return L
