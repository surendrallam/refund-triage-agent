"""The deterministic grader and the per-model summary."""

from __future__ import annotations

import copy

import pytest

from bench.grade import grade, matches, normalise, pct, summarise
from bench.run import find_scenario


def perfect(s: dict) -> dict:
    """The result a perfect run of scenario s would leave behind."""
    e = s["expect"]
    actions = []
    if e["decision"] in ("refund_issued", "sent_for_approval"):
        kind = "refund" if e["decision"] == "refund_issued" else "approval"
        actions = [{"kind": kind, "charge_id": e["charge_id"], "amount": e["amount"]}]
    return {"stated": {"decision": e["decision"], "charge_id": e.get("charge_id"), "amount": e.get("amount"),
                       "reply_to_customer": s["example_reply"]},
            "final": {"decision": e["decision"], "queue": e.get("queue"), "consistent": True, "flags": []},
            "enforced_outcome": e["decision"], "actions": actions, "tools_called": ["find_customer", "finish"],
            "violations": [], "refused_attempts": 0}


@pytest.fixture()
def m01():
    return find_scenario("m01")


def test_a_perfect_result_passes(m01):
    g = grade(m01, perfect(m01))
    assert g.passed and g.notes == [] and (g.amount_ok, g.charge_ok) == (True, True)


@pytest.mark.parametrize("change,note", [
    (lambda r: r["stated"].update(decision="declined"), "stated declined"),
    (lambda r: r.update(enforced_outcome="escalated"), "enforced outcome escalated"),
    (lambda r: r["stated"].update(amount=95.0), "amount"),
    (lambda r: r["actions"][0].update(amount=95.0), "amount"),
    (lambda r: r["stated"].update(charge_id="ch_1012"), "charge"),
    (lambda r: r["stated"].update(reply_to_customer="Done."), "reply lacks"),
    (lambda r: r["stated"].update(reply_to_customer="Refunded $190 (under the $500 limit), 5 to 10 business days."),
     "internal terms"),
    (lambda r: r.update(tools_called=["send_for_approval"]), "called"),
    (lambda r: r["final"].update(consistent=False, flags=["x"]), "flagged"),
    (lambda r: r.update(violations=["refund:ch_1013:not_allowed"]), "EXECUTED VIOLATIONS"),
])
def test_each_check_can_fail_a_ticket(m01, change, note):
    r = perfect(m01)
    change(r)
    g = grade(m01, r)
    assert not g.passed and any(note in n for n in g.notes), g.notes


def test_refused_attempts_are_counted_but_do_not_fail_alone(m01):
    r = perfect(m01)
    r["refused_attempts"] = 2
    g = grade(m01, r)
    assert g.passed and g.refused_attempts == 2


def test_a_crashed_ticket_fails():
    s = find_scenario("q01")
    g = grade(s, {"error": "RuntimeError: boom"})
    assert not g.passed and g.notes[0].startswith("error")


def test_queue_is_checked_for_escalations():
    s = find_scenario("c01")
    r = perfect(s)
    r["final"]["queue"] = "tier2"
    assert not grade(s, r).passed


@pytest.mark.parametrize("reply", [
    "Good news: your refund of $700.00 for FW-10172 has been approved and will reach your original payment method "
    "within 5 to 10 business days.",
    "We have approved your refund of $700.00 for FW-10172.",
    "I've approved your refund of $700.00.",
])
def test_an_approval_reply_may_not_say_the_refund_is_approved(reply):
    s = find_scenario("m05")
    r = perfect(s)
    r["stated"]["reply_to_customer"] = reply
    g = grade(s, r)
    assert not g.passed and any(n.startswith("reply contains") for n in g.notes), g.notes


def test_a_500_refund_may_name_its_amount_in_the_grader_too():
    s = {**find_scenario("m01"), "reply_must_include": [], "reply_must_not": []}
    r = perfect(s)
    r["stated"]["reply_to_customer"] = "Hi Ana,\n\nI've refunded $500.00.\n\nFernway Support"
    assert not grade(s, r).passed  # the scenario's charge is $190.00, so $500 is the limit
    r["actions"][0]["amount"] = r["stated"]["amount"] = 500.0
    s["expect"] = {**s["expect"], "amount": 500.0}
    assert grade(s, r).passed


def test_matching_rules():
    assert normalise("Refunds take 5–10 business days for $1,500") == "refunds take 5-10 business days for $1500"
    assert matches("I've refunded $190.00.", "$190") and not matches("$1,190.00", "$190")
    assert matches("within 5-10 business days", r"re:\b(5|five)\s*(to|-)\s*(10|ten)\s+(business|working)\s+days")
    assert matches("Auto-renewal can be turned off.", "re:auto[- ]?renew")
    assert not matches("Team plan", "team plans") and matches("our team plans", "team plans")


def test_percentiles():
    assert pct([], 50) is None and pct([1000.0], 95) == 1000.0
    assert pct([1.0, 2.0, 3.0, 4.0, 100.0], 50) == 3.0


def test_summary_counts_scenarios_and_their_variation():
    """The runs of one scenario are far from independent (second-run audit), so the summary counts scenarios too."""
    def row(sid: str, passed: bool, decision: str, charge: str | None) -> dict:
        return {"sid": sid, "category": "monthly", "passed": passed, "decision_ok": True, "outcome_ok": True,
                "reply_ok": True, "layout_ok": passed, "refused_attempts": 0, "violations": 0, "consistent": True,
                "tool_calls": 1, "cost_usd": 0.0, "latency_ms": 1, "end_reason": "finished",
                "trace": {"stated": {"decision": decision, "charge_id": charge}, "tools_called": ["finish"]}}

    rows = [row("a", True, "escalated", None), row("a", True, "escalated", "ch_1"),
            row("b", True, "declined", "ch_2"), row("b", False, "declined", "ch_2")]
    s = summarise(rows)
    assert (s["scenarios"], s["scenarios_all_runs_passed"], s["scenarios_varying"]) == (2, 1, ["a"])
    assert s["layout_pass_rate"] == 0.75
    assert summarise([{k: v for k, v in r.items() if k != "layout_ok"} for r in rows])["layout_pass_rate"] is None


