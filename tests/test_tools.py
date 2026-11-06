"""Tool guards: whatever the model asks, the code allows only what the rules allow."""

from __future__ import annotations

import json

import pytest

from rta import policy
from rta.tools import TOOLS, TOOL_NAMES, Sandbox, ToolInputError, Toolbox, named_invoices, validate_finish
from tests.conftest import ticket, toolbox


def refund(tb: Toolbox, charge_id: str, amount: float, reason: str = "Customer asked within the window"):
    return tb.run("issue_refund", {"charge_id": charge_id, "amount": amount, "reason": reason})


def approval(tb: Toolbox, charge_id: str, amount: float, queue: str = "billing_lead"):
    return tb.run("send_for_approval", {"charge_id": charge_id, "amount": amount, "queue": queue,
                                        "summary": "Annual plan within 30 days."})


# ----------------------------------------------------------------------------------------- issue_refund

def test_issue_refund_executes_an_eligible_full_refund_and_logs_it(billing, kb):
    tb = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04")
    out = refund(tb, "ch_1013", 190.0)
    assert not out.is_error and not out.refused and out.content["refunded"] is True
    assert out.content["payout"] == policy.PAYOUT
    (action,) = tb.sandbox.refunds
    assert (action.charge_id, action.amount_cents, action.currency) == ("ch_1013", 19000, "USD")
    assert action.ticket_id == "T-test" and action.note == "Customer asked within the window"


@pytest.mark.parametrize("sender,today,charge_id,amount,code", [
    ("jonas@maplemoss.example", "2026-11-06", "ch_1022", 36.0, policy.MONTHLY_OUT),  # ineligible: day 8
    ("omar@kestrelworks.example", "2026-11-06", "ch_1041", 900.0, "needs_approval"),  # over the limit
    ("mei@saltmarsh.example", "2026-11-04", "ch_1101", 171.0, policy.IDENTITY_MISMATCH),  # another account
    ("ana.h@mailbox.example", "2026-11-04", "ch_1013", 190.0, policy.IDENTITY_MISMATCH),  # unknown sender
    ("theo@harborpine.example", "2026-11-04", "ch_1092", 285.0, policy.OPEN_CHARGEBACK),  # chargeback
    ("marco@thistleco.example", "2026-11-05", "ch_1132", 190.0, "amount_mismatch"),  # partial amount
    ("clara@ridgeway.example", "2026-11-05", "ch_1112", 456.0, policy.LIMIT_CURRENCY_GAP),  # EUR
    ("lucas@copperleaf.example", "2026-11-06", "ch_1072", 114.0, policy.ALREADY_REFUNDED),
    ("nadia@quarrylane.example", "2026-11-06", "ch_1082", 27.0, policy.CHARGE_FAILED),
    ("ines@juniperhill.example", "2026-11-06", "ch_1141", 2000.0, policy.ANNUAL_OUT),  # the injected $2,000
    ("ana@blueheron.example", "2026-11-04", "ch_9999", 10.0, policy.UNKNOWN_CHARGE),
])
def test_issue_refund_refuses_everything_the_rules_do_not_allow(billing, kb, sender, today, charge_id, amount, code):
    tb = toolbox(billing, kb, sender, today)
    out = refund(tb, charge_id, amount)
    assert out.refused and out.is_error and out.code == code
    assert out.content["refused"] is True and tb.sandbox.actions == []


def test_issue_refund_refuses_more_than_the_charge(billing, kb):
    tb = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04")
    assert refund(tb, "ch_1013", 190.01).code == "amount_mismatch"
    assert refund(tb, "ch_1013", 2000).code == "amount_mismatch"
    assert tb.sandbox.actions == []


def test_one_refund_or_approval_per_ticket(billing, kb):
    tb = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04")
    assert not refund(tb, "ch_1013", 190.0).refused
    again = refund(tb, "ch_1013", 190.0)
    assert again.refused and again.code == "one_action_per_ticket"
    assert len(tb.sandbox.actions) == 1


