"""Command line.

    rta triage --from EMAIL --subject S --body TEXT [--today YYYY-MM-DD] [--model haiku] [--budget 0.5]
    rta triage --scenario m01 [--model sonnet]      run one benchmark ticket and grade it
    rta charges --from EMAIL [--today YYYY-MM-DD]   what the rules say about each charge (no API call)
    rta scenarios                                   list the benchmark tickets (no API call)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from rta import policy
from rta.billing import Billing, fmt_money, normalise_email
from rta.config import DEFAULT_EFFORT, DEFAULT_MODEL, EFFORTS, LOGS_DIR, MODELS, ROOT, TODAY


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from e


def cmd_triage(a: argparse.Namespace) -> int:
    from bench.grade import grade
    from bench.run import find_scenario, ticket_for
    from rta.agent import new_ticket, summary_lines, triage
    from rta.config import MissingKeyError
    from rta.kb import KB
    from rta.ledger import CostLedger
    from rta.llm import LLM

    scenario = None
    if a.scenario:
        scenario = find_scenario(a.scenario)
        if scenario is None:
            print(f"No scenario {a.scenario!r}; see `rta scenarios`.", file=sys.stderr)
            return 2
        ticket = ticket_for(scenario)
    else:
        body = Path(a.body_file).read_text() if a.body_file else a.body
        if not (a.sender and a.subject and body):
            print("Give --from, --subject and --body (or --body-file), or --scenario ID.", file=sys.stderr)
            return 2
        ticket = new_ticket(a.sender, a.subject, body, a.today)
    try:
        llm = LLM(CostLedger(run_budget_usd=a.budget))
    except MissingKeyError as e:
        print(e, file=sys.stderr)
        return 2
    trace = triage(ticket, llm, Billing.load(), KB.load(), model=a.model, effort=a.effort,
                   label=f"cli:{ticket.ticket_id}")
    # Written for every ticket, including one the budget stopped part way: an earlier refund stays on record.
    path = trace.write(LOGS_DIR / f"{ticket.ticket_id}.json")
    if a.json:
        print(json.dumps(trace.as_dict(), indent=2, ensure_ascii=False))
    else:
        print("\n".join(summary_lines(trace)))
        print(f"Log: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if scenario is not None:
        g = grade(scenario, trace.as_dict())
        print(f"\nScenario {scenario['id']}: expected {scenario['expect']['decision']} -> "
              f"{'PASS' if g.passed else 'MISS'}" + (f" ({'; '.join(g.notes)})" if g.notes else ""))
    if trace.end_reason == "budget_stop":
        print(f"Stopped before the next API call ({trace.errors[-1]}). The ticket was escalated and its trace "
              f"written. --budget is ${a.budget:.2f} for this ticket; RTA_BUDGET_USD caps the ledger file's total.",
              file=sys.stderr)
        return 3
    return 0


def cmd_charges(a: argparse.Namespace) -> int:
    billing = Billing.load()
    sender = normalise_email(a.sender)
    customer = billing.customer_for_email(sender)
    if customer is None:
        print(f"No account has {sender} as its billing contact or an admin (decision: needs_details).")
        return 0
    print(f"{customer.workspace} ({customer.id}) · {customer.plan} plan, {customer.billing}, {customer.seats} seats · "
          f"{customer.currency} · {sender} is the {customer.role_of(sender)} · rules as of {a.today.isoformat()}")
    for cb in billing.open_chargebacks(customer.id, a.today):
        print(f"Open chargeback {cb.id} on {cb.charge_id}, opened {cb.opened_on.isoformat()}")
    print()
    for c in billing.charges_for(customer.id, a.today):
        e = policy.check_charge(billing, c.id, sender, a.today)
        route = f", queue {e.route}" if e.route else ""
        print(f"{c.invoice}  {c.id}  {c.date.isoformat()}  {c.type:<20}  {fmt_money(c.amount_cents, c.currency):>10}  "
              f"{c.status_on(a.today):<8}  -> {e.expected_decision} ({e.reason}{route})")
    return 0


def cmd_scenarios(a: argparse.Namespace) -> int:
    from bench.run import load_scenarios

    for s in load_scenarios():
        e = s["expect"]
        amount = f" {e['amount']:.2f}" if e.get("amount") is not None else ""
        print(f"{s['id']}  {s['today']}  {s['from']:<30}  {e['decision']}{amount}  {s['title']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rta", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("triage", help="run the agent on one email")
    t.add_argument("--from", dest="sender", help="the sender's email address, as verified by the helpdesk")
    t.add_argument("--subject")
    t.add_argument("--body")
    t.add_argument("--body-file", help="read the email body from a file")
    t.add_argument("--scenario", help="run a benchmark scenario by id instead")
    t.add_argument("--today", type=_date, default=_date(TODAY), help=f"request date (default {TODAY})")
    t.add_argument("--model", default=DEFAULT_MODEL, choices=list(MODELS))
    t.add_argument("--effort", default=DEFAULT_EFFORT, choices=list(EFFORTS))
    t.add_argument("--budget", type=float, default=0.5, help="USD cap for this ticket (default 0.5)")
    t.add_argument("--json", action="store_true", help="print the full trace as JSON")
    c = sub.add_parser("charges", help="show what the rules say about each of the sender's charges")
    c.add_argument("--from", dest="sender", required=True)
    c.add_argument("--today", type=_date, default=_date(TODAY))
    sub.add_parser("scenarios", help="list the benchmark scenarios")
    a = ap.parse_args(argv)
    return {"triage": cmd_triage, "charges": cmd_charges, "scenarios": cmd_scenarios}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
