"""The rules at their boundaries, on small synthetic accounts (dates under full control)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rta import policy
from rta.billing import Billing

TODAY = date(2026, 11, 6)
SENDER = "owner@studio.example"


def acct(charges: list[dict], *, plan: str = "team", billing: str = "monthly", currency: str = "USD",
         chargebacks: list[dict] | None = None, others: list[dict] | None = None) -> Billing:
    customers = [{"id": "c1", "workspace": "Studio", "contact_name": "Ana", "billing_contact": SENDER,
                  "admins": ["admin@studio.example"], "plan": plan, "billing": billing, "seats": 5,
                  "currency": currency}]
    customers += others or []
    return Billing.from_dict({"customers": customers, "charges": charges, "chargebacks": chargebacks or []})


def ch(cid: str, days_ago: int, *, type: str = "monthly_subscription", amount: float = 95.0, status: str = "paid",
       billing: str = "monthly", currency: str = "USD", customer: str = "c1", **extra) -> dict:
    d = {"id": cid, "customer_id": customer, "invoice": f"FW-{cid}", "date": (TODAY - timedelta(days=days_ago)).isoformat(),
         "type": type, "plan": "team", "billing": billing, "amount": amount, "currency": currency, "status": status,
         "seats": 5}
    d.update(extra)
    return d


def check(b: Billing, charge_id: str, sender: str = SENDER, today: date = TODAY) -> policy.Eligibility:
    return policy.check_charge(b, charge_id, sender, today)


# ------------------------------------------------------------------------------------------------ monthly

@pytest.mark.parametrize("days", [0, 1, 6, 7])
def test_monthly_charge_is_refundable_through_day_7(days):
    e = check(acct([ch("m", days)]), "m")
    assert (e.eligible, e.reason, e.expected_decision) == (True, policy.MONTHLY_IN, policy.REFUND_ISSUED)
    assert e.amount_cents == 9500 and e.days_since == days and e.window_days == 7


@pytest.mark.parametrize("days", [8, 9, 30])
def test_monthly_charge_is_not_refundable_from_day_8(days):
    e = check(acct([ch("m", days)]), "m")
    assert (e.eligible, e.reason, e.expected_decision) == (False, policy.MONTHLY_OUT, policy.DECLINED)
    assert e.amount_cents is None and "7-day window" in e.detail


def test_only_the_most_recent_monthly_charge_can_be_refunded():
    b = acct([ch("old", 6), ch("new", 2)])  # both inside 7 days, e.g. after a plan change
    old, new = check(b, "old"), check(b, "new")
    assert (old.reason, old.expected_decision) == (policy.NOT_MOST_RECENT, policy.DECLINED)
    assert "FW-new" in old.detail
    # the answer is about the checked charge only: it says nothing about whether the latest one can be refunded
    assert old.detail.endswith("This answer is about this charge only.") and "refundable" not in old.detail
    assert new.expected_decision == policy.REFUND_ISSUED


# The detail gives the window's dates, so the model never works them out: the last day for a charge inside the
# window, and the day the window ended (its last day) for one outside it. Day N is inside (DECISIONS.md, gap 1).
@pytest.mark.parametrize("days,detail", [
    (0, "was made today. This is inside the 7-day window; its last day is 2026-11-13."),
    (1, "was made 1 day ago. This is inside the 7-day window; its last day is 2026-11-12."),
    (7, "was made 7 days ago. This is inside the 7-day window; its last day is 2026-11-06."),
    (8, "was made 8 days ago. The 7-day window ended on 2026-11-05, its last day, so this is outside the 7-day "
        "window."),
])
def test_the_monthly_detail_gives_the_window_dates(days, detail):
    assert detail in check(acct([ch("m", days)]), "m").detail


@pytest.mark.parametrize("type_,days,detail", [
    ("annual_subscription", 30, "30 days ago. This is inside the 30-day window; its last day is 2026-11-06."),
    ("annual_subscription", 31, "31 days ago. The 30-day window ended on 2026-11-05, its last day"),
    ("seat_addition", 30, "30 days ago. This is inside the 30-day window; its last day is 2026-11-06."),
    ("seat_addition", 31, "31 days ago. The 30-day window ended on 2026-11-05, its last day"),
])
def test_annual_and_seat_details_give_the_window_dates(type_, days, detail):
    extra = {"seats": None, "seats_added": 2, "period_start": "2026-05-10", "period_end": "2027-05-10"} \
        if type_ == "seat_addition" else {}
    b = acct([ch("a", days, type=type_, billing="annual", amount=420.0, **extra)], billing="annual")
    assert detail in check(b, "a").detail


def test_the_window_dates_on_the_benchmark_tickets(billing):
    def detail(charge_id, sender, today):
        return policy.check_charge(billing, charge_id, sender, date.fromisoformat(today)).detail

    assert "ended on 2026-10-22, its last day" in detail("ch_1051", "elif@pineconelabs.example", "2026-11-06")  # a05
    assert "ended on 2026-11-05, its last day" in detail("ch_1022", "jonas@maplemoss.example", "2026-11-06")  # m03
    assert "ended on 2026-11-19, its last day" in detail("ch_1031", "mei@saltmarsh.example", "2026-11-20")  # a04
    assert "its last day is 2026-11-19" in detail("ch_1031", "mei@saltmarsh.example", "2026-11-19")  # a03, day 30
    assert policy.last_day_in_window(date(2026, 10, 29), 7) == date(2026, 11, 5)


def test_most_recent_rule_is_checked_before_the_window():
    e = check(acct([ch("old", 35), ch("new", 5)]), "old")
    assert e.reason == policy.NOT_MOST_RECENT


def test_a_failed_attempt_is_not_the_most_recent_charge():
    b = acct([ch("paid", 5), ch("failed", 1, status="failed")])
    assert check(b, "paid").expected_decision == policy.REFUND_ISSUED
    assert (check(b, "failed").reason, check(b, "failed").expected_decision) == (policy.CHARGE_FAILED, policy.DECLINED)


def test_a_refunded_charge_still_counts_as_the_most_recent():
    b = acct([ch("older", 4), ch("latest", 1, status="refunded", refunded_on=TODAY.isoformat())])
    assert check(b, "older").reason == policy.NOT_MOST_RECENT


# ------------------------------------------------------------------------------------------------ annual

@pytest.mark.parametrize("days", [0, 29, 30])
def test_annual_plan_is_refundable_through_day_30(days):
    b = acct([ch("a", days, type="annual_subscription", billing="annual", amount=420.0)], billing="annual")
    e = check(b, "a")
    assert (e.eligible, e.reason, e.expected_decision) == (True, policy.ANNUAL_IN, policy.REFUND_ISSUED)
    assert e.amount_cents == 42000


@pytest.mark.parametrize("days", [31, 45])
def test_annual_plan_after_30_days_is_declined_and_mentions_auto_renewal(days):
    b = acct([ch("a", days, type="annual_subscription", billing="annual", amount=420.0)], billing="annual")
    e = check(b, "a")
    assert (e.reason, e.expected_decision) == (policy.ANNUAL_OUT, policy.DECLINED)
    assert "auto-renewal" in e.detail


# ------------------------------------------------------------------------------------------------- seats

def seats(days: int, billing: str = "annual", amount: float = 209.10) -> dict:
    return ch("s", days, type="seat_addition", billing=billing, amount=amount, seats=None, seats_added=2,
              period_start="2026-05-10", period_end="2027-05-10")


@pytest.mark.parametrize("days,reason,decision", [
    (30, policy.SEATS_IN, policy.REFUND_ISSUED),
    (31, policy.SEATS_OUT, policy.DECLINED),
])
def test_seat_additions_on_annual_plans_have_a_30_day_window(days, reason, decision):
    e = check(acct([seats(days)], billing="annual"), "s")
    assert (e.reason, e.expected_decision) == (reason, decision)


def test_seat_additions_on_monthly_plans_are_a_policy_gap_for_a_person():
    e = check(acct([seats(1, billing="monthly", amount=35.47)]), "s")
    assert (e.eligible, e.reason, e.expected_decision, e.route) == (
        False, policy.SEATS_ON_MONTHLY, policy.ESCALATED, policy.TIER2)


# ------------------------------------------------------------------------------- nothing to refund, as of date

def test_failed_charge_has_nothing_to_refund_even_inside_the_window():
    e = check(acct([ch("f", 1, status="failed")]), "f")
    assert (e.eligible, e.reason, e.expected_decision) == (False, policy.CHARGE_FAILED, policy.DECLINED)
    assert "no money was taken" in e.detail


def test_refunded_charge_counts_as_refunded_only_from_its_refund_date():
    b = acct([ch("r", 3, status="refunded", refunded_on="2026-11-05")])
    assert check(b, "r").reason == policy.ALREADY_REFUNDED
    assert check(b, "r", today=date(2026, 11, 4)).expected_decision == policy.REFUND_ISSUED


def test_charges_after_today_do_not_exist_yet():
    b = acct([ch("future", -2)])
    e = check(b, "future")
    assert (e.reason, e.expected_decision) == (policy.UNKNOWN_CHARGE, policy.NEEDS_DETAILS)


def test_unknown_charge_needs_details():
    assert check(acct([]), "nope").reason == policy.UNKNOWN_CHARGE


# ---------------------------------------------------------------------------------------------- identity

@pytest.mark.parametrize("sender", [SENDER, "admin@studio.example", "OWNER@Studio.Example",
                                    "Ana <owner@studio.example>"])
def test_billing_contact_and_admins_may_act(sender):
    assert check(acct([ch("m", 1)]), "m", sender=sender).expected_decision == policy.REFUND_ISSUED


@pytest.mark.parametrize("sender", ["member@studio.example", "owner@studio.example.attacker.example",
                                    "someone@else.example"])
def test_anyone_else_gets_needs_details_and_no_account_data(sender):
    e = check(acct([ch("m", 1)]), "m", sender=sender)
    assert (e.reason, e.expected_decision) == (policy.IDENTITY_MISMATCH, policy.NEEDS_DETAILS)
    shown = e.for_agent()
    assert set(shown) == {"charge_id", "eligible", "reason", "expected_decision", "detail"}
    # the same answer as for a charge that doesn't exist
    assert shown == {**check(acct([]), "m", sender=sender).for_agent(), "charge_id": "m"}


def test_identity_is_checked_before_anything_about_the_account():
    b = acct([ch("m", 1)], chargebacks=[{"id": "cb", "customer_id": "c1", "charge_id": "m",
                                          "opened_on": "2026-11-01", "status": "open"}])
    assert check(b, "m", sender="someone@else.example").reason == policy.IDENTITY_MISMATCH


def test_a_request_about_another_account_is_refused_even_from_a_known_customer():
    other = {"id": "c2", "workspace": "Other", "contact_name": "Bo", "billing_contact": "bo@other.example",
             "admins": [], "plan": "team", "billing": "monthly", "seats": 3, "currency": "USD"}
    b = acct([ch("m", 1, customer="c2")], others=[other])
    assert check(b, "m", sender=SENDER).reason == policy.IDENTITY_MISMATCH
    assert check(b, "m", sender="bo@other.example").expected_decision == policy.REFUND_ISSUED


# -------------------------------------------------------------------------------------- chargebacks, routes

def test_open_chargeback_blocks_refunds_and_routes_to_risk():
    b = acct([ch("old", 30), ch("m", 1)], chargebacks=[{"id": "cb", "customer_id": "c1", "charge_id": "old",
                                                       "opened_on": "2026-10-20", "status": "open"}])
    e = check(b, "m")
    assert (e.eligible, e.reason, e.expected_decision, e.route) == (False, policy.OPEN_CHARGEBACK, policy.ESCALATED,
                                                                     policy.RISK)
    assert policy.escalation_route(b, SENDER, TODAY) == policy.RISK


@pytest.mark.parametrize("cb", [
    {"id": "cb", "customer_id": "c1", "charge_id": "m", "opened_on": "2026-11-07", "status": "open"},  # not yet
    {"id": "cb", "customer_id": "c1", "charge_id": "m", "opened_on": "2026-10-01", "status": "closed"},
])
def test_future_or_closed_chargebacks_do_not_block(cb):
    b = acct([ch("m", 1)], chargebacks=[cb])
    assert check(b, "m").expected_decision == policy.REFUND_ISSUED
    assert policy.escalation_route(b, SENDER, TODAY) == policy.TIER2


def test_escalations_from_unknown_senders_go_to_tier2():
    assert policy.escalation_route(acct([]), "nobody@nowhere.example", TODAY) == policy.TIER2


# ------------------------------------------------------------------------------------------ limit, currency

@pytest.mark.parametrize("amount,decision,route", [
    (500.00, policy.REFUND_ISSUED, None),
    (500.01, policy.SENT_FOR_APPROVAL, policy.BILLING_LEAD),
    (499.99, policy.REFUND_ISSUED, None),
])
def test_agent_limit_is_500_inclusive(amount, decision, route):
    e = check(acct([ch("m", 1, amount=amount)]), "m")
    assert (e.expected_decision, e.route) == (decision, route)
    assert e.needs_approval == (decision == policy.SENT_FOR_APPROVAL)
    assert e.for_agent()["approval_queue"] == route


def test_eligible_charge_in_eur_goes_to_a_person():
    e = check(acct([ch("m", 1, amount=120.0, currency="EUR")], currency="EUR"), "m")
    assert (e.eligible, e.reason, e.expected_decision, e.route) == (True, policy.LIMIT_CURRENCY_GAP, policy.ESCALATED,
                                                                     policy.TIER2)


def test_ineligible_charge_in_eur_is_simply_declined():
    e = check(acct([ch("m", 8, amount=120.0, currency="EUR")], currency="EUR"), "m")
    assert e.expected_decision == policy.DECLINED


def test_requests_before_the_2026_policy_are_out_of_scope():
    early = acct([ch("m", 0) | {"date": "2026-02-25"}])
    e = policy.check_charge(early, "m", SENDER, date(2026, 2, 27))
    assert (e.reason, e.expected_decision, e.route) == (policy.BEFORE_CURRENT_POLICY, policy.ESCALATED, policy.TIER2)
    # From 1 March 2026 the current policy applies to the request, whenever the charge was made.
    assert policy.check_charge(early, "m", SENDER, date(2026, 3, 1)).expected_decision == policy.REFUND_ISSUED


def test_for_agent_shows_the_only_refundable_amount():
    e = check(acct([ch("m", 1, amount=190.0)]), "m")
    shown = e.for_agent()
    assert shown["refundable_amount"] == 190.0 and shown["expected_decision"] == policy.REFUND_ISSUED
    declined = check(acct([ch("m", 9)]), "m").for_agent()
    assert declined["refundable_amount"] is None


# ------------------------------------------------------------------------------------------- reply guard

@pytest.mark.parametrize("text", [
    "Our Billing Lead will approve it.", "Sent to billing_lead.", "The Risk team will contact you.",
    "Escalated to Tier 2.", "tier2 will review", "As a goodwill gesture...", "a good-will credit",
    "Refunds above $500 need approval.", "That's over $500.00.", "the limit is 500 USD", "our approval limit",
    # paraphrases a model may use once the exact names are forbidden
    "This is above my refund limit, so a colleague must approve it.", "Refunds over 500 need a manager's sign-off.",
    "Refunds above 500.00 USD need approval.", "Anything over 500 US dollars goes for approval.",
    "I've sent this to Risk for review.", "Our billing team lead will approve it.", "It is above our approval threshold.",
    "Tier II will look at it.", "second-tier support will look at it", "Refunds of $500 or less go through at once.",
    # paraphrases of the procedure without a name or a number (6 Nov audit)
    "A manager needs to sign off on refunds this large.",
    "Refunds above a certain amount need a senior colleague's approval.", "Our approvals team will review it.",
    "I have passed this to our fraud team.", "I have escalated it to level 2 support.",
    "The amount exceeds what I can approve myself.", "larger refunds need sign-off",
    "Because there is an open chargeback on your account, we can't issue refunds.",
    # the account's chargeback named without "open" (review of the audit changes)
    "Because of the chargeback on your October invoice, a colleague will review this and reply to you here.",
    "The chargeback you raised on FW-10091 means a colleague will review this.",
    "There's a chargeback for invoice FW-10091, so a colleague will reply to you here.",
    # more paraphrases of the approval procedure and the chargeback rule (second-run audit)
    "Refunds of this size need a second approval from a senior team member.",
    "This amount is above what support can sign off, so it is waiting for a final approval.",
    "Because the amount is over our support approval level, it is waiting for approval.",
    "Our finance team needs to approve refunds of this size.",
    "It is too large for me to approve, so it is waiting for a final approval.",
    "The refund of $700.00 is too large for me to approve.",
    "This needs my team lead's approval.",
    "A team lead has to approve refunds like this one.",
    "Bigger refunds need a second approval.",
    "Since there is an open dispute with your bank on the October payment, a colleague will review this.",
    "Because your bank is disputing the October payment, we can't refund anything right now.",
    "Because of the open card dispute on your account, a colleague will reply to you here.",
    # review of the second-run changes: probes that only one branch of the role entry catches, so deleting it
    # fails a test
    "Our team lead will review it and reply to you here.", "It needs approval from a team lead first.",
    "Refunds of this size go to a colleague.", "I've passed this to our finance team.",
    "It needs sign-off from a team lead.", "Amounts of that size go to a colleague for review.",
])
def test_reply_guard_catches_internal_terms(text):
    assert policy.internal_terms(text)


@pytest.mark.parametrize("text", [
    "I've refunded $1,500.00 to your card.", "Your plan costs $5,000 a year.", "We refunded $190.00.",
    "It arrives within 5 to 10 business days.", "Our billing team will reply.", "Your Team plan renews soon.",
    "A colleague will review your request.", "", "We refunded 1,500 USD.", "We don't want you to risk losing work.",
    "The second line of your invoice shows the seats.", "Your card's spending limit may be the cause.",
    "Starter allows up to 5 seats.", "It's within the 30-day refund window.",
    "Line 2 of invoice FW-10172 lists 20 seats, so a colleague will check it and reply.",
    "Your customer success manager can help once the approval is through.",
    "If you'd rather not raise a chargeback with your bank, a colleague will reply here soon.",
    "I've refunded $190.00, so there's no need to raise a chargeback with your bank.",
    "The chargeback process at your bank can take a few weeks.", "Your chargeback rights with your bank don't change.",
    "We can only refund the full charge, not a certain amount of it.",
    "It can take a certain amount of time for your bank to show it, usually 5 to 10 business days.",
    # the customer's own invoice dispute (d01) and the customer's own team are not internal (second-run audit)
    "We'll review your open dispute about the invoice and reply to you.",
    "I've passed your pending dispute about the seat count to a colleague, who will reply.",
    "A colleague will look at your existing dispute about invoice FW-10172 and reply.",
    "I've logged an open dispute about the 20 seats on FW-10172, and a colleague will review it and reply to you here.",
    "I've passed your dispute about the seat count to a colleague.",
    "If you dispute a charge with your bank, the bank will contact us.",
    "If you raise a dispute with your bank, it can take a few weeks.",
    "Please forward this to your finance team.", "Your team lead can write to us from their own address.",
    "Please ask your team lead or another admin to write in from their own address.",
    "If your finance team needs a corrected invoice, a colleague will reply here.",
    "Please ask a senior member of your team who is an admin to write in.",
    "Your team lead's approval isn't needed; any admin can write in.",
    "The refund is waiting for a final approval.",
])
def test_reply_guard_leaves_ordinary_replies_alone(text):
    assert policy.internal_terms(text) == []


def test_a_bare_500_is_allowed_only_as_the_refunded_amount():
    reply = "I've refunded $500.00 to your card."
    assert policy.internal_terms(reply) == [policy.LIMIT_LABEL]
    assert policy.internal_terms(reply, [50_000]) == []
    assert policy.internal_terms(reply, [19_000]) == [policy.LIMIT_LABEL]
    assert policy.internal_terms("Refunds above $500 need approval.", [50_000]) == [policy.LIMIT_LABEL]


def test_the_model_never_reads_the_limit_in_a_tool_result():
    e = check(acct([ch("m", 1, amount=120.0, currency="EUR")], currency="EUR"), "m")
    assert policy.internal_terms(e.detail) == [] and "limit" not in e.detail.lower()
    over = check(acct([ch("m", 1, amount=700.0)]), "m")
    assert policy.internal_terms(over.detail) == [] and "500" not in over.detail


def test_seats_after_30_days_give_the_faq_fact_not_the_renewal_line():
    """s02 (second-run audit): the renewal line fits an annual plan charge; for seats the Billing FAQ's fact does."""
    from rta.config import KB_DIR

    faq = (KB_DIR / "billing-faq.md").read_text()
    assert "Removed seats and plan downgrades take effect at the next renewal." in faq
    seats_out = check(acct([seats(31)], billing="annual"), "s")
    assert seats_out.detail.endswith(" They can no longer be refunded. " + policy.SEATS_REMOVED)
    assert "auto-renewal" not in seats_out.detail
    annual = ch("a", 31, type="annual_subscription", billing="annual", amount=420.0)
    annual_out = check(acct([annual], billing="annual"), "a")
    assert "auto-renewal" in annual_out.detail and policy.SEATS_REMOVED not in annual_out.detail