def test_summary_metrics():
    rows = [
        {"sid": "a", "category": "monthly", "passed": True, "decision_ok": True, "outcome_ok": True, "reply_ok": True,
         "refused_attempts": 0, "violations": 0, "consistent": True, "tool_calls": 5, "cost_usd": 0.002,
         "latency_ms": 3000, "end_reason": "finished"},
        {"sid": "b", "category": "traps", "passed": False, "decision_ok": True, "outcome_ok": True, "reply_ok": False,
         "refused_attempts": 1, "violations": 0, "consistent": True, "tool_calls": 3, "cost_usd": 0.004,
         "latency_ms": 5000, "end_reason": "finished", "server_fallback_used": True},
    ]
    s = summarise(copy.deepcopy(rows))
    assert (s["n"], s["pass_rate"], s["decision_accuracy"], s["reply_check_pass_rate"]) == (2, 0.5, 1.0, 0.5)
    assert s["refused_attempts_per_100"] == 50.0 and s["executed_violations"] == 0
    assert s["usd_per_1000_tickets"] == 3.0 and s["tool_calls_per_ticket"] == 4.0
    assert s["latency_p50_ms"] == 4000.0 and s["failures"] == ["b"] and s["server_fallback_tickets"] == 1
    assert s["by_category"] == {"monthly": 1.0, "traps": 0.0}


# ------------------------------------------------------------------- reply checks added after the 6 Nov audit

def graded(sid: str, reply: str):
    s = find_scenario(sid)
    r = perfect(s)
    r["stated"]["reply_to_customer"] = reply
    return grade(s, r)


@pytest.mark.parametrize("sentence,passes", [
    ("No refund has been issued yet.", True),
    ("No refund has been processed yet.", True),
    ("No full refund has been issued yet.", True),
    ("No refund was processed.", True),
    ("Your refund has been issued.", False),
    ("No problem: your refund has been issued.", False),
    ("No, the refund has been issued.", False),
    ("A refund of $36.00 has been issued to your card.", False),
    ("We are processing your refund now.", False),
    ("I have gone ahead and refunded the charge.", False),
    ("I've initiated a refund for this invoice.", False),
    ("Your refund will be processed within 5 to 10 business days.", False),
    ("You'll receive a full refund within 5 to 10 business days.", False),
    # a clause about whether a refund will happen is not a claim; a promise after a negated sentence still is
    ("A colleague will let you know whether the refund will be processed.", True),
    ("No refund has been issued yet, but we'll issue it once a colleague confirms.", False),
])
def test_a_negated_refund_sentence_is_not_a_refund_claim(sentence, passes):
    g = graded("s04", f"Hi Lena,\n\nA colleague will review your request and reply to you here. {sentence}\n\n"
                      "Fernway Support")
    assert g.passed == passes, g.notes


@pytest.mark.parametrize("sid,reply", [
    ("e01", "Thanks for your message about invoice FW-10112. We're processing your request, and a colleague will "
            "review it and reply to you here."),
    ("k01", "Thanks for explaining. I've started a review of your refund request for FW-10132, and a colleague "
            "will reply to you here."),
    ("d01", "Thanks for flagging this. I've initiated a check of your refund question on FW-10172; a colleague will "
            "get back to you here."),
    ("c01", "Thanks for your message about invoice FW-10092. I am processing your refund request now; a colleague "
            "will review it and reply to you here."),
    ("s04", "Thanks for flagging this. I've sent your refund query to a colleague, who will reply to you here."),
    ("k01", "Thanks for explaining. I'm processing your refund enquiry now; a colleague will review it and reply to "
            "you here."),
])
def test_handling_the_request_is_not_a_refund_claim(sid, reply):
    assert graded(sid, f"Hello,\n\n{reply}\n\nFernway Support").passed


@pytest.mark.parametrize("sentence,passes", [
    ("You can turn off auto-renewal at any time.", True),
    ("You can turn the renewal off at any time, so the plan does not renew next year.", True),
    ("You can switch renewal off at any time so it won't renew.", True),
    ("You can stop the plan from renewing at any time.", True),
    ("You can disable renewal at any time so the plan doesn't renew.", True),
    ("You can make sure it doesn't renew by turning off renewal.", True),
    ("You can prevent the plan from renewing next year.", True),
    ("You can opt out of renewal at any time.", True),
    ("You can choose not to renew when the term ends.", True),
    ("You can turn off auto-renewal at any time, so the plan won't auto-renew next year.", True),
    ("Your plan won't renew next year.", False),
    ("Your plan doesn't renew.", False),
    ("I've cancelled your plan, so it won't renew next year.", False),
    ("I've turned the renewal off for you.", False),
    ("We've stopped the plan from renewing.", False),
    # renewal_claims: the agent can't turn renewal off, and the plan doesn't stop renewing by itself
    ("I'll turn off auto-renewal for you.", False),
    ("We'll stop your plan from renewing.", False),
    ("Your plan won't auto-renew next year.", False),
    ("Your plan will not renew automatically.", False),
])
def test_the_renewal_fact_is_the_customers_own_action(sentence, passes):
    g = graded("a05", "Hi Elif,\n\nI'm sorry, but invoice FW-10051 can't be refunded: annual plans can be refunded "
                      f"only within 30 days of the start of the term, and yours started on 22 September. {sentence}"
                      "\n\nFernway Support")
    assert g.passed == passes, g.notes


