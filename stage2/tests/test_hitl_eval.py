"""HITL metrics: ami/hitl_eval.py, and evals.py reporting them.

Pins:
- the four counts, and the two rates computed from them
- a zero denominator is undefined, printed as such, never as 1.00
- errored and skipped cases are named in the report, not dropped
- the eval runner, run scripted (no model), prints TP/FP/FN/TN, HITL recall
  and escalation precision together, one row per case, and its numbers
  agree with its own per-case rows
- every HITL case carries a hand-written boolean label
"""

import json

import pytest

import evals
from ami import approvals, hitl_eval


def row(case, expected, observed, error=None, escalation=None):
    return {"case": case, "expected": expected, "observed": observed,
            "requests": ["REQ-000000000001:PENDING_APPROVAL"] if observed else [],
            "escalation": escalation, "error": error}


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------

def test_confusion_counts_each_quadrant():
    c = hitl_eval.confusion([(True, True), (True, False), (False, True),
                             (False, False), (False, False)])
    assert c == {"tp": 1, "fn": 1, "fp": 1, "tn": 2}


def test_recall_and_precision_come_from_the_counts():
    m = hitl_eval.metrics({"tp": 3, "fn": 1, "fp": 2, "tn": 9})
    assert m["hitl_recall"] == 3 / 4
    assert m["escalation_precision"] == 3 / 5


def test_a_false_negative_lowers_recall_not_precision():
    m = hitl_eval.metrics(hitl_eval.confusion([(True, True), (True, False)]))
    assert m == {"hitl_recall": 0.5, "escalation_precision": 1.0}


def test_recall_is_undefined_with_no_positive_cases():
    m = hitl_eval.metrics(hitl_eval.confusion([(False, False), (False, True)]))
    assert m["hitl_recall"] is None
    assert m["escalation_precision"] == 0.0


def test_precision_is_undefined_when_nothing_was_sent_for_approval():
    m = hitl_eval.metrics(hitl_eval.confusion([(True, False), (False, False)]))
    assert m["escalation_precision"] is None
    assert m["hitl_recall"] == 0.0


def test_an_empty_set_has_no_metrics_at_all():
    assert hitl_eval.metrics(hitl_eval.confusion([])) == \
           {"hitl_recall": None, "escalation_precision": None}


def test_ratio_with_a_zero_denominator_is_none_not_one():
    assert hitl_eval.ratio(0, 0) is None
    assert hitl_eval.ratio(0, 3) == 0.0


def test_undefined_is_printed_as_undefined():
    assert hitl_eval.fmt(None, 0, 0) == "undefined (0/0)"
    assert hitl_eval.fmt(0.8, 4, 5) == "0.80 (4/5)"


def test_labels_must_be_booleans():
    with pytest.raises(TypeError):
        hitl_eval.confusion([("yes", True)])
    with pytest.raises(TypeError):
        hitl_eval.confusion([(True, None)])


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def test_an_empty_report_never_claims_a_perfect_score():
    lines, _, _ = hitl_eval.report([])
    text = "\n".join(lines)
    assert "HITL recall           undefined (0/0)" in text
    assert "Escalation precision  undefined (0/0)" in text
    assert "1.00" not in text


def test_the_report_marks_false_negatives_and_false_positives():
    lines, c, _ = hitl_eval.report([row("missed", True, False), row("extra", False, True)])
    text = "\n".join(lines)
    assert c == {"tp": 0, "fn": 1, "fp": 1, "tn": 0}
    assert any(l.startswith("missed") and " FN " in l for l in lines)
    assert any(l.startswith("extra") and " FP " in l for l in lines)
    assert "HITL recall           0.00 (0/1)" in text
    assert "Escalation precision  0.00 (0/1)" in text


def test_an_errored_case_is_counted_and_named():
    lines, c, _ = hitl_eval.report([row("crashed", True, False, error="boom"),
                                    row("fine", True, True)])
    assert c["fn"] == 1                                 # not dropped to flatter recall
    assert "ERROR: boom" in "\n".join(lines)
    assert any("errored" in l and "crashed" in l for l in lines)


def test_skipped_cases_are_named():
    lines, _, _ = hitl_eval.report([row("a", True, True)], skipped=["b"])
    assert any("skipped" in l and "b" in l for l in lines)


def test_an_esc_ticket_is_shown_apart_from_approval_requests():
    lines, c, _ = hitl_eval.report([row("ticket", False, False, escalation="ESC-4417")])
    assert c["tn"] == 1
    line = next(l for l in lines if l.startswith("ticket"))
    assert "ESC-4417" in line and "REQ-" not in line


