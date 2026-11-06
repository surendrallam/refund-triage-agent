"""The agent's tools: small, strict JSON schemas, and the code that runs them.

Every rule is enforced here, whatever the model asks: lookups stay inside the sender's own account and say
nothing about other accounts, issue_refund and send_for_approval call the policy again and refuse anything it
doesn't allow (including a charge other than the one the email names), and at most one refund or approval can
happen per ticket. Executed actions land in a Sandbox: a refund log and an approval queue for this one ticket,
with the ticket number and the reason on every entry. No real money moves, and nothing is written back to the
billing data.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rta import policy
from rta.billing import Billing, fmt_money, money, normalise_email, to_cents
from rta.kb import KB

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_INVOICE = re.compile(r"\bFW[-\s]?(\d{5})\b", re.I)


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TOOLS: list[dict] = [
    {
        "name": "find_customer",
        "description": (
            "Look up the Fernway account that an email address belongs to, as its billing contact or an admin. "
            "Call it first, with the sender's address from the ticket details. It returns the account only for "
            "the sender's own address; for any other address it returns nothing, because you may act only for "
            "the account whose billing contact or admin wrote in."
        ),
        "strict": True,
        "input_schema": _schema({
            "email": {"type": "string", "description": "The address to look up, normally the sender's."},
        }),
    },
    {
        "name": "list_charges",
        "description": (
            "List the charges on the sender's account up to today: charge id, invoice number, date, type "
            "(monthly_subscription, annual_subscription or seat_addition), what was charged, amount, currency "
            "and status (paid, failed or refunded). Use it to find the charge the customer means, by invoice "
            "number when the email gives one. Works only for the sender's own account."
        ),
        "strict": True,
        "input_schema": _schema({
            "customer_id": {"type": "string", "description": "The customer_id that find_customer returned."},
        }),
    },
    {
        "name": "check_refund_eligibility",
        "description": (
            "Apply Fernway's refund rules to one charge, as of today. Call it for the charge the customer asks "
            "about before you refund, send for approval or decline anything. The rules are code; follow the "
            "answer. It returns whether the charge is eligible, a reason code, the only amount that may be "
            "refunded (the full charge), whether a person must approve it and in which queue, the decision the "
            "ticket should end with, and the policy facts that explain it."
        ),
        "strict": True,
        "input_schema": _schema({
            "charge_id": {"type": "string", "description": "A charge_id from list_charges."},
        }),
    },
    {
        "name": "search_policy",
        "description": (
            "Search Fernway's public help documents: the Refund policy (2026), the Billing FAQ and Plans and "
            "pricing (2026). Use it to answer billing questions and to check the policy wording for your reply. "
            "Returns up to 3 passages."
        ),
        "strict": True,
        "input_schema": _schema({
            "query": {"type": "string",
                      "description": "Keywords, for example 'annual refund auto-renewal' or 'failed payment retry'."},
        }),
    },
    {
        "name": "issue_refund",
        "description": (
            "Refund one charge in full to the original payment method. This is a sandbox and no real money "
            "moves, but treat it as real. Call it only when check_refund_eligibility returned expected_decision "
            "refund_issued for this charge, with exactly its refundable_amount. The code checks every rule "
            "again and refuses anything else."
        ),
        "strict": True,
        "input_schema": _schema({
            "charge_id": {"type": "string", "description": "The charge to refund."},
            "amount": {"type": "number",
                       "description": "The refundable_amount from check_refund_eligibility, in the charge's currency."},
            "reason": {"type": "string",
                       "description": "One line for the refund log, for example 'Monthly charge refunded within 7 days at the customer's request'."},
        }),
    },
    {
        "name": "send_for_approval",
        "description": (
            "Send a refund that the rules allow but that a person must approve to the approval queue. Call it "
            "only when check_refund_eligibility returned needs_approval true for this charge, with its "
            "refundable_amount and approval_queue. No money moves until a person approves it."
        ),
        "strict": True,
        "input_schema": _schema({
            "charge_id": {"type": "string", "description": "The charge to refund."},
            "amount": {"type": "number", "description": "The refundable_amount from check_refund_eligibility."},
            "queue": {"type": "string", "enum": [policy.BILLING_LEAD],
                      "description": "The approval_queue from check_refund_eligibility."},
            "summary": {"type": "string",
                        "description": "Two or three lines for the approver: what the customer asked and why the rules allow it."},
        }),
    },
    {
        "name": "finish",
        "description": (
            "End the ticket. Call it exactly once, as your last action, after the other tools have done their "
            "work. The code checks that the decision matches what the tools did; if it doesn't, the ticket goes "
            "to a person instead and your reply is not sent."
        ),
        "strict": True,
        "input_schema": _schema({
            "decision": {"type": "string", "enum": list(policy.DECISIONS),
                         "description": "How the ticket ends; see the system prompt for when to use each."},
            "charge_id": {**_NULLABLE_STRING,
                          "description": "The charge_id from list_charges that the decision is about (for example "
                                         "ch_1013, not the invoice number), or null when there is none."},
            "amount": {**_NULLABLE_NUMBER, "description": "The amount refunded or sent for approval, or null."},
            "reply_to_customer": {"type": "string", "description": "The email reply to the customer, ready to send."},
            "internal_note": {"type": "string", "description": "One or two lines for staff: the facts and the reason code."},
        }),
    },
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
SIDE_EFFECT_TOOLS = ("issue_refund", "send_for_approval")


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    sender: str
    subject: str
    body: str
    today: date


def named_invoices(ticket: Ticket) -> set[str]:
    """Invoice numbers the email names in its subject or body, normalised to FW-12345."""
    return {f"FW-{n}" for n in _INVOICE.findall(f"{ticket.subject}\n{ticket.body}")}


@dataclass(frozen=True)
class Action:
    """One executed action: a refund in the refund log, or a request in the approval queue."""

    id: str
    kind: str  # refund | approval
    charge_id: str
    customer_id: str
    invoice: str
    amount_cents: int
    currency: str
    queue: str | None
    note: str  # refund reason or approval summary
    ticket_id: str
    on: date

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "charge_id": self.charge_id, "customer_id": self.customer_id,
                "invoice": self.invoice, "amount": money(self.amount_cents), "currency": self.currency,
                "queue": self.queue, "note": self.note, "ticket_id": self.ticket_id, "date": self.on.isoformat()}


@dataclass
class Sandbox:
    actions: list[Action] = field(default_factory=list)

    @property
    def refunds(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "refund"]

    @property
    def approvals(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "approval"]


@dataclass
class ToolOutcome:
    content: dict
    is_error: bool = False
    refused: bool = False  # issue_refund or send_for_approval refused: a refused attempt
    blocked: bool = False  # a lookup outside the sender's own account
    code: str | None = None


class ToolInputError(ValueError):
    pass


_ARGS = {
    "find_customer": ("email",),
    "list_charges": ("customer_id",),
    "check_refund_eligibility": ("charge_id",),
    "search_policy": ("query",),
    "issue_refund": ("charge_id", "amount", "reason"),
    "send_for_approval": ("charge_id", "amount", "queue", "summary"),
    "finish": ("decision", "charge_id", "amount", "reply_to_customer", "internal_note"),
}


def _check_keys(name: str, args: Any) -> dict:
    if not isinstance(args, dict):
        raise ToolInputError(f"{name} takes a JSON object with {', '.join(_ARGS[name])}")
    unknown = sorted(set(args) - set(_ARGS[name]))
    if unknown:
        raise ToolInputError(f"unknown parameter(s) {', '.join(unknown)}; {name} takes {', '.join(_ARGS[name])}")
    return args


def _text(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ToolInputError(f"{key} must be a non-empty string")
    return v.strip()


def _cents(args: dict, key: str) -> int:
    v = args.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ToolInputError(f"{key} must be a number")
    try:
        return to_cents(v)
    except ValueError as e:
        raise ToolInputError(f"{key} must be a number") from e


def validate_finish(args: Any) -> dict:
    """The finish call's arguments, checked and normalised. Raises ToolInputError."""
    args = _check_keys("finish", args)
    decision = args.get("decision")
    if decision not in policy.DECISIONS:
        raise ToolInputError(f"decision must be one of {', '.join(policy.DECISIONS)}")
    charge_id = args.get("charge_id")
    if charge_id is not None and not isinstance(charge_id, str):
        raise ToolInputError("charge_id must be a string or null")
    amount = args.get("amount")
    amount_cents = None if amount is None else _cents(args, "amount")
    reply = args.get("reply_to_customer")
    if not isinstance(reply, str) or not reply.strip():
        raise ToolInputError("reply_to_customer must be a non-empty string")
    note = args.get("internal_note")
    if note is not None and not isinstance(note, str):
        raise ToolInputError("internal_note must be a string")
    return {"decision": decision, "charge_id": (charge_id or "").strip() or None, "amount_cents": amount_cents,
            "reply": reply.strip(), "internal_note": (note or "").strip()}