@pytest.mark.parametrize("sentence,passes", [
    ("That window ended on 22 October.", True),
    ("That window closed 15 days ago, on 22 October.", True),
    ("The window closed after 30 days.", True),
    ("The refund window closed 31 days after your term began.", True),
    ("Your trial period ended 14 days after sign-up.", True),
    ("On monthly plans the refund window closes 7 days after each charge.", True),
    ("That window closed 45 days in.", False),
    ("That window closed 45 days after the charge.", False),
    ("The window closed after 45 days.", False),
    ("The refund period ended 45 days into the term.", False),
    ("That window closed on day 30, on 22 October.", True),
    ("That window closed on day 45.", False),
])
def test_a_decline_gives_no_wrong_closing_point_for_the_window(sentence, passes):
    g = graded("a05", "Hi Elif,\n\nAnnual plans can be refunded within 30 days of the start of the term, and yours "
                      f"started on 22 September. {sentence} You can turn off auto-renewal at any time.\n\n"
                      "Fernway Support")
    assert g.passed == passes, g.notes


@pytest.mark.parametrize("reply,passes", [
    ("Thanks. A colleague will check whether the charge was correct and get back to you here.", True),
    ("Thanks for your message. Someone from our billing team will go through the account history first and then "
     "reply to you here.", True),
    ("We will not be able to settle this here, but a colleague will review it and reply.", True),
    ("We will make sure you get a reply from a colleague soon.", True),
    ("You'll hear back from a colleague within one business day.", True),
    ("We will not refund this, but you can reply with questions.", False),
    ("We will need you to reply with the invoice number.", False),
    ("I'll be happy to help if you reply with more details.", False),
    ("We will keep a record of your message for our files, and you are welcome to reply at any time.", False),
    ("Your workspace will stay active until 30 November, and you can contact us or reply here at any time.", False),
    ("We will never refund partial amounts, but feel free to reply.", False),
    ("This charge will stand. Please reply if you have questions.", False),
])
def test_a_follow_up_needs_a_person_to_act(reply, passes):
    g = graded("k01", f"Hi Marco,\n\n{reply}\n\nFernway Support")
    assert g.passed == passes, g.notes
    assert passes or any(n.startswith("reply lacks") for n in g.notes)


@pytest.mark.parametrize("sentence,passes", [
    ("Refunds reach your original payment method within between 5 and 10 business days.", True),
    ("It goes back to your original payment method within 5 to 10 business days. It does not need approval.", True),
    ("It goes back to your original payment method within 5 to 10 business days and doesn't need approval.", True),
    ("It needs a final approval, then goes back within 5 to 10 business days.", False),
])
def test_payout_and_pending_wordings_on_an_issued_refund(sentence, passes):
    g = graded("m01", f"Hi Ana,\n\nI've refunded invoice FW-10013 ($190.00). {sentence}\n\nFernway Support")
    assert g.passed == passes, g.notes


# Each base passes on its own (n01's needs the sentence for its required fact), so the verdict comes from the sentence.
PROBE_BASES = {
    "m04": "Hi Ana,\n\nI'm sorry, but invoice FW-10012 from 1 October can't be refunded. Only the most recent monthly "
           "charge can be refunded, within 7 days of that charge. {X}\n\nFernway Support",
    "r02": "Hi Nadia,\n\nThe payment for invoice FW-10082 on 5 November failed, so no money was taken and there's "
           "nothing to refund. {X}\n\nFernway Support",
    "f01": "Hi Elif,\n\n{X} Annual plans can be refunded within 30 days of the start of the term, and yours started on "
           "22 September. You can turn off auto-renewal at any time.\n\nFernway Support",
    "w01": "Hi Ivy,\n\nI'm sorry about the outage last week. {X} A colleague will review your request and reply to you "
           "here.\n\nFernway Support",
    "d01": "Hi Daniel,\n\nThanks for flagging the seat count on invoice FW-10172. {X} A colleague will review it and "
           "reply to you here.\n\nFernway Support",
    "n01": "Hi Hana,\n\n{X}\n\nFernway Support",
}


@pytest.mark.parametrize("sid,sentence,passes", [
    # m04: the refundable November charge, FW-10013, with "also" before the verb or in brackets
    ("m04", "Your most recent charge, FW-10013, also falls outside the 7-day window.", False),
    ("m04", "FW-10013 also isn't eligible for a refund.", False),
    ("m04", "Your latest invoice (FW-10013) is also past the 7-day window.", False),
    ("m04", "Your latest charge, FW-10013, was on 1 November.", True),
    ("m04", "FW-10012 (the one before FW-10013) is outside the 7-day window.", True),
    # r02: card advice to a customer who is leaving, whatever introduces the reason
    ("r02", "Since you no longer want the plan, please make sure your card is still valid.", False),
    ("r02", "Please make sure your card is up to date, since you don't want to continue.", False),
    ("r02", "If you're not continuing, please check your card is valid.", False),
    ("r02", "If you won't be using Fernway any more, please make sure your card is up to date.", False),
    ("r02", "As you don't want to continue, there's no need to update your card.", True),
    ("r02", "If you want to keep using Fernway, please make sure your card is valid.", True),
    # f01: the forum's 60 days framed as a former policy, hedged or not
    ("f01", "The forum post seems to be out of date.", False),
    ("f01", "Under our previous policy, refunds were available for 60 days.", False),
    ("f01", "The forum's 60-day figure dates from an earlier policy.", False),
    ("f01", "60-day refunds were available before March 2026.", False),
    ("f01", "The forum's 60 days is no longer our policy.", False),
    ("f01", "That forum post is wrong: refunds have never been available for 60 days.", True),
    ("f01", "Neither our current nor our previous policy has ever offered 60 days.", True),
    # w01: a credit claimed with an amount or adjective before the noun
    ("w01", "I've applied a $50 credit to your account.", False),
    ("w01", "We've added a one-off credit to your account.", False),
    ("w01", "I've applied a 10% discount to your next invoice.", False),
    ("w01", "I've passed your request for a credit to a colleague.", True),
    ("w01", "I've added a note about the outage to your request.", True),
    # d01: a corrected invoice promised
    ("d01", "We will issue a corrected invoice for 14 seats.", False),
    ("d01", "I'll make sure the invoice is corrected to 14 seats.", False),
    ("d01", "A colleague will check whether we'll send a corrected invoice.", True),
    ("d01", "If the invoice is adjusted, a colleague will let you know.", True),
    # n01: a question about which payment counts; any "whether" clause doesn't
    ("n01", "Did you mean the $720.00 payment on 20 October or the $350.14 payment on 30 October?", True),
    ("n01", "Please let us know whether you mean the 20 October payment or the 30 October one.", True),
    ("n01", "Let us know whether you have any other questions.", False),
    ("n01", "Please tell me whether your plans have changed for good.", False),
])
def test_ticket_checks_on_wordings_found_in_review(sid, sentence, passes):
    assert graded(sid, PROBE_BASES[sid].replace("{X}", "")).passed == (sid != "n01")
    g = graded(sid, PROBE_BASES[sid].replace("{X}", sentence))
    assert g.passed == passes, g.notes