# --------------------------------------------------------------------------
# The dataset
# --------------------------------------------------------------------------

def test_every_hitl_case_has_a_literal_boolean_label_and_a_script():
    for case in evals.HITL_CASES:
        assert type(case["needs_approval"]) is bool, case["name"]
        assert len(case["script"]) == len(case["turns"]), case["name"]


def test_the_dataset_has_both_classes_and_the_required_kinds():
    names = " ".join(c["name"] for c in evals.HITL_CASES)
    assert sum(c["needs_approval"] for c in evals.HITL_CASES) >= 3
    assert sum(not c["needs_approval"] for c in evals.HITL_CASES) >= 3
    for kind in ("over limit", "at limit", "under limit", "read-only", "ESC", "cancel", "return"):
        assert kind in names, kind


def test_the_main_table_has_no_unlabelled_approval_case():
    """Approval cases live in HITL_CASES, where every one is labelled."""
    assert not any("needs_approval" in c for c in evals.CASES)


# --------------------------------------------------------------------------
# The runner, end to end, without a model
# --------------------------------------------------------------------------

@pytest.fixture
def run_evals(monkeypatch, fresh_store, tmp_path, capsys):
    monkeypatch.setattr(approvals, "PATH", None)        # run_case sets it; put it back

    def run(*argv):
        out = tmp_path / "hitl.json"
        with pytest.raises(SystemExit):
            evals.main(["--scripted", "--out", str(out), *argv])
        return capsys.readouterr().out, json.loads(out.read_text())
    return run


def test_the_runner_reports_both_metrics_with_their_counts(run_evals):
    printed, saved = run_evals("--hitl")
    hitl = saved["hitl"]

    assert "TP " in printed and " FP " in printed and " FN " in printed and " TN " in printed
    assert "HITL recall" in printed and "Escalation precision" in printed
    assert f"TP {hitl['tp']}   FP {hitl['fp']}   FN {hitl['fn']}   TN {hitl['tn']}" in printed
    # The rates are the counts, divided — whatever the counts turn out to be.
    assert hitl["hitl_recall"] == hitl_eval.ratio(hitl["tp"], hitl["tp"] + hitl["fn"])
    assert hitl["escalation_precision"] == hitl_eval.ratio(hitl["tp"], hitl["tp"] + hitl["fp"])
    assert f"evaluated: {len(evals.HITL_CASES)} cases" in printed


def test_the_runner_reports_every_case_against_its_label(run_evals):
    printed, saved = run_evals("--hitl")
    rows = saved["hitl"]["rows"]

    assert [r["case"] for r in rows] == [c["name"] for c in evals.HITL_CASES]
    for r, case in zip(rows, evals.HITL_CASES):
        assert r["expected"] is case["needs_approval"]
        assert r["error"] is None, (r["case"], r["error"])
        assert any(l.startswith(case["name"][:52]) for l in printed.splitlines())
    counts = hitl_eval.confusion((r["expected"], r["observed"]) for r in rows)
    assert counts == {k: saved["hitl"][k] for k in ("tp", "fp", "fn", "tn")}


def test_scripted_policy_misses_no_action_that_needs_approval(run_evals):
    """A false negative here is the policy letting an over-limit refund run
    on the customer's word alone — the failure the gate exists to stop."""
    _, saved = run_evals("--hitl")
    assert saved["hitl"]["fn"] == 0, [r for r in saved["hitl"]["rows"]
                                      if r["expected"] and not r["observed"]]


def test_the_esc_case_opens_a_ticket_and_no_approval_request(run_evals):
    _, saved = run_evals("--hitl", "--only", "ESC ticket")
    (r,) = saved["hitl"]["rows"]
    assert r["escalation"] == "ESC-4417"
    assert r["observed"] is False and r["requests"] == []


def test_filtered_out_cases_are_reported_as_skipped(run_evals):
    printed, saved = run_evals("--hitl", "--only", "at limit")
    assert len(saved["hitl"]["rows"]) == 2
    assert "skipped (not run, not counted)" in printed
    assert len(saved["skipped"]) == len(evals.HITL_CASES) - 2


def test_cases_without_a_script_are_skipped_by_name_not_silently(run_evals):
    printed, saved = run_evals("--only", "escalate only once")
    assert any("no script" in s for s in saved["skipped"])
