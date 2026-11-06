"""Reconciliation: the model's finish stands only when it matches what the tools did and what the rules say."""

from __future__ import annotations

from datetime import date

import pytest

from rta import policy
from rta.reconcile import HOLDING_REPLY, Stated, audit, enforced_outcome, reconcile
from rta.tools import Action, Sandbox
from tests.conftest import ticket, toolbox

OK_REPLY = "Hi, I've refunded $190.00. It reaches your card within 5 to 10 business days.\n\nFernway Support"


def stated(decision: str, charge_id: str | None = None, amount: float | None = None, reply: str = OK_REPLY) -> Stated:
    return Stated(decision, charge_id, None if amount is None else round(amount * 100), reply, "note")


def run_refund(billing, kb, sender="ana@blueheron.example", today="2026-11-04", charge="ch_1013", amount=190.0):
    tb = toolbox(billing, kb, sender, today)
    out = tb.run("issue_refund", {"charge_id": charge, "amount": amount, "reason": "r"})
    assert not out.refused
    return tb


def test_consistent_refund_stands(billing, kb):
    tb = run_refund(billing, kb)
    final = reconcile(stated("refund_issued", "ch_1013", 190.0), tb.sandbox, billing, tb.ticket)
    assert final.consistent and final.decision == policy.REFUND_ISSUED and final.reply == OK_REPLY
    assert enforced_outcome(tb.sandbox, final) == policy.REFUND_ISSUED and final.queue is None


def test_refund_claimed_but_never_issued_is_escalated(billing, kb):
    t = ticket("ana@blueheron.example", "2026-11-04")
    final = reconcile(stated("refund_issued", "ch_1013", 190.0), Sandbox(), billing, t)
    assert final.decision == policy.ESCALATED and "refund_claimed_but_not_issued" in final.flags
    assert final.reply == HOLDING_REPLY and final.queue == policy.TIER2
    assert enforced_outcome(Sandbox(), final) == policy.ESCALATED


def test_refund_issued_but_finish_says_declined(billing, kb):
    tb = run_refund(billing, kb)
    final = reconcile(stated("declined", "ch_1013"), tb.sandbox, billing, tb.ticket)
    assert not final.consistent and any(f.startswith("action_taken_but_decision") for f in final.flags)
    assert enforced_outcome(tb.sandbox, final) == policy.REFUND_ISSUED  # the money moved; a person reviews


def test_finish_amount_must_match_the_executed_refund(billing, kb):
    tb = run_refund(billing, kb)
    final = reconcile(stated("refund_issued", "ch_1013", 95.0), tb.sandbox, billing, tb.ticket)
    assert final.flags == ["refund_differs_from_finish"]


def test_approval_claimed_must_match_the_queue_entry(billing, kb):
    tb = toolbox(billing, kb, "omar@kestrelworks.example", "2026-11-06")
    tb.run("send_for_approval", {"charge_id": "ch_1041", "amount": 900.0, "queue": "billing_lead", "summary": "s"})
    reply = "Your refund is allowed and waiting for a final approval.\n\nFernway Support"
    good = reconcile(stated("sent_for_approval", "ch_1041", 900.0, reply), tb.sandbox, billing, tb.ticket)
    assert good.consistent and good.queue == policy.BILLING_LEAD
    t = ticket("omar@kestrelworks.example", "2026-11-06")
    bad = reconcile(stated("sent_for_approval", "ch_1041", 900.0, reply), Sandbox(), billing, t)
    assert bad.flags == ["approval_claimed_but_not_sent"]


@pytest.mark.parametrize("sender,today,charge_id,flag", [
    ("ana@blueheron.example", "2026-11-04", "ch_1013", "decline_not_backed_by_rules:monthly_within_7_days"),
    ("marco@thistleco.example", "2026-11-05", "ch_1132", "decline_not_backed_by_rules:monthly_within_7_days"),
    ("clara@ridgeway.example", "2026-11-05", "ch_1112", "decline_not_backed_by_rules:non_usd_charge_limit_unclear"),
    ("ana@blueheron.example", "2026-11-04", None, "decline_names_no_charge"),
])
def test_a_decline_must_be_backed_by_the_rules(billing, sender, today, charge_id, flag):
    reply = "I'm sorry, but this charge can't be refunded.\n\nFernway Support"
    final = reconcile(stated("declined", charge_id, reply=reply), Sandbox(), billing, ticket(sender, today))
    assert flag in final.flags and final.decision == policy.ESCALATED