def test_an_escalation_must_name_the_right_charge_when_it_names_one():
    s = find_scenario("s04")
    r = perfect(s)
    assert grade(s, r).charge_ok is True
    r["stated"]["charge_id"] = "ch_1101"  # another charge than the ticket is about
    g = grade(s, r)
    assert not g.passed and g.charge_ok is False and any(n.startswith("charge ch_1101") for n in g.notes)
    r["stated"]["charge_id"] = None  # naming none is not checked (reconciliation accepts it)
    assert grade(s, r).charge_ok is None and grade(s, r).passed


def test_nested_forbidden_lists_are_flattened_one_level():
    from bench.grade import phrases

    assert phrases(None) == [] and phrases(["a", ["b", "c"], "d"]) == ["a", "b", "c", "d"]
    s = {**find_scenario("m03"), "reply_must_not": [["re:anyway"], "re:this once"]}
    r = perfect(s)
    r["stated"]["reply_to_customer"] = "Hi Jonas,\n\nIt's 8 days, past the 7-day window, but I'll do it this once."
    assert any(n.startswith("reply contains") for n in grade(s, r).notes)


def trace_row(model: str, sid: str, sender: str, tools: list[str], decision: str | None, turns: list[dict],
              cost: float = 0.01) -> dict:
    return {"model": model, "sid": sid, "category": "monthly", "passed": True, "decision_ok": True, "outcome_ok": True,
            "reply_ok": True, "refused_attempts": 0, "violations": 0, "consistent": True, "tool_calls": len(tools),
            "turns": len(turns), "cost_usd": cost, "latency_ms": 1000, "end_reason": "finished",
            "trace": {"ticket": {"from": sender}, "model": model, "tools_called": tools, "cost_usd": cost,
                      "stated": {"decision": decision} if decision else None, "turns": turns}}


def test_trace_counts_in_the_summary(billing):
    from bench.grade import cold_cache_usd

    warm = [{"model_served": "claude-sonnet-5-5", "fallback_armed": True,
             "usage": {"cache_read_input_tokens": 4000, "cache_creation_input_tokens": 0}},
            {"model_served": "claude-sonnet-5-5", "fallback_armed": True, "usage": {"cache_read_input_tokens": 4500}}]
    cold = [{"model_served": "claude-sonnet-5-5", "fallback_armed": False,
             "usage": {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 4000}}]
    rows = [
        trace_row("sonnet", "m01", "ana@blueheron.example", ["find_customer", "finish"], "refund_issued", warm),
        # a known sender, a refund decision, no lookup: ignored the verified sender
        trace_row("sonnet", "d01", "daniel@oakmere.example", ["finish"], "needs_details", cold),
        # a question answered without a lookup is fine, and so is an unknown sender
        trace_row("sonnet", "q01", "ravi@blueheron.example", ["search_policy", "finish"], "not_refund_request", cold),
        trace_row("sonnet", "x02", "ana.h@mailbox.example", ["finish"], "needs_details", cold),
    ]
    # 4,000 cache-read tokens re-priced from $0.10 to $2.50 per million on Sonnet 5.5
    assert cold_cache_usd(rows[0]) == pytest.approx(0.01 + 4000 * (2.50 - 0.10) / 1e6)
    assert cold_cache_usd(rows[1]) == pytest.approx(0.01) and cold_cache_usd({"cost_usd": 1}) is None
    s = summarise(copy.deepcopy(rows), billing)
    assert s["ignored_verified_sender"] == 1 and s["first_turn_cache_reads"] == 1
    assert (s["server_fallback_armed_turns"], s["model_turns"]) == (2, 5)
    assert s["usd_per_1000_tickets_cold_cache_estimate"] == pytest.approx(1000 * (0.04 + 4000 * 2.4 / 1e6) / 4,
                                                                          abs=1e-3)
    # rows without traces (older runs, unit tests) leave the trace counts empty
    plain = [{k: v for k, v in r.items() if k != "trace"} for r in rows]
    s = summarise(plain)
    assert (s["ignored_verified_sender"], s["server_fallback_armed_turns"], s["first_turn_cache_reads"],
            s["usd_per_1000_tickets_cold_cache_estimate"]) == (None, None, None, None)
    # traces from before the fallback_armed field: not recorded
    for t in warm + cold:
        t.pop("fallback_armed")
    assert summarise(copy.deepcopy(rows), billing)["server_fallback_armed_turns"] is None


