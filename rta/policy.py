"""The refund rules, in code: identity, eligibility, amounts, limits and routing.

Deterministic and unit-tested at the day boundaries (tests/test_policy.py). The model never decides any of
this; it asks check_refund_eligibility and follows the answer, and issue_refund / send_for_approval call
check_charge again before they do anything.

Sources (fictional Fernway documents):
- Refund policy (2026), current since 1 March 2026 (kb/refund-policy-2026.md).
- Refund handling procedure (internal, staff only): support agents approve refunds up to $500, larger ones need
  the Billing Lead; accounts with an open chargeback go to the Risk team and get no refunds from support.
- Support escalation procedure (internal): Tier 2 handles billing exceptions.
The Refund policy (2025) was superseded on 1 March 2026 and is not implemented. Every place where the
documents are silent and a choice was made is listed in DECISIONS.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from rta.billing import Billing, Charge, fmt_money, money

# Decisions a ticket can end with.
REFUND_ISSUED = "refund_issued"
SENT_FOR_APPROVAL = "sent_for_approval"
DECLINED = "declined"
NEEDS_DETAILS = "needs_details"
ESCALATED = "escalated"
NOT_REFUND_REQUEST = "not_refund_request"
DECISIONS = (REFUND_ISSUED, SENT_FOR_APPROVAL, DECLINED, NEEDS_DETAILS, ESCALATED, NOT_REFUND_REQUEST)

# Internal queues (never named to customers).
BILLING_LEAD = "billing_lead"  # approves refunds above the agent limit
RISK = "risk"  # accounts with an open chargeback
TIER2 = "tier2"  # billing exceptions the policy doesn't cover
QUEUES = (BILLING_LEAD, RISK, TIER2)

POLICY_EFFECTIVE = date(2026, 3, 1)
MONTHLY_WINDOW_DAYS = 7
ANNUAL_WINDOW_DAYS = 30
SEAT_WINDOW_DAYS = 30
AGENT_LIMIT_CENTS = 50_000  # $500.00, inclusive
LIMIT_CURRENCY = "USD"
PAYOUT = "Refunds go back to the original payment method within 5 to 10 business days."
# Escalation routing is internal (second-run audit): the model-facing detail says what to do and what the reply may
# say, so the reply doesn't state the rule that sent the ticket to a person or decide the request.
ESCALATION_NOTE = ("This routing is internal: in the reply, say only that a colleague will review the request and "
                   "reply, not why it went to a person or whether it can be granted.")
# The eligible details give the full amount to refund. A customer who asked for another amount is escalated, and
# the reply mustn't present the full-charge rule as policy (k01 in the second run).
PARTIAL_NOTE = ("If the customer asked for a different or partial amount, don't act on this charge: escalate the "
                "ticket. A person decides that request; in the reply, say only that a colleague will review it "
                "and reply.")
# The Billing FAQ ("Changing seats or plans"): the documented fact for a customer who wants to drop seats.
SEATS_REMOVED = "Removed seats take effect at the next renewal."

# Reason codes returned by check_charge.
UNKNOWN_CHARGE = "unknown_charge"
IDENTITY_MISMATCH = "identity_mismatch"
# What the model is told for both of the above, so it can't tell another account's charge from one that doesn't
# exist (the internal reason stays in the trace).
NOT_ON_SENDERS_ACCOUNT = "not_on_senders_account"
NOT_ON_SENDERS_ACCOUNT_DETAIL = (
    "There is no charge with this id on the sender's account. Use a charge_id from list_charges. The sender can act "
    "only on the account whose billing contact or admin they are; for any other account, ask that account's billing "
    "contact to write in from their own address (decision needs_details).")
BEFORE_CURRENT_POLICY = "request_before_current_policy"
OPEN_CHARGEBACK = "open_chargeback"
CHARGE_FAILED = "charge_failed"
ALREADY_REFUNDED = "already_refunded"
NOT_MOST_RECENT = "not_most_recent_monthly_charge"
MONTHLY_IN = "monthly_within_7_days"
MONTHLY_OUT = "monthly_after_7_days"
ANNUAL_IN = "annual_within_30_days"
ANNUAL_OUT = "annual_after_30_days"
SEATS_IN = "seats_within_30_days"
SEATS_OUT = "seats_after_30_days"
SEATS_ON_MONTHLY = "seats_on_monthly_plan"
LIMIT_CURRENCY_GAP = "non_usd_charge_limit_unclear"


@dataclass(frozen=True)
class Eligibility:
    """What the rules say about refunding one charge, for one sender, on one date."""

    charge_id: str
    reason: str
    eligible: bool  # the refund policy allows a full refund of this charge
    expected_decision: str  # how the ticket should end, as far as this charge goes
    route: str | None = None  # billing_lead for approvals; risk or tier2 for escalations
    amount_cents: int | None = None  # the full amount of the charge: the only amount that may be refunded
    currency: str | None = None
    customer_id: str | None = None
    invoice: str | None = None
    days_since: int | None = None
    window_days: int | None = None
    detail: str = ""

    @property
    def can_issue(self) -> bool:
        return self.expected_decision == REFUND_ISSUED

    @property
    def needs_approval(self) -> bool:
        return self.expected_decision == SENT_FOR_APPROVAL

    def for_agent(self) -> dict:
        """The tool result the model sees. Another account's charge and a charge that doesn't exist get the same
        answer, so nothing about other accounts can be inferred from it."""
        if self.reason in (UNKNOWN_CHARGE, IDENTITY_MISMATCH):
            return {"charge_id": self.charge_id, "eligible": False, "reason": NOT_ON_SENDERS_ACCOUNT,
                    "expected_decision": NEEDS_DETAILS, "detail": NOT_ON_SENDERS_ACCOUNT_DETAIL}
        return {
            "charge_id": self.charge_id,
            "invoice": self.invoice,
            "eligible": self.eligible,
            "reason": self.reason,
            "refundable_amount": money(self.amount_cents) if self.eligible else None,
            "currency": self.currency,
            "needs_approval": self.needs_approval,
            "approval_queue": self.route if self.needs_approval else None,
            "expected_decision": self.expected_decision,
            "days_since": self.days_since,
            "window_days": self.window_days,
            "detail": self.detail,
        }


def escalation_route(billing: Billing, sender: str, today: date) -> str:
    """Escalations go to Risk when the sender's account has an open chargeback, otherwise to Tier 2."""
    customer = billing.customer_for_email(sender)
    if customer is not None and billing.open_chargebacks(customer.id, today):
        return RISK
    return TIER2


