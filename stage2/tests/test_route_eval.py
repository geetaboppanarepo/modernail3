"""Routing metrics (ami/route_eval.py) and evals.py --compare, without a model.

The metric tests feed hand-made rows with known answers. The runner tests
play the scripted provider through the real router, policy layer and
approvals store, three strategies on the same cases.
"""

import json

import pytest

import evals
from ami import approvals, policy, route_eval


def row(**kw):
    base = {"case": "c", "category": "faq", "strategy": "route", "request_ids": ["REQ-1"],
            "pass": True, "quality": 100.0, "dims": {"correct": [1, 1]}, "fails": [],
            "route_label": ["cheap"], "tiers": ["cheap"], "classified": ["cheap"],
            "final_tiers": ["cheap"], "reasons": ["r"], "escalated": False, "escalations": [],
            "fallbacks": 0, "unavailable": False, "calls": 1, "strong_calls": 0,
            "tokens_in": 100, "tokens_out": 10, "cost": 0.001, "projected_cost": 0.001,
            "cost_sources": ["configured"], "requested_models": ["m"], "served_by": ["m"],
            "ms": 100, "needs_approval": None, "approval_requested": False, "requests": [],
            "human": None, "handed_off": False, "escalation_ticket": None, "review": [],
            "error": None}
    return {**base, **kw}


# --------------------------------------------------------------------------
# The metrics
# --------------------------------------------------------------------------

def test_cost_quality_and_success_are_computed_from_the_rows():
    rows = [row(cost=0.002, quality=100.0, ms=100),
            row(cost=0.004, quality=50.0, ms=300, **{"pass": False})]
    m = route_eval.summarize(rows)
    assert m["total_cost"] == pytest.approx(0.006) and m["avg_cost"] == pytest.approx(0.003)
    assert m["cost_per_success"] == pytest.approx(0.002)     # only the resolved one
    assert (m["quality"], m["success_rate"], m["avg_ms"]) == (75.0, 0.5, 200)


def test_an_unknown_cost_is_never_counted_as_free():
    m = route_eval.summarize([row(cost=0.002), row(cost=None)])
    assert m["total_cost"] is None and m["avg_cost"] is None
    assert m["known_cost_sum"] == 0.002 and m["cost_known"] == 1
    m = route_eval.summarize([row(cost=None, projected_cost=None)])
    assert m["cost_per_success"] is None and m["projected_avg"] is None


def test_escalation_rate_and_strong_share():
    rows = [row(escalated=True, calls=2, strong_calls=1), row(calls=2, strong_calls=0),
            row(calls=1, strong_calls=1, tiers=["strong"])]
    m = route_eval.summarize(rows)
    assert m["escalation_rate"] == pytest.approx(1 / 3) and m["escalated"] == 1
    assert m["strong_share"] == pytest.approx(2 / 5)


def test_hitl_recall_and_precision_use_only_the_approval_labels():
    rows = [row(needs_approval=True, approval_requested=True),
            row(needs_approval=True, approval_requested=False),     # FN
            row(needs_approval=False, approval_requested=True),     # FP
            row(needs_approval=False, approval_requested=False),
            row(needs_approval=None, approval_requested=True)]      # unlabelled: not counted
    h = route_eval.summarize(rows)["hitl"]
    assert (h["tp"], h["fn"], h["fp"], h["tn"]) == (1, 1, 1, 1)
    assert h["hitl_recall"] == 0.5 and h["escalation_precision"] == 0.5


def test_hand_off_counts_esc_tickets_against_the_human_label():
    rows = [row(human=True, handed_off=True),
            row(human=False, handed_off=True),                      # an unneeded ticket
            row(human=False, needs_approval=True, approval_requested=True)]
    h = route_eval.summarize(rows)["handoff"]
    assert (h["tp"], h["fp"]) == (2, 1)
    assert h["escalation_precision"] == pytest.approx(2 / 3)


def test_routing_accuracy_names_the_under_and_over_routed_cases():
    rows = [row(case="a", route_label=["strong"], classified=["cheap"]),
            row(case="b", route_label=["cheap"], classified=["strong"]),
            row(case="c", route_label=["cheap", "strong"], classified=["cheap", "strong"])]
    m = route_eval.summarize(rows)
    assert m["routing_accuracy"] == pytest.approx(2 / 4) and m["routing_turns"] == 4
    assert m["under_routed"] == ["a"] and m["over_routed"] == ["b"]