def test_a_backed_decline_stands(billing):
    reply = "I'm sorry, but this charge is outside the 7-day window.\n\nFernway Support"
    final = reconcile(stated("declined", "ch_1022", reply=reply), Sandbox(), billing,
                      ticket("jonas@maplemoss.example", "2026-11-06"))
    assert final.consistent and final.decision == policy.DECLINED


@pytest.mark.parametrize("decision,charge_id", [("declined", "ch_1092"), ("needs_details", None)])
def test_chargeback_accounts_always_go_to_risk(billing, decision, charge_id):
    reply = "Could you send the invoice number?\n\nFernway Support"
    final = reconcile(stated(decision, charge_id, reply=reply), Sandbox(), billing,
                      ticket("theo@harborpine.example", "2026-11-04"))
    assert "open_chargeback_goes_to_a_person" in final.flags
    assert (final.decision, final.queue) == (policy.ESCALATED, policy.RISK)


def test_escalations_are_routed_by_code(billing):
    reply = "A colleague will review your request.\n\nFernway Support"
    risk = reconcile(stated("escalated", "ch_1092", reply=reply), Sandbox(), billing,
                     ticket("theo@harborpine.example", "2026-11-04"))
    tier2 = reconcile(stated("escalated", "ch_1132", reply=reply), Sandbox(), billing,
                      ticket("marco@thistleco.example", "2026-11-05"))
    assert (risk.consistent, risk.queue, tier2.consistent, tier2.queue) == (True, policy.RISK, True, policy.TIER2)


def test_internal_terms_in_the_reply_withhold_it(billing, kb):
    tb = run_refund(billing, kb)
    leak = "Refunds under $500 don't need the Billing Lead, so I've refunded $190.00."
    final = reconcile(stated("refund_issued", "ch_1013", 190.0, leak), tb.sandbox, billing, tb.ticket)
    assert final.decision == policy.ESCALATED and final.reply == HOLDING_REPLY
    assert any(f.startswith("internal_terms_in_reply") for f in final.flags)
    assert policy.internal_terms(HOLDING_REPLY) == []


def test_a_refund_of_exactly_the_limit_may_state_its_amount(kb):
    from rta.billing import Billing

    billing = Billing.from_dict({
        "customers": [{"id": "c1", "workspace": "Studio", "contact_name": "Bo", "billing_contact": "bo@studio.example",
                       "admins": [], "plan": "team", "billing": "monthly", "seats": 5, "currency": "USD"}],
        "charges": [{"id": "ch_1", "customer_id": "c1", "invoice": "FW-20001", "date": "2026-11-02",
                     "type": "monthly_subscription", "plan": "team", "billing": "monthly", "seats": 5, "amount": 500.0,
                     "currency": "USD", "status": "paid"}]})
    tb = toolbox(billing, kb, "bo@studio.example", "2026-11-04")
    assert not tb.run("issue_refund", {"charge_id": "ch_1", "amount": 500.0, "reason": "r"}).refused
    ok = reconcile(stated("refund_issued", "ch_1", 500.0, "I've refunded $500.00 to your card."), tb.sandbox, billing,
                   tb.ticket)
    assert ok.consistent and ok.decision == policy.REFUND_ISSUED
    leak = reconcile(stated("refund_issued", "ch_1", 500.0, "Refunds above $500 need approval; I've refunded $500.00."),
                     tb.sandbox, billing, tb.ticket)
    assert leak.flags == ["internal_terms_in_reply:the $500 limit"]
    # Without an executed $500.00 refund, a bare $500 is the limit.
    assert "the $500 limit" in policy.internal_terms("I've refunded $500.00 to your card.")


