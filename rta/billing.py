"""The fictional billing system: customers, charges and chargebacks, read from data/billing.json.

Every lookup takes the request date ("today"): a charge dated after today doesn't exist yet, a refund counts
from its refunded_on date and a chargeback from its opened_on date. Money is kept in integer cents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from rta.config import BILLING_PATH

# Fernway list prices, USD per seat per month (kb/plans-and-pricing-2026.md). Annual billing is charged once a
# year, so an annual charge is seats x annual price x 12.
PLAN_PRICES: dict[str, dict[str, int]] = {
    "starter": {"monthly": 9, "annual": 7},
    "team": {"monthly": 19, "annual": 15},
    "business": {"monthly": 35, "annual": 29},
}
SEAT_LIMITS: dict[str, tuple[int, int | None]] = {"starter": (1, 5), "team": (3, 50), "business": (10, None)}
CHARGE_TYPES = ("monthly_subscription", "annual_subscription", "seat_addition")
STATUSES = ("paid", "failed", "refunded")
CURRENCIES = ("USD", "EUR", "GBP")
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def to_cents(value: Any) -> int:
    """Amount in cents from a number or a numeric string, rounded half up. Raises ValueError otherwise."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not an amount: {value!r}")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"not an amount: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"not an amount: {value!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100, 2)


def fmt_money(cents: int, currency: str = "USD") -> str:
    return f"{_SYMBOLS.get(currency, currency + ' ')}{cents / 100:,.2f}"


def normalise_email(raw: str | None) -> str:
    """'Ana <Ana@Example.example>' -> 'ana@example.example'."""
    _, addr = parseaddr(raw or "")
    return (addr or raw or "").strip().lower()


@dataclass(frozen=True)
class Customer:
    id: str
    workspace: str
    contact_name: str
    billing_contact: str
    admins: tuple[str, ...]
    plan: str
    billing: str  # monthly | annual
    seats: int
    currency: str

    def role_of(self, email: str) -> str | None:
        e = normalise_email(email)
        if e == self.billing_contact.lower():
            return "billing contact"
        if e in {a.lower() for a in self.admins}:
            return "admin"
        return None

    def authorised(self, email: str) -> bool:
        """The sender may act on this account: they are its billing contact or one of its admins."""
        return self.role_of(email) is not None


@dataclass(frozen=True)
class Charge:
    id: str
    customer_id: str
    invoice: str
    date: date
    type: str
    plan: str
    billing: str
    amount_cents: int
    currency: str
    status: str
    seats: int | None = None
    seats_added: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    refunded_on: date | None = None

    def status_on(self, today: date) -> str:
        if self.status == "refunded" and self.refunded_on is not None and self.refunded_on > today:
            return "paid"
        return self.status

    def describe(self) -> str:
        plan = self.plan.capitalize()
        if self.type == "seat_addition":
            return (f"{self.seats_added} seats added to the {plan} plan ({self.billing}), prorated to "
                    f"{self.period_end.isoformat() if self.period_end else 'the end of the period'}")
        return f"{plan} plan, {self.billing}, {self.seats} seats"


@dataclass(frozen=True)
class Chargeback:
    id: str
    customer_id: str
    charge_id: str
    opened_on: date
    status: str  # open | closed

    def open_on(self, today: date) -> bool:
        return self.status == "open" and self.opened_on <= today


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class Billing:
    def __init__(self, customers: list[Customer], charges: list[Charge], chargebacks: list[Chargeback]) -> None:
        self.customers = {c.id: c for c in customers}
        self.charges = {c.id: c for c in charges}
        self.chargebacks = list(chargebacks)

    @classmethod
    def from_dict(cls, data: dict) -> "Billing":
        customers = [
            Customer(c["id"], c["workspace"], c["contact_name"], c["billing_contact"].lower(),
                     tuple(a.lower() for a in c.get("admins", [])), c["plan"], c["billing"], int(c["seats"]),
                     c.get("currency", "USD"))
            for c in data["customers"]
        ]
        charges = [
            Charge(c["id"], c["customer_id"], c["invoice"], date.fromisoformat(c["date"]), c["type"], c["plan"],
                   c["billing"], to_cents(c["amount"]), c.get("currency", "USD"), c["status"], c.get("seats"),
                   c.get("seats_added"), _d(c.get("period_start")), _d(c.get("period_end")),
                   _d(c.get("refunded_on")))
            for c in data["charges"]
        ]
        chargebacks = [
            Chargeback(b["id"], b["customer_id"], b["charge_id"], date.fromisoformat(b["opened_on"]), b["status"])
            for b in data.get("chargebacks", [])
        ]
        return cls(customers, charges, chargebacks)

    @classmethod
    def load(cls, path: Path | str = BILLING_PATH) -> "Billing":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def customer(self, customer_id: str) -> Customer | None:
        return self.customers.get(customer_id)

    def customer_for_email(self, email: str) -> Customer | None:
        """The account whose billing contact or admin has this address."""
        for c in self.customers.values():
            if c.authorised(email):
                return c
        return None

    def charge(self, charge_id: str, today: date | None = None) -> Charge | None:
        c = self.charges.get(charge_id)
        if c is None or (today is not None and c.date > today):
            return None
        return c

    def charges_for(self, customer_id: str, today: date) -> list[Charge]:
        return sorted((c for c in self.charges.values() if c.customer_id == customer_id and c.date <= today),
                      key=lambda c: (c.date, c.id))

    def latest_monthly_charge(self, customer_id: str, today: date) -> Charge | None:
        """The most recent monthly subscription charge on which money was taken (paid or since refunded)."""
        taken = [c for c in self.charges_for(customer_id, today)
                 if c.type == "monthly_subscription" and c.status_on(today) in ("paid", "refunded")]
        return taken[-1] if taken else None

    def open_chargebacks(self, customer_id: str, today: date) -> list[Chargeback]:
        return [b for b in self.chargebacks if b.customer_id == customer_id and b.open_on(today)]


def expected_amount_cents(charge: Charge) -> int:
    """What a charge should cost under the price list (used to validate the fixture).

    Subscription: seats x price per seat per month (x 12 for annual). Seat addition: seats added x the
    per-seat price for the whole period x remaining days / days in the period, rounded half up to the cent.
    """
    price = PLAN_PRICES[charge.plan][charge.billing]
    per_seat_period = price * (12 if charge.billing == "annual" else 1)
    if charge.type in ("monthly_subscription", "annual_subscription"):
        return int(charge.seats or 0) * per_seat_period * 100
    if charge.period_start is None or charge.period_end is None or not charge.seats_added:
        raise ValueError(f"{charge.id}: a seat addition needs seats_added, period_start and period_end")
    remaining = (charge.period_end - charge.date).days
    period = (charge.period_end - charge.period_start).days
    amount = Decimal(charge.seats_added * per_seat_period) * Decimal(remaining) / Decimal(period)
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