def _iso(d: date) -> str:
    return d.isoformat()


def _ago(days: int) -> str:
    return "today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"


def last_day_in_window(start: date, window_days: int) -> date:
    """The last request date inside a window of N days that starts on `start`: day N is inside (DECISIONS.md, gap 1),
    so a monthly charge on 29 October can be refunded through 5 November."""
    return start + timedelta(days=window_days)


def _window_fact(start: date, days: int, window: int) -> str:
    """The window's dates, worked out in code so the model never does the date arithmetic: for a charge inside the
    window its last day, for one outside it the day it ended (its last day)."""
    last = _iso(last_day_in_window(start, window))
    if days > window:
        return f"The {window}-day window ended on {last}, its last day, so this is outside the {window}-day window."
    return f"This is inside the {window}-day window; its last day is {last}."


def check_charge(billing: Billing, charge_id: str, sender: str, today: date) -> Eligibility:
    """Apply the rules to one charge. The order of the checks is part of the policy (see DECISIONS.md)."""
    charge = billing.charge(charge_id, today)
    if charge is None:
        return Eligibility(charge_id, UNKNOWN_CHARGE, False, NEEDS_DETAILS,
                           detail=f"No charge with this id exists on or before {_iso(today)}.")
    customer = billing.customer(charge.customer_id)
    if customer is None or not customer.authorised(sender):
        return Eligibility(charge_id, IDENTITY_MISMATCH, False, NEEDS_DETAILS,
                           detail=("The sender is not the billing contact or an admin of the account this charge "
                                   "belongs to. Only that account's billing contact or an admin can ask for a "
                                   "refund on it."))
    base = dict(customer_id=customer.id, invoice=charge.invoice, currency=charge.currency)
    if today < POLICY_EFFECTIVE:
        return Eligibility(charge_id, BEFORE_CURRENT_POLICY, False, ESCALATED, TIER2, **base,
                           detail="The request date is before 1 March 2026, when the current refund policy took "
                                  "effect. Only the current policy is implemented, so a person handles the ticket: "
                                  "escalate it. " + ESCALATION_NOTE)
    open_cbs = billing.open_chargebacks(customer.id, today)
    if open_cbs:
        return Eligibility(charge_id, OPEN_CHARGEBACK, False, ESCALATED, RISK, **base,
                           detail=f"The account has an open chargeback (opened {_iso(open_cbs[0].opened_on)}). "
                                  "No refund is issued on an account with an open chargeback; a person handles "
                                  "the ticket, so escalate it. " + ESCALATION_NOTE)
    status = charge.status_on(today)
    if status == "failed":
        return Eligibility(charge_id, CHARGE_FAILED, False, DECLINED, **base,
                           detail=f"This charge ({charge.invoice}, {_iso(charge.date)}) failed, so no money was "
                                  "taken and there is nothing to refund. A failed card payment is retried 3 times "
                                  "over 7 days, on days 1, 3 and 7.")
    if status == "refunded":
        return Eligibility(charge_id, ALREADY_REFUNDED, False, DECLINED, **base,
                           detail=f"This charge ({charge.invoice}) was already refunded on "
                                  f"{_iso(charge.refunded_on)}. {PAYOUT}")

    days = (today - charge.date).days
    if charge.type == "monthly_subscription":
        window = MONTHLY_WINDOW_DAYS
        latest = billing.latest_monthly_charge(customer.id, today)
        if latest is not None and latest.id != charge.id:
            return Eligibility(charge_id, NOT_MOST_RECENT, False, DECLINED, **base, days_since=days,
                               window_days=window,
                               detail=f"Only the most recent monthly charge can be refunded, within {window} days "
                                      f"of that charge. The most recent monthly charge is {latest.invoice} on "
                                      f"{_iso(latest.date)}; this charge ({charge.invoice}, {_iso(charge.date)}) "
                                      "is older. This answer is about this charge only.")
        fact = (f"On monthly plans, the most recent monthly charge can be refunded if the customer asks within "
                f"{window} days of that charge. This charge ({charge.invoice}, {_iso(charge.date)}) was made "
                f"{_ago(days)}. " + _window_fact(charge.date, days, window))
        if days > window:
            return Eligibility(charge_id, MONTHLY_OUT, False, DECLINED, **base, days_since=days, window_days=window,
                               detail=fact)
        return _eligible(charge, MONTHLY_IN, days, window, fact, base)

    if charge.type == "annual_subscription":
        window = ANNUAL_WINDOW_DAYS
        fact = (f"Annual plans can be refunded in full within {window} days of the start of the annual term. "
                f"This term started on {_iso(charge.date)} ({charge.invoice}), {_ago(days)}. "
                + _window_fact(charge.date, days, window))
        if days > window:
            return Eligibility(charge_id, ANNUAL_OUT, False, DECLINED, **base, days_since=days, window_days=window,
                               detail=fact + " The plan is no longer refundable. The customer can turn off "
                                             "auto-renewal at any time.")
        return _eligible(charge, ANNUAL_IN, days, window, fact, base)

    if charge.type == "seat_addition":
        window = SEAT_WINDOW_DAYS
        if charge.billing != "annual":
            return Eligibility(charge_id, SEATS_ON_MONTHLY, False, ESCALATED, TIER2, **base, days_since=days,
                               detail="These seats were added to a monthly plan, and the refund policy's seat rule "
                                      "is about seats added to annual plans, so a person decides: escalate the "
                                      "ticket. " + ESCALATION_NOTE)
        fact = (f"Seats added to an annual plan can be refunded within {window} days of being added. These "
                f"{charge.seats_added} seats were added on {_iso(charge.date)} ({charge.invoice}), {_ago(days)}. "
                + _window_fact(charge.date, days, window))
        if days > window:
            return Eligibility(charge_id, SEATS_OUT, False, DECLINED, **base, days_since=days, window_days=window,
                               detail=fact + f" They can no longer be refunded. {SEATS_REMOVED}")
        return _eligible(charge, SEATS_IN, days, window, fact, base)

    raise ValueError(f"{charge.id}: unknown charge type {charge.type!r}")