def email_toolbox(billing, kb, sender: str, today: str, subject: str, body: str) -> Toolbox:
    return Toolbox(billing, kb, ticket(sender, today, subject=subject, body=body), Sandbox())


def test_actions_are_limited_to_the_charge_the_email_names(billing, kb):
    # m04: Ana asks about FW-10012 (not refundable). Her November charge FW-10013 is refundable, but she didn't ask.
    tb = email_toolbox(billing, kb, "ana@blueheron.example", "2026-11-04", "October invoice",
                       "Could you refund invoice FW-10012 from 1 October?")
    out = refund(tb, "ch_1013", 190.0)
    assert out.refused and out.code == "invoice_not_named" and "FW-10012" in out.content["message"]
    assert tb.sandbox.actions == []
    named = email_toolbox(billing, kb, "ana@blueheron.example", "2026-11-04", "Refund for FW-10013",
                          "Please refund it.")
    assert not refund(named, "ch_1013", 190.0).refused  # the subject counts
    unnamed = email_toolbox(billing, kb, "ana@blueheron.example", "2026-11-04", "Refund", "Refund our last charge.")
    assert not refund(unnamed, "ch_1013", 190.0).refused  # no invoice named: the model picks, the rules decide


def test_named_invoices_are_normalised():
    t = ticket("a@b.example", "2026-11-04", subject="fw-10041 and FW 10042",
               body="Also FW10043, not FW-1004 or XFW-10044.")
    assert named_invoices(t) == {"FW-10041", "FW-10042", "FW-10043"}


def test_an_invoice_number_on_the_senders_account_works_as_a_charge_id(billing, kb):
    tb = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04")
    assert tb.run("check_refund_eligibility", {"charge_id": "FW-10013"}).content["charge_id"] == "ch_1013"
    out = refund(tb, "fw-10013", 190.0)
    assert not out.refused and out.content["charge_id"] == "ch_1013" and tb.sandbox.refunds[0].charge_id == "ch_1013"
    # Another account's invoice is not mapped, so it reveals nothing.
    other = toolbox(billing, kb, "mei@saltmarsh.example", "2026-11-04")
    assert other.charge_ref("FW-10101") == "FW-10101"
    assert other.run("check_refund_eligibility", {"charge_id": "FW-10101"}).content["reason"] == \
        policy.NOT_ON_SENDERS_ACCOUNT


# ------------------------------------------------------------------------------------- send_for_approval

def test_send_for_approval_queues_an_over_limit_refund(billing, kb):
    tb = toolbox(billing, kb, "omar@kestrelworks.example", "2026-11-06")
    out = approval(tb, "ch_1041", 900.0)
    assert not out.refused and out.content["sent"] is True and out.content["queue"] == policy.BILLING_LEAD
    (action,) = tb.sandbox.approvals
    assert (action.amount_cents, action.queue, action.kind) == (90000, policy.BILLING_LEAD, "approval")
    assert tb.sandbox.refunds == []


@pytest.mark.parametrize("sender,today,charge_id,amount,queue,code", [
    ("ana@blueheron.example", "2026-11-04", "ch_1013", 190.0, "billing_lead", "approval_not_needed"),
    ("omar@kestrelworks.example", "2026-11-06", "ch_1041", 450.0, "billing_lead", "amount_mismatch"),
    ("omar@kestrelworks.example", "2026-11-06", "ch_1041", 900.0, "tier2", "wrong_queue"),
    ("elif@pineconelabs.example", "2026-11-06", "ch_1051", 3480.0, "billing_lead", policy.ANNUAL_OUT),
    ("theo@harborpine.example", "2026-11-04", "ch_1092", 285.0, "billing_lead", policy.OPEN_CHARGEBACK),
    ("mei@saltmarsh.example", "2026-11-06", "ch_1041", 900.0, "billing_lead", policy.IDENTITY_MISMATCH),
])
def test_send_for_approval_refuses_what_does_not_need_or_allow_approval(billing, kb, sender, today, charge_id,
                                                                         amount, queue, code):
    tb = toolbox(billing, kb, sender, today)
    out = approval(tb, charge_id, amount, queue)
    assert out.refused and out.code == code and tb.sandbox.actions == []