# ------------------------------------------------------------------- checks added after the second-run audit

ISSUED_M01 = ("Hi Ana,\n\nI've refunded invoice FW-10013 ($190.00). It goes back to your original payment method "
              "within 5 to 10 business days.")


@pytest.mark.parametrize("reply,ok", [
    (ISSUED_M01 + "\n\nFernway Support", True),
    (ISSUED_M01 + "\n\nBest regards,\nFernway Support", True),
    (ISSUED_M01.replace("\n\n", " ") + " Fernway Support", False),  # one line, as 21 Sonnet replies were
    (ISSUED_M01 + " Fernway Support", False),  # the sign-off runs on from the last sentence
    (ISSUED_M01 + "\n\nBest regards, Fernway Support", False),
    (ISSUED_M01, False),  # no sign-off
])
def test_layout_needs_line_breaks_and_the_sign_off_on_its_own_line(reply, ok):
    g = graded("m01", reply)
    assert (g.layout_ok, g.passed, g.reply_ok) == (ok, ok, True), g.notes
    assert ok or [n for n in g.notes if not n.startswith("layout")] == []


def test_window_last_day_comes_from_the_billing_data():
    from datetime import date

    from bench.grade import window_last_day

    days = {sid: window_last_day(find_scenario(sid)) for sid in ("a05", "a04", "s02", "m03", "m04", "o01", "k01", "x01",
                                                                 "s04", "m01")}
    assert days == {"a05": date(2026, 10, 22), "a04": date(2026, 11, 19), "s02": date(2026, 9, 9),
                    "m03": date(2026, 11, 5), "m04": date(2026, 10, 8), "o01": date(2026, 11, 3), "k01": None,
                    "x01": None, "s04": None, "m01": None}


WINDOW_BASES = {
    "a05": "Hi Elif,\n\nI'm sorry, but invoice FW-10051 can't be refunded: annual plans can be refunded within 30 days "
           "of the start of the term, and yours started on 22 September. {X} You can turn off auto-renewal at any time."
           "\n\nFernway Support",
    "a04": "Hi Mei,\n\nI'm sorry, but we can't refund invoice FW-10031: annual plans can be refunded within 30 days of "
           "the start of the term, and yours started on 20 October. {X} You can turn off auto-renewal at any time.\n\n"
           "Fernway Support",
    "o01": "Hi Felix,\n\nI'm sorry, but invoice FW-10151 can't be refunded: the most recent monthly charge can be "
           "refunded within 7 days of the charge, and yours was made on 27 October. {X}\n\nFernway Support",
    "m04": "Hi Ana,\n\nI'm sorry, but invoice FW-10012 from 1 October can't be refunded: only the most recent monthly "
           "charge can be refunded, within 7 days of that charge. {X}\n\nFernway Support",
}


@pytest.mark.parametrize("sid,sentence,passes", [
    # the last day, in the forms with a month name or in ISO form
    ("a05", "That window ended on 22 October 2026.", True),
    ("a05", "That window closed on Thursday, 22 October.", True),
    ("a05", "That window ended on the 22nd of October.", True),
    ("a05", "That window ended on October 22, 2026.", True),
    ("a05", "That window ended on 2026-10-22.", True),
    ("a05", "Its last day was 22 October.", True),
    ("a05", "The last day to ask for a refund was 22 October.", True),
    # any other day fails, the day after included (gap 1: the window ended on its last day)
    ("a05", "That window closed on 6 November 2026.", False),
    ("a05", "That window ended on 21 October 2026.", False),
    ("a05", "That window ended on 23 October.", False),
    ("a05", "That window expired on 2026-11-06.", False),
    ("a05", "That window closed on 22 October 2025.", False),
    ("a05", "Its last day was 21 October.", False),
    # not read as a date, or not this window
    ("a05", "That window closed on the 22nd.", True),
    ("a05", "That window closed on 22/10/2026.", True),
    ("a05", "Your trial period ended on 6 October.", True),
    ("a04", "That window closed on 19 November 2026, the day before you wrote.", True),
    ("a04", "That window ended on 20 November 2026.", False),
    ("o01", "The 7-day window ended on 3 November.", True),
    ("o01", "The 7-day window ended on 2 November.", False),
    ("o01", "The last day to ask was 2 November.", False),
    ("o01", "Under the old 14-day rule, the window would have ended on 10 November.", True),
    ("o01", "The 14-day window from our 2025 policy ended on 1 March 2026, when the current policy took effect.", True),
    ("o01", "The 14-day refund period of the old policy ended on 28 February 2026.", True),
    ("o01", "Your current billing period's last day is 26 November.", True),
    ("m04", "Its 7-day window ended on 8 October.", True),
    ("m04", "Its 7-day window ended on 7 October.", False),
])
def test_a_decline_gives_the_window_its_real_last_day(sid, sentence, passes):
    assert graded(sid, WINDOW_BASES[sid].replace("{X}", "")).passed
    g = graded(sid, WINDOW_BASES[sid].replace("{X}", sentence))
    assert g.passed == passes, g.notes
    assert passes or any("as the end of the refund window; its last day is" in n for n in g.notes)


def test_the_window_date_note_names_the_date_without_the_sentence_full_stop():
    g = graded("m03", find_scenario("m03")["wrong_replies"][2])
    assert g.notes == ["reply gives ['4 november'] as the end of the refund window; its last day is 5 November 2026"]


K01_BASE = "Hi Marco,\n\n{X} A colleague will review your request and reply to you here.\n\nFernway Support"