@pytest.mark.parametrize("sender,today,decision,charge_id,tools,flag", [
    # needs_details on a charge the sender may refund is not backed by the rules
    ("ana@blueheron.example", "2026-11-04", "needs_details", "ch_1013", (),
     "needs_details_not_backed_by_rules:monthly_within_7_days"),
    # backed: someone else's account (x01), an unknown sender (x02), no such charge, no charge named (n01)
    ("mei@saltmarsh.example", "2026-11-04", "needs_details", "ch_1101", (), None),
    ("ana.h@mailbox.example", "2026-11-04", "needs_details", "ch_1013", (), None),
    ("ana@blueheron.example", "2026-11-04", "needs_details", "FW-10101", (), None),
    ("hana@willowwren.example", "2026-11-06", "needs_details", None, ("find_customer", "list_charges"), None),
    # not_refund_request that names a charge or used the refund tools
    ("ana@blueheron.example", "2026-11-04", "not_refund_request", "ch_1013", (),
     "not_refund_request_after_refund_tools"),
    ("ana@blueheron.example", "2026-11-04", "not_refund_request", None, ("find_customer", "check_refund_eligibility"),
     "not_refund_request_after_refund_tools"),
    ("ana@blueheron.example", "2026-11-04", "not_refund_request", None, ("find_customer", "issue_refund"),
     "not_refund_request_after_refund_tools"),
    ("ravi@blueheron.example", "2026-11-04", "not_refund_request", None, ("find_customer", "search_policy"), None),
])
def test_needs_details_and_not_refund_request_must_fit_the_tools_and_rules(billing, sender, today, decision,
                                                                            charge_id, tools, flag):
    reply = "Could you tell us more?\n\nFernway Support"
    final = reconcile(stated(decision, charge_id, reply=reply), Sandbox(), billing, ticket(sender, today),
                      tools_called=tools)
    if flag is None:
        assert final.consistent and final.decision == decision, final.flags
    else:
        assert final.flags == [flag] and final.decision == policy.ESCALATED and final.reply == HOLDING_REPLY


def test_audit_catches_an_action_on_a_charge_the_email_does_not_name(billing):
    t = ticket("ana@blueheron.example", "2026-11-04", body="Could you refund invoice FW-10012 from 1 October?")
    forged = Sandbox([Action("x", "refund", "ch_1013", "cus_101", "FW-10013", 19000, "USD", None, "forged",
                             t.ticket_id, date(2026, 11, 4))])
    assert audit(forged, billing, t) == ["refund:ch_1013:invoice_not_named"]


def test_no_finish_is_escalated(billing):
    final = reconcile(None, Sandbox(), billing, ticket("ana@blueheron.example", "2026-11-04"), "turn_cap")
    assert (final.decision, final.flags, final.reply, final.queue) == (
        policy.ESCALATED, ["no_finish:turn_cap"], HOLDING_REPLY, policy.TIER2)


def test_audit_passes_legitimate_actions(billing, kb):
    tb = run_refund(billing, kb)
    assert audit(tb.sandbox, billing, tb.ticket) == []


def test_audit_catches_actions_the_rules_do_not_allow(billing):
    t = ticket("jonas@maplemoss.example", "2026-11-06")
    forged = Sandbox([
        Action("x1", "refund", "ch_1022", "cus_102", "FW-10022", 3600, "USD", None, "forged", t.ticket_id,
               date(2026, 11, 6)),
        Action("x2", "refund", "ch_1021", "cus_102", "FW-10021", 100, "USD", None, "forged", t.ticket_id,
               date(2026, 11, 6)),
    ])
    found = audit(forged, billing, t)
    assert "more_than_one_action" in found
    assert any(v.startswith("refund:ch_1022:not_allowed") for v in found)
    assert any(v.startswith("refund:ch_1021:not_allowed") for v in found)


def test_audit_catches_a_wrong_amount_or_queue(billing):
    t = ticket("omar@kestrelworks.example", "2026-11-06")
    wrong_queue = Sandbox([Action("x", "approval", "ch_1041", "cus_104", "FW-10041", 90000, "USD", "tier2", "s",
                                  t.ticket_id, date(2026, 11, 6))])
    wrong_amount = Sandbox([Action("y", "approval", "ch_1041", "cus_104", "FW-10041", 100, "USD", "billing_lead", "s",
                                   t.ticket_id, date(2026, 11, 6))])
    assert audit(wrong_queue, billing, t) == ["approval:ch_1041:wrong_queue"]
    assert audit(wrong_amount, billing, t) == ["approval:ch_1041:wrong_amount"]


