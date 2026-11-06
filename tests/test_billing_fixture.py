"""The fictional billing fixture is internally consistent and every amount follows the price list."""

from __future__ import annotations

import json
import re

from rta.billing import PLAN_PRICES, SEAT_LIMITS, CHARGE_TYPES, CURRENCIES, STATUSES, expected_amount_cents
from rta.config import BILLING_PATH

RESERVED = re.compile(r"^[a-z0-9._+-]+@([a-z0-9-]+\.)*(example|example\.com|example\.org|example\.net)$")


def test_fixture_is_labelled_fictional():
    raw = json.loads(BILLING_PATH.read_text())
    assert "FICTIONAL" in raw["_about"]


def test_about_fifteen_customers_with_unique_ids_and_addresses(billing):
    assert 15 <= len(billing.customers) <= 20
    addresses = [a for c in billing.customers.values() for a in (c.billing_contact, *c.admins)]
    assert len(addresses) == len(set(addresses)), "an address belongs to one account only"
    assert all(RESERVED.match(a) for a in addresses), "only reserved example domains"
    assert all(re.fullmatch(r"[A-Z][a-z]+", c.contact_name) for c in billing.customers.values()), "first names only"


def test_every_charge_amount_follows_the_price_list(billing):
    for c in billing.charges.values():
        assert c.amount_cents == expected_amount_cents(c), c.id


def test_the_worked_example_from_the_price_list():
    raw = json.loads(BILLING_PATH.read_text())
    team_annual_5 = next(c for c in raw["charges"] if c["id"] == "ch_1041")
    assert (team_annual_5["plan"], team_annual_5["billing"], team_annual_5["seats"]) == ("team", "annual", 5)
    assert team_annual_5["amount"] == 5 * 15 * 12 == 900


def test_charges_are_well_formed(billing):
    invoices = [c.invoice for c in billing.charges.values()]
    assert len(invoices) == len(set(invoices))
    for c in billing.charges.values():
        customer = billing.customer(c.customer_id)
        assert customer is not None, c.id
        assert c.type in CHARGE_TYPES and c.status in STATUSES and c.currency in CURRENCIES
        assert (c.plan, c.billing, c.currency) == (customer.plan, customer.billing, customer.currency), c.id
        assert c.plan in PLAN_PRICES
        if c.type == "seat_addition":
            assert c.period_start <= c.date < c.period_end, c.id
            years = c.period_end.year - c.period_start.year
            months = years * 12 + c.period_end.month - c.period_start.month
            assert months == (12 if c.billing == "annual" else 1) and c.period_end.day == c.period_start.day
        else:
            assert c.type == f"{c.billing}_subscription", c.id
        if c.status == "refunded":
            assert c.refunded_on is not None and c.refunded_on >= c.date
        else:
            assert c.refunded_on is None


def test_seats_add_up_and_stay_within_plan_limits(billing):
    for customer in billing.customers.values():
        lo, hi = SEAT_LIMITS[customer.plan]
        assert lo <= customer.seats and (hi is None or customer.seats <= hi), customer.id
        charges = sorted((c for c in billing.charges.values() if c.customer_id == customer.id),
                         key=lambda c: c.date)
        subs = [c for c in charges if c.type != "seat_addition"]
        latest = subs[-1]
        added = sum(c.seats_added for c in charges if c.type == "seat_addition" and c.date > latest.date)
        assert latest.seats + added == customer.seats, customer.id


def test_usd_except_one_documented_eur_account(billing):
    non_usd = [c for c in billing.customers.values() if c.currency != "USD"]
    assert [c.currency for c in non_usd] == ["EUR"]
    # One non-USD case in the whole fixture: the charge ticket e01 asks about.
    assert [c.id for c in billing.charges.values() if c.currency != "USD"] == ["ch_1112"]


def test_chargebacks_point_at_the_customers_own_charges(billing):
    assert billing.chargebacks
    for cb in billing.chargebacks:
        charge = billing.charges[cb.charge_id]
        assert charge.customer_id == cb.customer_id and cb.opened_on > charge.date