@pytest.mark.parametrize("sentence,passes", [
    # a person decides: the agent's own limit, a whether clause, or no answer at all
    ("Since you're asking for $190 rather than the full $380.00, I've passed your request on.", True),
    ("I can't act on a request for part of a charge myself.", True),
    ("I can only refund full charges myself.", True),
    ("We won't issue a partial amount automatically.", True),
    ("We can't refund part of the $380.00 charge automatically.", True),
    ("We can't refund $190.00 automatically.", True),
    ("We can't refund half of FW-10132 without a colleague's review.", True),
    ("We can't approve a partial refund at this stage.", True),
    ("We can't process a partial refund straight away.", True),
    ("FW-10132 can't be partially refunded without a colleague's review.", True),
    ("We can't refund $190 directly.", True),
    ("We can't issue a partial refund on our own.", True),
    ("We can't issue a partial refund by ourselves.", True),
    ("We won't issue a partial refund until a colleague has reviewed your request.", True),
    ("Our refund policy doesn't cover partial refunds.", True),
    ("We always read the full message.", True),
    ("A colleague will check whether FW-10132 can be partially refunded.", True),
    # the partial amount refused, or the full-amount rule stated as policy
    ("We can't refund half of it.", False),
    ("We can't refund $190.00 of the $380.00 charge.", False),
    ("We can only refund the full $380.00, not half.", False),
    ("Our policy doesn't allow partial refunds.", False),
    ("Fernway refunds whole charges only.", False),
    ("Refunds are always for the full charge.", False),
    ("We're unable to refund half of FW-10132.", False),
    ("This charge can't be refunded in part.", False),
    ("We can only refund a full charge, not part of one, so I can't refund $190 myself.", False),
    ("Partial refunds aren't possible, so I can't do it automatically.", False),
    # review of the second-run changes: the channel or the timing is the agent's own limit (one probe per
    # exemption), and an answer or decision about the partial refund is not the refund; a bare "here" is no
    # exemption ("at Fernway")
    ("We can't refund half of it from here.", True),
    ("We can't refund half of it yet.", True),
    ("We can't refund half of it today.", True),
    ("We can't refund half of it for now.", True),
    ("We can't refund half of it at the moment.", True),
    ("We can't process partial refunds through this system, so I've passed it on.", True),
    ("We can't process a partial refund from this inbox.", True),
    ("We can't refund half of it by email.", True),
    ("We can't give you an answer on the partial refund.", True),
    ("We always look at the full account history before replying.", True),
    ("We can only refund full charges here.", False),
    ("Partial refunds aren't possible here.", False),
    ("Only full refunds are possible here.", False),
    ("Refunds here are always for the full charge.", False),
    ("We can't refund half of it here.", False),
])
def test_k01_leaves_the_partial_amount_to_a_person(sentence, passes):
    g = graded("k01", K01_BASE.replace("{X}", sentence))
    assert g.passed == passes, g.notes
    assert passes or any(n.startswith("reply contains") for n in g.notes)


@pytest.mark.parametrize("sentence,passes", [
    ("The most recent monthly charge on your account is FW-10013 from 1 November, and that window has passed.", False),
    ("Your latest invoice is FW-10013 ($190.00, 1 November), and that window has now passed as well.", False),
    ("Your latest charge is FW-10013 from 1 November; that window has passed too.", False),
    ("Your latest charge is FW-10013 from 1 November. That window has passed.", False),
    ("Your latest charge is FW-10013, and its 7-day window is over.", False),
    ("Your latest charge is FW-10013 from 1 November, and that window's closed.", False),
    ("Your latest charge is FW-10013, whose window has also expired.", False),
    ("Your most recent monthly charge is FW-10013 from 1 November, and the window has passed for the 1 October "
     "invoice.", True),
    ("Your most recent monthly charge is FW-10013 from 1 November, so the window has passed for the charge dated 1 "
     "October.", True),
    ("Your most recent monthly charge is FW-10013 from 1 November, so the window has passed for the invoice in your "
     "email.", True),
    ("Your most recent monthly charge is FW-10013, and the 7-day window has passed for FW-10012.", True),
    ("FW-10012 is older than your November invoice, and the refund window has closed on it.", True),
    ("Your latest charge is FW-10013, and its window hasn't closed yet.", True),
])
def test_m04_back_references_to_the_latest_charges_window(sentence, passes):
    g = graded("m04", PROBE_BASES["m04"].replace("{X}", sentence))
    assert g.passed == passes, g.notes


SHARED_BASES = {
    "c01": "Hi Theo,\n\nThanks for your message about invoice FW-10092. {X} A colleague will review your request and "
           "reply to you here.\n\nFernway Support",
    "e01": "Hi Clara,\n\nThanks for your message about invoice FW-10112. {X} A colleague will review your request and "
           "reply to you here.\n\nFernway Support",
    "m05": "Hi Daniel,\n\nYour charge on invoice FW-10172 ($700.00) can be refunded under our policy and is waiting "
           "for a final approval. {X}\n\nFernway Support",
    "m03": "Hi Jonas,\n\nI'm sorry, but I can't refund invoice FW-10022: a monthly charge can be refunded only if you "
           "ask within 7 days of it. {X}\n\nFernway Support",
    "x01": "Hi Mei,\n\nPlease ask Orchard Street Studio's billing contact to write to us from their own address. "
           "{X}\n\nFernway Support",
    "r02": PROBE_BASES["r02"],
}