def test_nothing_measured_is_reported_as_undefined_not_perfect():
    m = route_eval.summarize([])
    assert m["hitl"]["hitl_recall"] is None and m["quality"] is None
    assert m["routing_accuracy"] is None and m["success_rate"] is None


def test_the_safety_gate_fails_on_one_missed_approval_in_any_strategy():
    ok = route_eval.summarize([row(needs_approval=True, approval_requested=True)])
    miss = route_eval.summarize([row(needs_approval=True, approval_requested=False)])
    none = route_eval.summarize([row()])
    assert route_eval.safe({"route": ok, "cheap": ok})
    assert not route_eval.safe({"route": ok, "cheap": miss})
    assert not route_eval.safe({"route": none})             # undefined recall is not a pass


META = {"mode": "LIVE", "cheap": "c-model", "strong": "s-model", "planner": "react",
        "runs": 1, "date": "today", "skipped": [], "stopped_at": None, "max_cost": 5}


def test_the_report_flags_a_provider_that_served_one_model_for_every_name():
    rows = [row(strategy=s, requested_models=["c-model", "s-model"], served_by=["one-model"])
            for s in route_eval.STRATEGIES]
    sums = {s: route_eval.summarize([r for r in rows if r["strategy"] == s])
            for s in route_eval.STRATEGIES}
    text = "\n".join(route_eval.report(rows, sums, META))
    assert "served one model for every request" in text and "one-model" in text
    assert "Projected cost" in text and "not a bill" in text
    for s in route_eval.STRATEGIES:
        assert route_eval.NAMES[s] in text


def test_the_report_marks_unknown_costs_and_the_safety_verdict():
    rows = [row(strategy=s, cost=None, projected_cost=None, needs_approval=True,
                approval_requested=True) for s in route_eval.STRATEGIES]
    sums = {s: route_eval.summarize([r for r in rows if r["strategy"] == s])
            for s in route_eval.STRATEGIES}
    text = "\n".join(route_eval.report(rows, sums, {**META, "scripted": True}))
    assert "Comparison type: SIMULATED" in text and "unknown (n/a)" in text
    assert "no cost bars" in text and "Overall: **PASS**" in text


# --------------------------------------------------------------------------
# evals.py --compare, scripted, end to end
# --------------------------------------------------------------------------

@pytest.fixture
def compare(monkeypatch, fresh_store, tmp_path, capsys):
    monkeypatch.setattr(approvals, "PATH", None)
    monkeypatch.delenv("AMI_ROUTING", raising=False)

    def run(*argv):
        out, md = tmp_path / "cmp.json", tmp_path / "cmp.md"
        with pytest.raises(SystemExit) as e:
            evals.main(["--compare", "--scripted", "--out", str(out), "--report", str(md), *argv])
        return e.value.code, json.loads(out.read_text()), md.read_text(), capsys.readouterr().out
    return run


def test_every_strategy_runs_the_same_cases(compare):
    code, saved, md, _ = compare("--only", "reviewer")
    names = {s: [r["case"] for r in saved["rows"] if r["strategy"] == s]
             for s in route_eval.STRATEGIES}
    assert names["cheap"] == names["strong"] == names["route"] and len(names["route"]) == 4
    assert code == 0 and "Overall: **PASS**" in md


def test_the_fixed_strategies_use_only_their_tier(compare):
    _, saved, _, _ = compare("--only", "faq")
    for r in saved["rows"]:
        if r["strategy"] != "route":
            assert set(r["final_tiers"]) == {r["strategy"]}, r
    routed = [r for r in saved["rows"] if r["strategy"] == "route"]
    assert routed and all(r["final_tiers"] == ["cheap"] for r in routed)


def test_scripted_costs_are_unknown_never_zero(compare):
    _, saved, md, _ = compare("--only", "faq")
    assert all(r["cost"] is None and r["tokens_in"] is None for r in saved["rows"])
    assert saved["summaries"]["route"]["total_cost"] is None
    assert "$0.0000" not in md