class Toolbox:
    """Runs tool calls for one ticket. Bound to the ticket's verified sender and date, not to what the model says."""

    def __init__(self, billing: Billing, kb: KB, ticket: Ticket, sandbox: Sandbox | None = None) -> None:
        self.billing, self.kb, self.ticket = billing, kb, ticket
        self.sandbox = sandbox if sandbox is not None else Sandbox()
        self.sender = normalise_email(ticket.sender)
        self.today = ticket.today

    @staticmethod
    def resolve(name: str) -> str | None:
        """Exact tool name, or a case-only variant of one (the model occasionally changes the case)."""
        if name in TOOL_NAMES:
            return name
        lowered = {n.lower(): n for n in TOOL_NAMES}
        return lowered.get(str(name).lower())

    def charge_ref(self, ref: str | None) -> str | None:
        """A charge id as given, or an invoice number on the sender's own account mapped to its charge id.

        Models sometimes pass the invoice number (FW-10013) where the charge id (ch_1013) belongs. Only invoices
        on the sender's account are mapped, so this reveals nothing about other accounts.
        """
        if ref is None:
            return None
        ref = ref.strip()
        m = _INVOICE.fullmatch(ref)
        customer = self.billing.customer_for_email(self.sender) if m else None
        if customer is None:
            return ref
        invoice = f"FW-{m.group(1)}"
        return next((c.id for c in self.billing.charges_for(customer.id, self.today) if c.invoice == invoice), ref)

    def run(self, name: str, args: Any) -> ToolOutcome:
        resolved = self.resolve(name)
        if resolved is None:
            return ToolOutcome({"error": "unknown_tool", "message": f"No tool named {name!r}. Tools: "
                                f"{', '.join(TOOL_NAMES)}."}, is_error=True, code="unknown_tool")
        if resolved == "finish":
            return ToolOutcome({"error": "finish_handled_by_agent"}, is_error=True, code="finish_handled_by_agent")
        try:
            return getattr(self, "_" + resolved)(_check_keys(resolved, args))
        except ToolInputError as e:
            return ToolOutcome({"error": "invalid_input", "message": str(e)}, is_error=True, code="invalid_input")

    # ------------------------------------------------------------------------------------------- lookups

    def _find_customer(self, args: dict) -> ToolOutcome:
        email = normalise_email(_text(args, "email"))
        if email != self.sender:
            # The same answer whether or not an account exists, so the lookup can't be used to probe for accounts.
            return ToolOutcome({"is_sender": False,
                                "note": "This is not the sender's address. You may act only for the account whose "
                                        "billing contact or admin sent the email, so nothing is shown for any "
                                        "other address."},
                               blocked=True, code="not_sender")
        customer = self.billing.customer_for_email(self.sender)
        if customer is None:
            return ToolOutcome({"found": False, "is_sender": True,
                                "note": "No Fernway account has this address as its billing contact or an admin."})
        return ToolOutcome({
            "found": True, "is_sender": True, "customer_id": customer.id, "workspace": customer.workspace,
            "sender_role": customer.role_of(self.sender), "plan": customer.plan, "billing": customer.billing,
            "seats": customer.seats, "currency": customer.currency,
        })

    def _list_charges(self, args: dict) -> ToolOutcome:
        customer_id = _text(args, "customer_id")
        customer = self.billing.customer(customer_id)
        if customer is None or not customer.authorised(self.sender):
            # One answer for an unknown id and for another account's id; the internal code tells them apart.
            return ToolOutcome({"error": "not_senders_account",
                                "message": "This customer_id is not the sender's account, so no charges can be "
                                           "shown. Use the customer_id that find_customer returned for the "
                                           "sender's own address."},
                               is_error=True, blocked=customer is not None,
                               code="unknown_customer" if customer is None else policy.IDENTITY_MISMATCH)
        charges = []
        for c in self.billing.charges_for(customer.id, self.today):
            row = {"charge_id": c.id, "invoice": c.invoice, "date": c.date.isoformat(), "type": c.type,
                   "description": c.describe(), "amount": money(c.amount_cents), "currency": c.currency,
                   "status": c.status_on(self.today)}
            if row["status"] == "refunded" and c.refunded_on:
                row["refunded_on"] = c.refunded_on.isoformat()
            charges.append(row)
        return ToolOutcome({"customer_id": customer.id, "today": self.today.isoformat(), "charges": charges})

    def _check_refund_eligibility(self, args: dict) -> ToolOutcome:
        e = policy.check_charge(self.billing, self.charge_ref(_text(args, "charge_id")), self.sender, self.today)
        blocked = e.reason == policy.IDENTITY_MISMATCH
        code = e.reason if e.reason in (policy.IDENTITY_MISMATCH, policy.UNKNOWN_CHARGE) else None
        return ToolOutcome(e.for_agent(), blocked=blocked, code=code)

    def _search_policy(self, args: dict) -> ToolOutcome:
        hits = self.kb.search(_text(args, "query"), k=3)
        if not hits:
            return ToolOutcome({"results": [], "note": "No passage matched these keywords."})
        return ToolOutcome({"results": [p.as_result() for p, _ in hits]})

    # ------------------------------------------------------------------------------------------- actions

    @staticmethod
    def _refuse(code: str, message: str, shown_code: str | None = None, **extra: Any) -> ToolOutcome:
        """A refused attempt. `code` goes in the trace; the model sees `shown_code` when one is given."""
        return ToolOutcome({"refused": True, "code": shown_code or code, "message": message, **extra},
                           is_error=True, refused=True, code=code)

    def _guard(self, charge_id: str, want: str) -> tuple[policy.Eligibility, ToolOutcome | None]:
        """Shared checks for both actions. Returns the rules' answer and a refusal, if any."""
        e = policy.check_charge(self.billing, charge_id, self.sender, self.today)
        if self.sandbox.actions:
            return e, self._refuse("one_action_per_ticket",
                                   "A refund or approval has already been made on this ticket. One ticket handles "
                                   "one charge; several charges go to a person (decision escalated).")
        if e.reason in (policy.UNKNOWN_CHARGE, policy.IDENTITY_MISMATCH):
            return e, self._refuse(e.reason, policy.NOT_ON_SENDERS_ACCOUNT_DETAIL,
                                   shown_code=policy.NOT_ON_SENDERS_ACCOUNT)
        named = named_invoices(self.ticket)
        if named and e.invoice not in named:
            return e, self._refuse("invoice_not_named",
                                   f"The email names {', '.join(sorted(named))}, and this charge is {e.invoice}. Act "
                                   "only on a charge the email names. If the customer may mean another charge, ask "
                                   "which one (decision needs_details).")
        if e.expected_decision != want:
            if want == policy.REFUND_ISSUED and e.needs_approval:
                return e, self._refuse("needs_approval",
                                       "This refund needs a person's approval; use send_for_approval.",
                                       expected_decision=e.expected_decision)
            if want == policy.SENT_FOR_APPROVAL and e.can_issue:
                return e, self._refuse("approval_not_needed",
                                       "This refund doesn't need approval; the rules allow issue_refund.",
                                       expected_decision=e.expected_decision)
            return e, self._refuse(e.reason, "The rules don't allow this: " + e.detail,
                                   expected_decision=e.expected_decision)
        return e, None

    def _issue_refund(self, args: dict) -> ToolOutcome:
        charge_id = self.charge_ref(_text(args, "charge_id"))
        amount, reason = _cents(args, "amount"), _text(args, "reason")
        e, refusal = self._guard(charge_id, policy.REFUND_ISSUED)
        if refusal is not None:
            return refusal
        if amount != e.amount_cents:
            # Worded as the tool's own rule and internal routing, not as policy the reply could repeat (k01 in the
            # second run stated "we can only refund a full charge" as Fernway policy).
            return self._refuse("amount_mismatch",
                                f"This tool refunds whole charges only ({fmt_money(e.amount_cents, e.currency)}); "
                                f"{fmt_money(amount, e.currency)} was asked for. A different or partial amount goes "
                                f"to a person (decision escalated). {policy.ESCALATION_NOTE}")
        action = self._record("refund", e, amount, None, reason)
        return ToolOutcome({"refunded": True, "refund_id": action.id, "charge_id": charge_id, "invoice": e.invoice,
                            "amount": money(amount), "currency": e.currency, "payout": policy.PAYOUT,
                            "logged_with": {"ticket": self.ticket.ticket_id, "reason": reason}})

    def _send_for_approval(self, args: dict) -> ToolOutcome:
        charge_id, amount = self.charge_ref(_text(args, "charge_id")), _cents(args, "amount")
        queue, summary = _text(args, "queue"), _text(args, "summary")
        e, refusal = self._guard(charge_id, policy.SENT_FOR_APPROVAL)
        if refusal is not None:
            return refusal
        if queue != e.route:
            return self._refuse("wrong_queue", f"This approval goes to the {e.route} queue.")
        if amount != e.amount_cents:
            # The same wording as issue_refund's: the tool's own rule and internal routing (second-run audit).
            return self._refuse("amount_mismatch",
                                f"This tool sends whole charges only ({fmt_money(e.amount_cents, e.currency)}); "
                                f"{fmt_money(amount, e.currency)} was asked for. A different or partial amount goes "
                                f"to a person (decision escalated). {policy.ESCALATION_NOTE}")
        action = self._record("approval", e, amount, queue, summary)
        return ToolOutcome({"sent": True, "approval_id": action.id, "charge_id": charge_id, "invoice": e.invoice,
                            "amount": money(amount), "currency": e.currency, "queue": queue,
                            "status": "waiting for approval; no money has moved"})

    def _record(self, kind: str, e: policy.Eligibility, amount: int, queue: str | None, note: str) -> Action:
        n = len(self.sandbox.actions) + 1
        action = Action(f"{'rf' if kind == 'refund' else 'ap'}-{self.ticket.ticket_id}-{n}", kind, e.charge_id,
                        e.customer_id or "", e.invoice or "", amount, e.currency or "USD", queue, note,
                        self.ticket.ticket_id, self.today)
        self.sandbox.actions.append(action)
        return action


def timed_run(toolbox: Toolbox, name: str, args: Any) -> tuple[ToolOutcome, float]:
    started = time.perf_counter()
    outcome = toolbox.run(name, args)
    return outcome, (time.perf_counter() - started) * 1000