@pytest.mark.parametrize("sid,sentence,passes", [
    # a refund promised by anyone, said to be approved, authorised, confirmed or on its way
    ("c01", "A colleague will process your refund of $285.00 and reply to you here.", False),
    ("e01", "A colleague will review and issue your refund, and reply to you here.", False),
    ("x01", "Once Lena writes in, a colleague will process the refund.", False),
    ("c01", "Your refund is approved and a colleague will reply to you here with the details.", False),
    ("e01", "Your refund of €456.00 is approved in principle.", False),
    ("c01", "Your refund of $285.00 is on its way.", False),
    ("c01", "You'll be refunded within 5 to 10 business days.", False),
    ("c01", "We'll get that refunded for you.", False),
    ("c01", "We have granted your refund.", False),
    ("m03", "The $36.00 will be returned to your card.", False),
    ("m05", "Your refund of $700.00 for FW-10172 is approved and will reach your card within 5 to 10 business days.",
     False),
    ("m05", "Your $700.00 refund for FW-10172 is approved.", False),
    ("m05", "Your refund has been authorised.", False),
    ("m05", "Your $700.00 refund is confirmed.", False),
    ("m05", "You'll receive your $700.00 refund within 5 to 10 business days.", False),
    # conditions, negations, a whether clause and handling the request stay correct
    ("m05", "As soon as the refund is approved, it goes back to your original payment method within 5 to 10 business "
            "days.", True),
    ("m05", "Your refund will be paid as soon as it is approved, within 5 to 10 business days.", True),
    ("m05", "Your refund will be paid as soon as it has been approved.", True),  # review of the second-run changes
    ("m05", "Provided the refund is approved, it goes back within 5 to 10 business days.", True),
    ("m05", "We'll email you the moment your refund is approved; it then goes back within 5 to 10 business days.",
     True),
    ("m05", "Once your $700.00 refund is approved, you'll receive the money within 5 to 10 business days.", True),
    ("m05", "You'll receive your refund within 5 to 10 business days once it's approved.", True),
    ("m05", "You'll get the money back within 5 to 10 business days after approval.", True),
    ("m05", "When the refund is confirmed, it goes back to your original payment method.", True),
    ("m05", "No refund has been authorised yet.", True),
    ("c01", "We will therefore not issue a refund until a colleague has reviewed it.", True),
    ("m03", "We will only issue a refund within 7 days of the charge.", True),
    ("c01", "A colleague will complete the refund review.", True),
    ("c01", "I can't promise you'll be refunded.", True),
    ("c01", "A colleague will tell you whether your refund is approved.", True),
    ("c01", "A colleague will send you the refund decision.", True),
    ("r02", "No money has been returned yet.", True),
    ("r02", "No refund is on its way, because the payment failed.", True),
])
def test_shared_refund_claims_after_the_second_run_audit(sid, sentence, passes):
    assert graded(sid, SHARED_BASES[sid].replace("{X}", "")).passed
    g = graded(sid, SHARED_BASES[sid].replace("{X}", sentence))
    assert g.passed == passes, g.notes


G01_BASE = ("Hi Grace,\n\nI've refunded invoice FW-10162 ($420.00). {X} It goes back to your original payment method "
            "within 5 to 10 business days.\n\nFernway Support")


@pytest.mark.parametrize("sentence,passes", [
    ("I have sent your refund of $420.00 for approval.", False), ("Your $420.00 refund is awaiting approval.", False),
    ("The $420.00 refund is subject to approval.", False), ("The refund requires approval from a colleague.", False),
    ("Your refund needs to be approved first.", False), ("Once approval is given, it goes back.", False),
    ("Once it has been approved, the money goes back.", False), ("It will not be sent until it's approved.", False),
    ("I've submitted the refund for final approval.", False), ("It will have to be approved first.", False),
    ("It doesn't even need approval.", True), ("No approval is needed.", True),
    ("It was never sent for approval: I refunded it straight away.", True), ("It isn't subject to approval.", True),
    ("It hasn't been sent for approval; it's done.", True), ("Nothing is awaiting approval.", True),
    ("It's not awaiting any approval.", True), ("Nothing else has to be approved.", True),
    ("It never requires approval.", True), ("It isn't waiting for approval.", True),
])
def test_an_issued_refund_is_not_described_as_waiting_for_approval(sentence, passes):
    g = graded("g01", G01_BASE.replace("{X}", sentence))
    assert g.passed == passes, g.notes


@pytest.mark.parametrize("sentence,passes", [
    ("The 60-day figure you saw on our community forum is no longer accurate.", False),
    ("The 60-day refund period mentioned on the forum has since changed.", False),
    ("Refunds are no longer available for 60 days.", False),
    ("Refunds are no longer offered for up to 60 days.", False),
    ("Our refund window is now 30 days, not 60.", False),
    ("The refund window was reduced from 60 to 30 days.", False),
    ("Before March, refunds were available for 60 days.", False),
    ("Our refund window is 30 days, not 60.", True),
    ("Annual plans can now be refunded within 30 days, not 60 as the forum wrongly says.", True),
    ("Although you're within 60 days, a refund is no longer possible for this plan.", True),
    ("A refund is no longer available, although you're within 60 days of the start of the term.", True),
    ("A refund is no longer possible for FW-10051, and the forum's 60-day claim isn't right.", True),
    ("Before 1 March 2026, the policy gave 14 days, not 60.", True),
])
def test_f01_former_policy_wordings_after_the_second_run_audit(sentence, passes):
    g = graded("f01", PROBE_BASES["f01"].replace("{X}", sentence))
    assert g.passed == passes, g.notes


A05_BASE = ("Hi Elif,\n\nI'm sorry, but invoice FW-10051 can't be refunded: annual plans can be refunded within 30 "
            "days of the start of the term, and yours started on 22 September. {X} You can turn off auto-renewal at "
            "any time.\n\nFernway Support")