def test_reviewer_decisions_are_recorded_and_checked(compare):
    _, saved, _, _ = compare("--only", "reviewer")
    by_case = {r["case"]: r for r in saved["rows"] if r["strategy"] == "route"}
    assert by_case["reviewer approves: $348 return runs once"]["review"] == ["approved"]
    assert by_case["reviewer rejects: $249 cancellation never runs"]["review"] == ["rejected"]
    assert by_case["reviewer requests changes: $200 return never runs"]["review"] == \
        ["changes_requested"]
    assert by_case["reviewer never answers: $200 cancellation expires"]["review"] == ["expired"]
    assert all(r["pass"] for r in by_case.values()), [r["fails"] for r in by_case.values()]


def test_the_full_scripted_comparison_is_safe_and_routes_every_turn_as_labelled(compare):
    code, saved, md, _ = compare()
    s = saved["summaries"]
    assert code == 0
    for name in route_eval.STRATEGIES:
        assert s[name]["hitl"]["hitl_recall"] == 1.0 and s[name]["hitl"]["fn"] == 0
        assert s[name]["success_rate"] == 1.0, [r["fails"] for r in saved["rows"] if r["fails"]]
    assert s["route"]["routing_accuracy"] == 1.0, (s["route"]["under_routed"],
                                                   s["route"]["over_routed"])
    assert 0 < s["route"]["strong_share"] < 1                # it does use both tiers


def test_a_policy_regression_fails_the_safety_gate(compare, monkeypatch):
    """Raise the approval limit so nothing is sent to a reviewer: every
    strategy misses, and the comparison must fail, not just report."""
    monkeypatch.setattr(policy, "APPROVAL_LIMIT", 10_000.0)
    code, saved, md, _ = compare("--only", "reviewer approves")
    assert code == 1 and "FAIL" in md
    assert saved["summaries"]["route"]["hitl"]["fn"] == 1


def test_every_labelled_case_has_category_route_and_human_labels():
    cats = {"simple", "faq", "ambiguous", "complex", "missing_info", "policy_exception",
            "high_risk", "escalate_human", "no_escalation"}
    for case in evals.ALL_CASES:
        assert case["category"] in cats, case["name"]
        route = case["route"]
        assert route in ("cheap", "strong") or (
            len(route) == len(case["turns"]) and set(route) <= {"cheap", "strong"}), case["name"]
        assert type(case["human"]) is bool, case["name"]
    for case in evals.ROUTING_CASES:
        assert type(case["needs_approval"]) is bool and len(case["script"]) == len(case["turns"])
    assert {c["category"] for c in evals.ALL_CASES} == cats  # every kind is represented


def _result(**kw):
    base = {"error": None, "reply": "", "called": [], "executed": [], "refused": [],
            "transcript": "", "store": {}, "approval_requested": False, "turns": [],
            "unverified": []}
    return {**base, **kw}


def test_reply_checks_ignore_typographic_quotes_and_dashes():
    case = {"reply_has_any": ["3-5", "hasn't shipped"], "reply_lacks": ["can't dispute"]}
    ok = _result(reply="Refunds take 3–5 business days; it hasn’t shipped.")
    assert evals.score(case, ok) == []
    bad = _result(reply="You can’t dispute that.")
    assert evals.score(case, bad) == [f"reply has none of {case['reply_has_any']}",
                                      "reply contains 'can't dispute'"]


def test_a_request_no_model_answered_is_a_failure_not_a_pass():
    r = _result(reply="I'm sorry, I can't reach our support systems right now.",
                turns=[{"unavailable": True}])
    assert "no model answered (every model failed)" in evals.score({}, r)
    assert evals.score({}, _result(reply="Fine.", turns=[{"unavailable": False}])) == []


def test_a_scripted_comparison_never_overwrites_the_live_report(monkeypatch):
    """Defaults only: `evals.py --compare --scripted` writes the *_scripted files."""
    from ami import llm
    monkeypatch.setattr(llm, "_client", llm._client)   # a non-scripted main() swaps the client
    seen = {}
    monkeypatch.setattr(evals, "compare", lambda a, cases, skipped: seen.update(
        out=a.out, report=a.report) or (_ for _ in ()).throw(SystemExit(0)))
    for argv, out, report in (
            (["--compare", "--scripted"], "results/routing_compare_scripted.json",
             "results/routing_report_scripted.md"),
            (["--compare"], "results/routing_compare.json", "results/routing_report.md")):
        with pytest.raises(SystemExit):
            evals.main(argv)
        assert (seen["out"], seen["report"]) == (out, report)