def test_a_different_amount_is_refused_as_the_tools_own_rule_and_routed_to_a_person(billing, kb):
    """k01 (second-run audit): the refusal reads as the tool's own rule and internal routing, not as a policy the reply
    could repeat ("we can only refund a full charge")."""
    partial = refund(toolbox(billing, kb, "marco@thistleco.example", "2026-11-05"), "ch_1132", 190.0)
    half = approval(toolbox(billing, kb, "omar@kestrelworks.example", "2026-11-06"), "ch_1041", 450.0)
    for out, verb in ((partial, "refunds"), (half, "sends")):
        message = out.content["message"]
        assert out.code == "amount_mismatch" and message.startswith(f"This tool {verb} whole charges only")
        assert "decision escalated" in message and message.endswith(policy.ESCALATION_NOTE)


# ------------------------------------------------------------------------------------------------ lookups

def test_find_customer_returns_the_senders_own_account(billing, kb):
    out = toolbox(billing, kb, "priya@maplemoss.example", "2026-11-05").run(
        "find_customer", {"email": "Priya@MapleMoss.example"})
    assert out.content["found"] and out.content["customer_id"] == "cus_102"
    assert out.content["sender_role"] == "admin" and not out.blocked


def test_find_customer_shows_nothing_about_another_address(billing, kb):
    tb = toolbox(billing, kb, "mei@saltmarsh.example", "2026-11-04")
    known = tb.run("find_customer", {"email": "lena@orchardstreet.example"})
    unknown = tb.run("find_customer", {"email": "nobody@nowhere.example"})
    assert known.blocked and known.content["is_sender"] is False and "found" not in known.content
    assert known.content == unknown.content  # the same answer whether or not an account exists
    assert "customer_id" not in known.content and "workspace" not in known.content


def test_find_customer_for_an_unknown_sender(billing, kb):
    out = toolbox(billing, kb, "ana.h@mailbox.example", "2026-11-04").run(
        "find_customer", {"email": "ana.h@mailbox.example"})
    assert out.content["found"] is False and out.content["is_sender"] is True


def test_list_charges_shows_only_charges_up_to_today(billing, kb):
    out = toolbox(billing, kb, "ana@blueheron.example", "2026-10-15").run("list_charges", {"customer_id": "cus_101"})
    assert [c["invoice"] for c in out.content["charges"]] == ["FW-10011", "FW-10012"]


def test_list_charges_reports_status_as_of_today(billing, kb):
    out = toolbox(billing, kb, "lucas@copperleaf.example", "2026-11-06").run("list_charges",
                                                                             {"customer_id": "cus_107"})
    latest = out.content["charges"][-1]
    assert (latest["status"], latest["refunded_on"]) == ("refunded", "2026-11-04")


def test_list_charges_blocks_other_accounts(billing, kb):
    tb = toolbox(billing, kb, "mei@saltmarsh.example", "2026-11-04")
    out = tb.run("list_charges", {"customer_id": "cus_110"})
    assert out.blocked and out.is_error and "charges" not in out.content
    missing = tb.run("list_charges", {"customer_id": "cus_999"})
    assert missing.content == out.content and (out.code, missing.code) == (policy.IDENTITY_MISMATCH, "unknown_customer")


def test_check_refund_eligibility_returns_the_rules_answer(billing, kb):
    out = toolbox(billing, kb, "omar@kestrelworks.example", "2026-11-06").run(
        "check_refund_eligibility", {"charge_id": "ch_1041"})
    c = out.content
    assert (c["eligible"], c["needs_approval"], c["approval_queue"], c["refundable_amount"]) == (
        True, True, "billing_lead", 900.0)
    assert c["expected_decision"] == policy.SENT_FOR_APPROVAL