# Rule 5b (6 Nov audit): with no charge named, "needs details" is not backed when the verified sender may act on
# their account and every invoice the email names is a charge on it dated on or before the ticket date.
@pytest.mark.parametrize("sender,today,body,flags,queue", [
    # d01: the billing contact names his own invoice (Haiku asked him to write in again)
    ("daniel@oakmere.example", "2026-11-03", "Invoice FW-10172 charged us for 20 seats. Please correct it.",
     ["needs_details_not_backed:sender_and_invoice_verified"], policy.TIER2),
    # an admin, not only the billing contact (m02's sender)
    ("priya@maplemoss.example", "2026-11-05", "Can we get FW-10022 refunded?",
     ["needs_details_not_backed:sender_and_invoice_verified"], policy.TIER2),
    # an account with an open chargeback still goes to Risk
    ("theo@harborpine.example", "2026-11-04", "Please refund our November invoice, FW-10092.",
     ["needs_details_not_backed:sender_and_invoice_verified", "open_chargeback_goes_to_a_person"], policy.RISK),
    # backed: no invoice named (n01), another account's invoice (x01), an unknown sender (x02), an invoice that
    # doesn't exist, one dated after the ticket date, and one of two named invoices on another account
    ("hana@willowwren.example", "2026-11-06", "Could you refund the payment we made last month?", [], None),
    ("mei@saltmarsh.example", "2026-11-04", "Could you refund their invoice FW-10101?", [], None),
    ("ana.h@mailbox.example", "2026-11-04", "Please refund invoice FW-10013.", [], None),
    ("daniel@oakmere.example", "2026-11-03", "Please refund invoice FW-99999.", [], None),
    ("ana@blueheron.example", "2026-10-15", "Please refund invoice FW-10013.", [], None),
    ("mei@saltmarsh.example", "2026-11-04", "Please refund FW-10031 and FW-10101.", [], None),
])
def test_needs_details_with_no_charge_when_code_knows_who_and_what(billing, sender, today, body, flags, queue):
    reply = "Could you write to us from the billing contact's address?\n\nFernway Support"
    final = reconcile(stated("needs_details", None, reply=reply), Sandbox(), billing, ticket(sender, today, body=body),
                      tools_called=("finish",))
    assert final.flags == flags
    if flags:
        assert (final.decision, final.queue, final.reply) == (policy.ESCALATED, queue, HOLDING_REPLY)
    else:
        assert final.consistent and final.decision == policy.NEEDS_DETAILS and final.reply == reply


@pytest.mark.parametrize("sender,today,body,charge_id,flags", [
    # d01 again, but finish names a charge the sender can't act on: a guessed id, or another account's charge.
    # The rules back "needs details" for that charge, yet code knows who is asking and about what (rule 5b).
    ("daniel@oakmere.example", "2026-11-03", "Invoice FW-10172 charged us for 20 seats.", "ch_10172",
     ["needs_details_not_backed:sender_and_invoice_verified"]),
    ("daniel@oakmere.example", "2026-11-03", "Invoice FW-10172 charged us for 20 seats.", "ch_1013",
     ["needs_details_not_backed:sender_and_invoice_verified"]),
    # the right charge is checked by rule 5 first, as before
    ("daniel@oakmere.example", "2026-11-03", "Invoice FW-10172 charged us for 20 seats.", "ch_1172",
     ["needs_details_not_backed_by_rules:monthly_within_7_days"]),
    # backed: another account's invoice and charge (x01), an unknown sender (x02)
    ("mei@saltmarsh.example", "2026-11-04", "Could you refund their invoice FW-10101?", "ch_1101", []),
    ("ana.h@mailbox.example", "2026-11-04", "Please refund invoice FW-10013.", "ch_1013", []),
])
def test_needs_details_on_a_charge_the_sender_cannot_act_on(billing, sender, today, body, charge_id, flags):
    final = reconcile(stated("needs_details", charge_id), Sandbox(), billing, ticket(sender, today, body=body),
                      tools_called=("finish",))
    assert final.flags == flags
    assert final.decision == (policy.ESCALATED if flags else policy.NEEDS_DETAILS)