def test_escalation_details_keep_the_routing_internal():
    """Every escalation detail ends by saying the routing is internal and the reply decides nothing; the EUR detail no
    longer says the policy allows the refund (second-run audit, k01 and the escalation details)."""
    eur = check(acct([ch("m", 1, amount=120.0, currency="EUR")], currency="EUR"), "m")
    chargeback = check(acct([ch("m", 1)], chargebacks=[{"id": "cb", "customer_id": "c1", "charge_id": "m",
                                                         "opened_on": "2026-11-01", "status": "open"}]), "m")
    monthly_seats = check(acct([seats(1, billing="monthly", amount=35.47)]), "s")
    early = policy.check_charge(acct([ch("m", 0) | {"date": "2026-02-25"}]), "m", SENDER, date(2026, 2, 27))
    for e in (eur, chargeback, monthly_seats, early):
        assert e.expected_decision == policy.ESCALATED and e.detail.endswith(policy.ESCALATION_NOTE), e.reason
    assert "allows" not in eur.detail
    # an eligible charge's detail says what to do when the customer asked for another amount, without a policy rule
    for e in (check(acct([ch("m", 1, amount=190.0)]), "m"), check(acct([ch("m", 1, amount=700.0)]), "m")):
        assert e.eligible and e.detail.endswith(policy.PARTIAL_NOTE)
        assert not any(rule in e.detail.lower() for rule in ("only the full", "full amount only", "full charge only"))


def test_fixture_rules_for_a_real_ticket(billing):
    e = policy.check_charge(billing, "ch_1092", "theo@harborpine.example", date(2026, 11, 4))
    assert (e.reason, e.route) == (policy.OPEN_CHARGEBACK, policy.RISK)