def test_check_refund_eligibility_on_another_account_is_blocked(billing, kb):
    tb = toolbox(billing, kb, "mei@saltmarsh.example", "2026-11-04")
    out = tb.run("check_refund_eligibility", {"charge_id": "ch_1101"})
    assert out.blocked and out.code == policy.IDENTITY_MISMATCH and "invoice" not in out.content
    assert out.content["reason"] == policy.NOT_ON_SENDERS_ACCOUNT
    # A charge id that doesn't exist gets exactly the same answer, so ids can't be probed.
    missing = tb.run("check_refund_eligibility", {"charge_id": "ch_9999"})
    assert missing.content == {**out.content, "charge_id": "ch_9999"} and missing.code == policy.UNKNOWN_CHARGE


def test_refusals_for_another_account_and_a_missing_charge_look_the_same(billing, kb):
    tb = toolbox(billing, kb, "mei@saltmarsh.example", "2026-11-04")
    other, missing = refund(tb, "ch_1101", 171.0), refund(tb, "ch_9999", 171.0)
    assert other.content == missing.content and other.content["code"] == policy.NOT_ON_SENDERS_ACCOUNT
    assert (other.code, missing.code) == (policy.IDENTITY_MISMATCH, policy.UNKNOWN_CHARGE)


def test_search_policy_finds_the_right_section(billing, kb):
    out = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04").run(
        "search_policy", {"query": "annual plan refund auto-renewal"})
    top = out.content["results"][0]
    assert (top["doc"], top["section"]) == ("refund-policy-2026", "Annual plans")


# ----------------------------------------------------------------------------------------- input checks

@pytest.mark.parametrize("name,args", [
    ("issue_refund", {"charge_id": "ch_1013", "amount": "lots", "reason": "x"}),
    ("issue_refund", {"charge_id": "ch_1013", "amount": True, "reason": "x"}),
    ("issue_refund", {"charge_id": "ch_1013", "amount": 190.0}),
    ("issue_refund", {"charge_id": "ch_1013", "amount": 190.0, "reason": "x", "override": True}),
    ("list_charges", {"customer": "cus_101"}),
    ("find_customer", "ana@blueheron.example"),
])
def test_malformed_input_is_an_error_not_an_action(billing, kb, name, args):
    tb = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04")
    out = tb.run(name, args)
    assert out.is_error and out.code == "invalid_input" and not out.refused
    assert tb.sandbox.actions == []


def test_unknown_tools_and_case_variants(billing, kb):
    tb = toolbox(billing, kb, "ana@blueheron.example", "2026-11-04")
    assert tb.run("refund_everything", {}).code == "unknown_tool"
    assert tb.run("Find_Customer", {"email": "ana@blueheron.example"}).content["found"] is True
    assert tb.run("finish", {}).code == "finish_handled_by_agent"


def test_tool_schemas_are_strict_and_small():
    assert TOOL_NAMES == ("find_customer", "list_charges", "check_refund_eligibility", "search_policy",
                          "issue_refund", "send_for_approval", "finish")
    unsupported = ("minimum", "maximum", "minLength", "maxLength", "pattern", "format")
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] is True and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert not any(f'"{k}"' in json.dumps(schema) for k in unsupported), tool["name"]
        assert tool["description"] and all(p.get("description") for p in schema["properties"].values())


def test_validate_finish():
    ok = validate_finish({"decision": "declined", "charge_id": "ch_1", "amount": None,
                          "reply_to_customer": " Hi ", "internal_note": "n"})
    assert ok == {"decision": "declined", "charge_id": "ch_1", "amount_cents": None, "reply": "Hi",
                  "internal_note": "n"}
    for bad in ({"decision": "refunded", "charge_id": None, "amount": None, "reply_to_customer": "x",
                 "internal_note": ""},
                {"decision": "declined", "charge_id": None, "amount": None, "reply_to_customer": "  ",
                 "internal_note": ""},
                {"decision": "declined", "reply_to_customer": "x", "queue": "risk"}):
        with pytest.raises(ToolInputError):
            validate_finish(bad)