def _eligible(charge: Charge, reason: str, days: int, window: int, fact: str, base: dict) -> Eligibility:
    amount = fmt_money(charge.amount_cents, charge.currency)
    common = dict(amount_cents=charge.amount_cents, days_since=days, window_days=window, **base)
    if charge.currency != LIMIT_CURRENCY:
        # The agent limit is set in USD and the documents give no conversion rule (DECISIONS.md, gap 6). The
        # model-facing detail says only that a person decides, so it has no limit wording to repeat, and (second-run
        # audit) it no longer says the policy allows the refund, which the replies had passed on as a decision.
        return Eligibility(charge.id, LIMIT_CURRENCY_GAP, True, ESCALATED, TIER2, **common,
                           detail=fact + f" Refunds of charges in {charge.currency} are decided by a person, so "
                                         f"escalate the ticket. {ESCALATION_NOTE}")
    if charge.amount_cents <= AGENT_LIMIT_CENTS:
        return Eligibility(charge.id, reason, True, REFUND_ISSUED, None, **common,
                           detail=fact + f" The full amount, {amount}, can be refunded now. {PAYOUT} "
                                         f"{PARTIAL_NOTE}")
    return Eligibility(charge.id, reason, True, SENT_FOR_APPROVAL, BILLING_LEAD, **common,
                       detail=fact + f" The full amount, {amount}, can be refunded once a person approves it. "
                                     f"{PAYOUT} {PARTIAL_NOTE}")