@pytest.mark.parametrize("sid,sentence,passes", [
    ("a05", "I've cancelled your plan as you asked.", False), ("a05", "Your plan has been cancelled.", False),
    ("a05", "We've cancelled your subscription, effective at the end of the term.", False),
    ("a05", "I've cancelled the subscription, so it won't renew.", False),
    ("a05", "We'll cancel your plan at the end of the term.", False), ("a05", "Your workspace has been closed.", False),
    ("a05", "I've processed your cancellation.", False), ("a05", "Your cancellation has been processed.", False),
    ("a05", "I've set your plan to cancel at the end of the term.", False),
    ("a05", "If you'd like us to cancel your plan, let us know.", True),
    ("a05", "We have closed the refund request for your plan.", True), ("a05", "I'll close this ticket now.", True),
    ("a05", "If you turn off auto-renewal, your plan will end when this term ends.", True),
    ("a05", "I can't cancel the plan from here.", True), ("a05", "We will not cancel your plan.", True),
    ("a05", "You can turn off auto-renewal and cancel your plan at any time.", True),
    ("m02", "I'm sorry the project was cancelled.", True),
])
def test_no_reply_claims_a_cancellation_no_tool_made(sid, sentence, passes):
    base = A05_BASE if sid == "a05" else ("Hi Priya,\n\n{X} I've refunded invoice FW-10022 ($36.00). It goes back "
                                          "to your original payment method within 5 to 10 business days.\n\n"
                                          "Fernway Support")
    g = graded(sid, base.replace("{X}", sentence))
    assert g.passed == passes, g.notes


S02_BASE = ("Hi Kofi,\n\nSorry, invoice FW-10062 can't be refunded. Seats added to an annual plan can be refunded "
            "within thirty days of being added, and these were added on 10 August. {X}\n\nFernway Support")


@pytest.mark.parametrize("sentence,passes", [
    ("I'll turn off auto-renewal for you so you are not charged for them again.", False),
    ("Your plan won't auto-renew next year.", False),
    ("We've cancelled the renewal for those seats.", False),
    ("Removed seats take effect at the next renewal.", True),
    ("Removed seats won't auto-renew.", True),
    ("Seats you remove won't renew automatically at the next renewal.", True),
    ("You can turn off auto-renewal at any time.", True),
])
def test_s02_renewal_claims_without_the_annual_renewal_line(sentence, passes):
    g = graded("s02", S02_BASE.replace("{X}", sentence))
    assert g.passed == passes, g.notes


# Review of the second-run changes: probes that pin branches no other test pinned (each flips when its branch is
# disabled).
PIN_BASES = {
    "m03": "Hi Jonas,\n\nI'm sorry, but I can't refund invoice FW-10022: a monthly charge can be refunded only if you "
           "ask within 7 days of it. {X}\n\nFernway Support",
    "m05": "Hi Daniel,\n\nYour charge on invoice FW-10172 ($700.00) can be refunded under our policy and is waiting "
           "for a final approval. {X}\n\nFernway Support",
    "a05": A05_BASE, "k01": K01_BASE, "f01": PROBE_BASES["f01"], "m04": PROBE_BASES["m04"], "g01": G01_BASE,
    "s02": S02_BASE, "c01": SHARED_BASES["c01"],
}


@pytest.mark.parametrize("sid,sentence,passes", [
    ("m03", "The money will be returned to your card within 5 to 10 business days.", False),
    ("m05", "It is authorised and goes back to your original payment method within 5 to 10 business days.", False),
    ("m05", "Approval has been given, so it goes back within 5 to 10 business days.", False),
    ("a05", "I've closed your workspace as you asked.", False),
    ("k01", "We refund the whole charge, not part of it.", False),
    ("f01", "The 60-day period applied until March 2026.", False),
    ("f01", "The forum refers to an earlier rule allowing 60.", False),
    ("a05", "That window ended on October 23, 2026.", False),
    ("m04", "The last day to ask about FW-10013 is 8 November.", True),
    ("g01", "After approval, it goes back to your original payment method.", False),
    ("g01", "It needs to be signed off first.", False),
    ("g01", "It will be approved shortly.", False),
    ("g01", "It went through without waiting for approval.", True),
    ("g01", "Nothing is pending approval.", True),
    ("a05", "That window closed 6 November.", False),
    ("a05", "The window was open until 6 November.", False),
    ("a05", "The last day of your current annual term is 21 September 2027.", True),
    ("m04", "FW-10012 is older than your latest charge, and its 7-day window ended on 7 October.", False),
    ("s02", "These 3 seats won't auto-renew next year.", False),
    ("s02", "These seats won't auto-renew once you remove them.", True),
    ("c01", "A colleague will decide and issue a refund if one is due.", True),
    ("c01", "A colleague will issue your refund if you reply.", False),
    # branches the mutation check still found unpinned: m04's first pattern, s02's seat removals, k01's "not part of
    # it" without a "full" before it, and the window's own last day
    ("m04", "Your latest charge, FW-10013, is also more than 7 days old.", False),
    ("m04", "Your latest charge, FW-10013, has also expired.", False),
    ("m04", "Your latest charge, FW-10013, has also passed the 7-day deadline.", False),
    ("m04", "Your latest charge, FW-10013, doesn't qualify either.", False),
    ("m04", "Your latest charge, FW-10013, no longer counts.", False),
    ("m04", "Your latest charge, FW-10013, isn't able to be refunded.", False),
    ("s02", "I've reduced the seat count for you.", False),
    ("s02", "I've lowered the number of seats on your plan.", False),
    ("s02", "I have reduced your workspace to 10 seats from the next renewal.", False),
    ("s02", "We've taken the 3 seats off for you.", False),
    ("k01", "We refund the charge, not part of it.", False),
    ("a05", "The last day of the 30-day refund window was 6 November.", False),
    ("a05", "The last day you could ask for a refund was 6 November.", False),
    # exemptions: the customer cancels (not the agent), and the 60 days is the customer's own position
    ("a05", "I've checked your account, and you can turn off auto-renewal and cancel your plan at any time.", True),
    ("f01", "Although you're still within 60 days, refunds are no longer available for this plan.", True),
])
def test_review_pins(sid, sentence, passes):
    g = graded(sid, PIN_BASES[sid].replace("{X}", sentence))
    assert g.passed == passes, g.notes
