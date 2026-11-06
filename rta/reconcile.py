"""After the loop: code reconciles what the model said with what the tools did, and audits the actions.

The model's finish call is a claim. It stands only if it matches the sandbox (the refund log and the approval
queue) and the rules; otherwise the ticket becomes an escalation to a person, the model's reply is not sent,
and the reasons are recorded as flags.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from rta import policy
from rta.billing import Billing, money
from rta.tools import SIDE_EFFECT_TOOLS, Sandbox, Ticket, named_invoices

# Tools a ticket that is "not a refund request" has no reason to call.
REFUND_TOOLS = frozenset({"check_refund_eligibility", *SIDE_EFFECT_TOOLS})

HOLDING_REPLY = ("Thank you for your message. A member of our billing team will look at your request and reply to "
                 "you here.\n\nFernway Support")


@dataclass(frozen=True)
class Stated:
    """What the model said in finish."""

    decision: str
    charge_id: str | None
    amount_cents: int | None
    reply: str
    internal_note: str = ""

    def as_dict(self) -> dict:
        return {"decision": self.decision, "charge_id": self.charge_id, "amount": money(self.amount_cents),
                "reply_to_customer": self.reply, "internal_note": self.internal_note}


@dataclass
class Final:
    """What the ticket ends with after reconciliation."""

    decision: str
    queue: str | None
    charge_id: str | None
    amount_cents: int | None
    reply: str
    flags: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.flags

    def as_dict(self) -> dict:
        return {"decision": self.decision, "queue": self.queue, "charge_id": self.charge_id,
                "amount": money(self.amount_cents), "reply": self.reply, "flags": list(self.flags),
                "consistent": self.consistent}


def _matches(action, stated: Stated) -> bool:
    return action.charge_id == stated.charge_id and action.amount_cents == stated.amount_cents


def _sender_and_invoices_verified(billing: Billing, ticket: Ticket) -> bool:
    """The verified sender is the billing contact or an admin of an account (the identity rule of rta/policy.py),
    the email names at least one invoice, and every invoice it names is a charge on that account dated on or before
    the ticket date."""
    customer = billing.customer_for_email(ticket.sender)
    named = named_invoices(ticket)
    if customer is None or not named:
        return False
    return named <= {c.invoice for c in billing.charges_for(customer.id, ticket.today)}


def reconcile(stated: Stated | None, sandbox: Sandbox, billing: Billing, ticket: Ticket,
              end_reason: str = "finished", tools_called: Iterable[str] = ()) -> Final:
    """tools_called: the names of every tool the model called on this ticket, refused calls included."""
    flags: list[str] = []
    refunds, approvals = sandbox.refunds, sandbox.approvals
    if stated is None:
        flags.append(f"no_finish:{end_reason}")
    else:
        d = stated.decision
        if d == policy.REFUND_ISSUED:
            if not refunds:
                flags.append("refund_claimed_but_not_issued")
            elif len(refunds) != 1 or approvals or not _matches(refunds[0], stated):
                flags.append("refund_differs_from_finish")
        elif d == policy.SENT_FOR_APPROVAL:
            if not approvals:
                flags.append("approval_claimed_but_not_sent")
            elif len(approvals) != 1 or refunds or not _matches(approvals[0], stated):
                flags.append("approval_differs_from_finish")
        else:
            if refunds or approvals:
                flags.append(f"action_taken_but_decision_{d}")
            if d == policy.DECLINED:
                # A decline must be backed by the rules for the charge it names.
                if not stated.charge_id:
                    flags.append("decline_names_no_charge")
                else:
                    e = policy.check_charge(billing, stated.charge_id, ticket.sender, ticket.today)
                    if e.expected_decision != policy.DECLINED:
                        flags.append(f"decline_not_backed_by_rules:{e.reason}")
            if d == policy.NEEDS_DETAILS:
                # Asking for details about a named charge must be backed by the rules too: the sender may not act
                # on it, or no such charge exists. An eligible charge of the sender's own is not "needs details".
                e = (policy.check_charge(billing, stated.charge_id, ticket.sender, ticket.today)
                     if stated.charge_id else None)
                if e is not None and e.expected_decision != policy.NEEDS_DETAILS:
                    flags.append(f"needs_details_not_backed_by_rules:{e.reason}")
                elif _sender_and_invoices_verified(billing, ticket):
                    # Rule 5b: with no charge named, or one the sender can't act on (a guessed id, another
                    # account's), asking for details is not backed either when code already knows who is asking
                    # and about what: the verified sender may act on their account, and every invoice the email
                    # names is a charge on it (6 Nov audit: a reply asked a verified billing contact to write in).
                    flags.append("needs_details_not_backed:sender_and_invoice_verified")
            if d == policy.NOT_REFUND_REQUEST and (stated.charge_id or REFUND_TOOLS & set(tools_called)):
                flags.append("not_refund_request_after_refund_tools")
            if d in (policy.DECLINED, policy.NEEDS_DETAILS):
                customer = billing.customer_for_email(ticket.sender)
                if customer is not None and billing.open_chargebacks(customer.id, ticket.today):
                    flags.append("open_chargeback_goes_to_a_person")
        # A bare $500.00 is allowed only as the amount the tools actually refunded (the limit is inclusive).
        leaks = policy.internal_terms(stated.reply, [a.amount_cents for a in sandbox.actions])
        if leaks:
            flags.append("internal_terms_in_reply:" + ",".join(leaks))

    if flags:
        return Final(policy.ESCALATED, policy.escalation_route(billing, ticket.sender, ticket.today),
                     stated.charge_id if stated else None, stated.amount_cents if stated else None,
                     HOLDING_REPLY, flags)
    assert stated is not None
    queue = None
    if stated.decision == policy.SENT_FOR_APPROVAL:
        queue = approvals[0].queue
    elif stated.decision == policy.ESCALATED:
        queue = policy.escalation_route(billing, ticket.sender, ticket.today)
    return Final(stated.decision, queue, stated.charge_id, stated.amount_cents, stated.reply)


def enforced_outcome(sandbox: Sandbox, final: Final) -> str:
    """What actually happened: an executed action wins over any decision."""
    if sandbox.refunds:
        return policy.REFUND_ISSUED
    if sandbox.approvals:
        return policy.SENT_FOR_APPROVAL
    return final.decision


def audit(sandbox: Sandbox, billing: Billing, ticket: Ticket) -> list[str]:
    """Independent re-check of every executed action against the rules. Must always be empty."""
    violations = []
    if len(sandbox.actions) > 1:
        violations.append("more_than_one_action")
    named = named_invoices(ticket)
    for a in sandbox.actions:
        e = policy.check_charge(billing, a.charge_id, ticket.sender, ticket.today)
        want = policy.REFUND_ISSUED if a.kind == "refund" else policy.SENT_FOR_APPROVAL
        if e.expected_decision != want:
            violations.append(f"{a.kind}:{a.charge_id}:not_allowed:{e.reason}")
        elif named and e.invoice not in named:
            violations.append(f"{a.kind}:{a.charge_id}:invoice_not_named")
        elif a.amount_cents != e.amount_cents:
            violations.append(f"{a.kind}:{a.charge_id}:wrong_amount")
        elif a.kind == "approval" and a.queue != e.route:
            violations.append(f"{a.kind}:{a.charge_id}:wrong_queue")
    return violations