# ---------------------------------------------------------------------------------------------- reply guard

# Internal roles, queues and limits that must never reach a customer (refund handling and escalation procedures).
# The patterns cover the names themselves and the paraphrases listed in tests/test_policy.py ("to Risk", "Tier II",
# "second-tier support", "my refund limit", "over 500", "a manager needs to sign off", "larger refunds", "our fraud
# team", "level 2 support", "an open chargeback on your account", "the chargeback on your October invoice"). A
# wording outside those can still get through.
LIMIT_LABEL = "the $500 limit"
_500 = r"(?<![\d,.])500(?:\.00)?(?!\d|[.,]\d)"  # 500 or 500.00, not part of 1,500 or 500.50
_USD = r"(?:usd\s?|us\$\s?|\$\s?)?"
_LIMIT_IN_CONTEXT = (  # the limit named as a limit: always internal
    r"\b(?:over|above|under|below|beyond|exceeds?|exceeding|more than|less than|up to|at most|no more than"
    rf"|limit of|threshold of|cap of)\s+(?:the\s+)?{_USD}{_500}"
    rf"|{_USD}{_500}\s?(?:usd|us\s?dollars|dollars)?\s+(?:limit|threshold|cap|or (?:less|under|below|more|above|over)"
    r"|and (?:under|below|over|above))\b"
)
_LIMIT_BARE = (  # a bare $500: internal, unless it is the amount the agent actually refunded
    rf"\$\s?{_500}|{_500}\s?(?:usd|us\s?dollars|dollars)\b|\busd\s?{_500}|\bfive hundred (?:us )?dollars\b"
)
_ROLE = r"(?:team[\s-]?lead|line\s+manager|finance\s+team|senior\s+(?:team\s+)?members?)"
_NEAR = r"(?:[^.;!?]|\.\d){0,40}"  # within the sentence; the point in "$700.00" doesn't end it
INTERNAL_TERMS: tuple[tuple[str, str], ...] = (
    ("Billing Lead", r"billing[\s_-]*(?:team[\s_-]*)?lead"),
    ("Risk team or queue", r"\brisk[\s_-]*(?:team|queue|department|group|colleagues?|specialists?)\b"
                           r"|\b(?:to|with|by)\s+(?:our\s+|the\s+)?(?-i:Risk)\b"),
    ("Tier 2", r"\btier[\s_-]*(?:2|two|ii)\b|\btier2\b|\bsecond[\s-]tier\b"
               r"|\bsecond[\s-]line\s+(?:support|team|agents?|staff)\b"),
    ("goodwill", r"\bgood[\s-]?will\b"),
    (LIMIT_LABEL, _LIMIT_IN_CONTEXT),
    ("an approval limit", r"\b(?:refund|approval|agent|support)\s+(?:limit|threshold|cap)s?\b"
                          r"|\b(?:my|our)\s+(?:refund\s+|approval\s+)?(?:limit|threshold)s?\b"
                          r"|\bthe\s+(?:approval\s+)?threshold\b"
                          # paraphrases (6 Nov audit): "exceeds what I can approve", "larger refunds"
                          r"|\b(?:exceeds?|above|beyond|over)\b.{0,30}"
                          r"\b(?:what\s+(?:i|we)\s+can\s+approve|my\s+authority|our\s+authority)"
                          r"|\brefunds?\s+(?:above|over|of\s+more\s+than)\s+a\s+certain\s+amount\b"
                          r"|\blarger\s+refunds?\b"),
    # A manager's or senior colleague's sign-off on a refund (not the public "customer success manager").
    ("a manager's sign-off", r"(?<!success )\b(?:manager|supervisor|senior\s+(?:colleague|agent|member|staff))\b.{0,40}"
                             r"\b(?:approv|sign[\s-]?off)"
                             r"|\b(?:approv|sign[\s-]?off).{0,40}(?<!success )\b(?:manager|supervisor"
                             r"|senior\s+(?:colleague|agent|member|staff))\b"),
    ("an internal team or queue", r"\b(?:approvals?|fraud|disputes?|chargebacks?|escalations?)\s+"
                                  r"(?:team|queue|department|desk)\b"
                                  r"|\b(?:level|line)[\s-]*(?:2|two|ii)\s+(?:support|team|agents?|staff)\b"),
    # The open-chargeback rule is staff-only, and so is naming the account's chargeback ("the chargeback on your
    # October invoice"); "raise a chargeback with your bank" and "the chargeback process" are not.
    ("the chargeback rule", r"\b(?:open|pending|active)\s+chargebacks?\b"
                            r"|\b(?:the|this|that|your)\s+chargeback\b(?!\s+(?:process|option|right)s?\b)"
                            r"|\bchargebacks?\s+(?:on|against|for)\s+(?:your|the|this|invoice)\b"),
    # Second-run audit: more paraphrases of the approval procedure and of the chargeback rule. A team lead, line
    # manager, finance team or senior member counts when it is our own or sits near approve or sign-off (not the
    # customer's: "your team lead"); a dispute counts only as a bank or card dispute, so the customer's own invoice
    # dispute on d01 ("your open dispute about the seat count") passes.
    ("an approval role or level", r"\b(?:my|our)\s+" + _ROLE + r"\b"
                                  r"|(?<!your )(?<!their )\b" + _ROLE + _NEAR + r"\b(?:approv|sign[\s-]?off)"
                                  r"|\b(?:approv|sign[\s-]?off)\w*\b" + _NEAR
                                  + r"(?<!your )(?<!their )\b" + _ROLE + r"\b"
                                  r"|\bsecond\s+(?:approval|sign[\s-]?off)\b"
                                  r"|\b(?:too\s+(?:large|big|high)|above\s+what|more\s+than\s+what|over\s+what)\b"
                                  r"(?:[^.;!?]|\.\d){0,30}\b(?:approve|sign[\s-]?off|authori[sz]e)"
                                  r"|\bapproval\s+(?:level|authority|tier)\b"
                                  r"|\b(?:refunds?|amounts?)\s+of\s+(?:this|that)\s+size\b"),
    ("a bank dispute", r"\b(?:open|pending|active|ongoing|existing)\s+(?:bank|card|payment)\s+disputes?\b"
                       r"|\b(?:open|pending|active|ongoing|existing)\s+disputes?\b(?:[^.;!?]|\.\d){0,40}"
                       r"\b(?:with|at|by|from|through)\s+(?:your|the)\s+(?:bank|card issuer|card provider)\b"
                       r"|\b(?:your|the)\s+(?:bank|card issuer|card provider)\s+(?:is|has been|has)\s+disput"),
)
_INTERNAL = [(label, re.compile(p, re.I)) for label, p in INTERNAL_TERMS]
_BARE = re.compile(_LIMIT_BARE, re.I)


def internal_terms(text: str | None, refunded_cents: Iterable[int] = ()) -> list[str]:
    """Labels of the internal terms found in a customer reply (empty when the reply is clean).

    refunded_cents: amounts the tools actually refunded or sent for approval on this ticket. A bare "$500.00" is
    then the amount of the customer's own charge, not the limit (the limit is inclusive, so the agent can issue
    exactly $500.00). The limit named as a limit ("above $500", "the $500 limit") is always internal.
    """
    t = text or ""
    allow_bare = AGENT_LIMIT_CENTS in set(refunded_cents)
    found = []
    for label, rx in _INTERNAL:
        if rx.search(t) or (label == LIMIT_LABEL and not allow_bare and _BARE.search(t)):
            found.append(label)
    return found
