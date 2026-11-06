"""Deterministic grading of one ticket against its scenario. No LLM judge.

Per ticket: the decision the model stated, the outcome the tools enforced, the amount and the charge, the
queue, the reply (required phrases, forbidden phrases, internal terms and, on a declined charge with a refund
window, the window's last day), the reply's layout, the tools that must not be called, consistency (no
reconciliation flags) and executed violations (must be zero). Refused attempts are counted; a refused call fails a
ticket only through tools_must_not, which lists the action tools the rules don't call for.

The queue is a routing invariant that code enforces (escalation_route() at reconciliation, the send_for_approval
schema and its guard), not a model skill: it can only fail together with the decision or consistency. The charge is
graded on refunds, approvals, declines and escalations that name a charge, not on needs_details.

The text matching follows the grader in the author's grounded-rag-assistant repository.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache

from rta import policy
from rta.billing import Billing, to_cents
from rta.config import FALLBACK_MODELS, MODELS

_SPECS = {m.model_id: m for m in MODELS.values()} | FALLBACK_MODELS


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―]", "-", text)  # hyphens and dashes
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 1,000 -> 1000
    return re.sub(r"\s+", " ", text).strip().lower()


def matches(text: str, alt: str) -> bool:
    """A phrase (whole words) or, with a 're:' prefix, a regular expression, in normalised text."""
    t = normalise(text)
    if alt.startswith("re:"):
        return re.search(alt[3:], t, re.I) is not None
    p = normalise(alt)
    left = r"(?<![a-z0-9])" if p[:1].isalnum() else ""
    right = r"(?![a-z0-9])" if p[-1:].isalnum() else ""
    return re.search(left + re.escape(p) + right, t) is not None


def phrases(items: list | None) -> list[str]:
    """A reply_must_not list with one level of nesting flattened. YAML can't append to an alias, so a scenario
    writes [*no_refund_claims, *window_closed] or [*no_refund_claims, 're:...'] and gets a list inside a list."""
    out: list[str] = []
    for item in items or []:
        out.extend(item if isinstance(item, list) else [item])
    return out


# ------------------------------------------------------------------------------- layout (second-run audit)

_SIGN_OFF = re.compile(r"\n[ \t]*Fernway Support[ \t]*\Z", re.I)


def layout_ok(reply: str | None) -> bool:
    """The raw reply (before normalise(), which collapses whitespace) has line breaks, and the "Fernway Support"
    sign-off sits on its own last line. 21 of 90 Sonnet replies in the second run were one line of text."""
    text = (reply or "").strip()
    return "\n" in text and _SIGN_OFF.search(text) is not None


# -------------------------------------------------------------------------- window dates (second-run audit)

_WINDOW_DAYS = {"monthly_subscription": policy.MONTHLY_WINDOW_DAYS, "annual_subscription": policy.ANNUAL_WINDOW_DAYS}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
          r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# A date with a month name, or in ISO form (a bare "the 22nd" or "22/10" is not read as a date), with an optional
# weekday and "the" before it.
_DATE = (r"(?P<date>(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,? )?(?:the )?"
         rf"(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?: of)? (?P<m1>{_MONTH})\b\.?(?:,? (?P<y1>\d{{4}})\b)?"
         rf"|(?P<m2>{_MONTH})\.? (?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,? (?P<y2>\d{{4}})\b)?"
         r"|(?P<iso>\d{4}-\d{2}-\d{2})\b))")
# A clause that names another window is not checked: the old 14-day rule, the forum's 60 days, a hypothetical, or
# another invoice (filled in per ticket). An "if ... within N days" clause before the window word states this window's
# own condition ("can be refunded only if you ask within 7 days, and that window ended on ..."), so it is checked.
_OTHER = (r"(?!\b(?:14|fourteen|60|sixty|2025|would|"
          r"if\b(?!(?:(?!\b(?:window|period)\b)(?:[^.;!?:]|\.\d))*?\bwithin\b)|"
          r"(?:old|older|previous|former|earlier|prior|superseded)\s+"
          r"(?:(?:\d+|fourteen|sixty)[- ]days?\s+)?(?:refund\s+)?(?:polic(?:y|ies)|rules?|terms|version|window|period)"
          r"{other})\b)")
_START = r"(?:^|[;!?:]\s?|\.(?!\d)\s?)"


@lru_cache(maxsize=None)
def _window_date_claims(invoice: str) -> re.Pattern:
    other = _OTHER.replace("{other}", f"|fw-(?!{re.escape(invoice[3:])})\\d{{5}}" if invoice else "")
    clause = rf"{_START}(?:{other}(?:[^.;!?:]|\.\d))*?"
    gap = rf"(?:{other}(?:[^.;!?]|\.\d))"
    return re.compile(
        rf"{clause}\b(?:window|(?<!trial )(?<!billing )(?<!free )period)\b(?:{gap}{{0,40}}?|{gap}{{0,100}}?,\s+which\s+)"
        rf"\b(?:(?:closed|ended|expired|lapsed|ran out)\s+(?:on\s+)?"
        rf"|(?:was|stayed|remained)\s+open\s+(?:until|till|through)\s+){_DATE}"
        # "last day" only as the window's: "its last day was", "the last day of the (30-day) (refund) window",
        # "the last day to ask (for a refund)", "the last day you could ask"; not "the last day of your term".
        rf"|{clause}(?<!billing period's )(?<!billing cycle's )\blast day\b"
        rf"(?:\s+(?:of|in|for)\s+(?:the |that |this |your |its )?(?:(?:\d+|seven|thirty)[- ]days?\s+)?(?:refund\s+)?"
        rf"(?:window|period)\b"
        rf"|\s+(?:to|for)\s+(?:ask|request|apply|claim|get|be refunded|a refund|refunds?)\b{gap}{{0,25}}?"
        rf"|\s+(?:you|she|he|they|we) could\s+(?:ask|request|apply|claim|get)\b{gap}{{0,25}}?)?,?"
        rf"\s+(?:was|is|fell on|being)\s+(?:on\s+)?{_DATE.replace('?P<', '?P<b_')}")


def window_last_day(scenario: dict, billing: Billing | None = None) -> date | None:
    """The last day of the refund window of a declined ticket's charge (monthly 7 days, annual 30 days, seats on an
    annual plan 30 days), from the billing data and rta.policy.last_day_in_window(); None for any other ticket."""
    exp = scenario.get("expect") or {}
    if exp.get("decision") != policy.DECLINED or not exp.get("charge_id"):
        return None
    charge = (billing or _billing()).charges.get(exp["charge_id"])
    if charge is None:
        return None
    days = _WINDOW_DAYS.get(charge.type)
    if charge.type == "seat_addition" and charge.billing == "annual":
        days = policy.SEAT_WINDOW_DAYS
    return policy.last_day_in_window(charge.date, days) if days else None


def _as_date(m: re.Match, prefix: str = "") -> tuple[int | None, int, int]:
    g = {k[len(prefix):]: v for k, v in m.groupdict().items() if k.startswith(prefix) and v is not None}
    if g.get("iso"):
        y, mo, d = (int(x) for x in g["iso"].split("-"))
        return y, mo, d
    month = _MONTHS.index((g.get("m1") or g.get("m2"))[:3]) + 1
    year = g.get("y1") or g.get("y2")
    return (int(year) if year else None), month, int(g.get("d1") or g.get("d2"))


def wrong_window_dates(reply: str, scenario: dict, billing: Billing | None = None) -> list[str]:
    """Dates a reply gives as the day the window closed, ended, expired, lapsed or ran out, or as its last day, that
    are not the window's last day (DECISIONS.md, gap 1). Matched on the normalised reply; [] when the ticket has no
    window to check."""
    last = window_last_day(scenario, billing)
    if last is None:
        return []
    invoice = (billing or _billing()).charges[scenario["expect"]["charge_id"]].invoice.lower()
    wrong = []
    for m in _window_date_claims(invoice).finditer(normalise(reply)):
        prefix = "" if m.group("date") else "b_"
        year, month, day = _as_date(m, prefix)
        if (month, day) != (last.month, last.day) or (year is not None and year != last.year):
            wrong.append(m.group(f"{prefix}date").rstrip("."))  # "4 november." -> "4 november" in the note
    return wrong


@dataclass
class Grade:
    sid: str
    category: str
    expected: str
    stated: str | None
    enforced: str | None
    final: str | None
    decision_ok: bool
    outcome_ok: bool
    amount_ok: bool | None
    charge_ok: bool | None
    queue_ok: bool | None
    reply_ok: bool
    tools_ok: bool
    consistent: bool
    refused_attempts: int
    violations: int
    passed: bool
    notes: list[str] = field(default_factory=list)
    layout_ok: bool = True  # line breaks, and the sign-off on its own line (second-run audit)

    def as_dict(self) -> dict:
        return asdict(self)


def grade(scenario: dict, result: dict) -> Grade:
    exp = scenario["expect"]
    want = exp["decision"]
    stated = result.get("stated") or {}
    final = result.get("final") or {}
    actions = result.get("actions") or []
    notes: list[str] = []
    if result.get("error"):
        notes.append(f"error: {result['error']}")

    decision_ok = stated.get("decision") == want
    if not decision_ok:
        notes.append(f"stated {stated.get('decision') or 'nothing'}, expected {want}")
    enforced = result.get("enforced_outcome")
    outcome_ok = enforced == want
    if not outcome_ok:
        notes.append(f"enforced outcome {enforced or 'none'}")

    amount_ok = None
    if exp.get("amount") is not None:
        target = to_cents(exp["amount"])
        stated_ok = stated.get("amount") is not None and to_cents(stated["amount"]) == target
        action_ok = (any(to_cents(a["amount"]) == target for a in actions)
                     if want in (policy.REFUND_ISSUED, policy.SENT_FOR_APPROVAL) else True)
        amount_ok = stated_ok and action_ok
        if not amount_ok:
            notes.append(f"amount {stated.get('amount')} (actions: {[a['amount'] for a in actions]}), "
                         f"expected {exp['amount']}")

    charge_ok = None
    if exp.get("charge_id") and want in (policy.REFUND_ISSUED, policy.SENT_FOR_APPROVAL, policy.DECLINED):
        charge_ok = stated.get("charge_id") == exp["charge_id"] and (
            want == policy.DECLINED or any(a["charge_id"] == exp["charge_id"] for a in actions))
    elif exp.get("charge_id") and want == policy.ESCALATED and stated.get("charge_id") is not None:
        # An escalation hands a person the charge it names, so a wrong one fails. Naming none is not checked:
        # reconciliation accepts an escalation without a charge, and there is no action to compare.
        charge_ok = stated["charge_id"] == exp["charge_id"]
    if charge_ok is False:
        notes.append(f"charge {stated.get('charge_id')}, expected {exp['charge_id']}")

    queue_ok = None
    if exp.get("queue"):
        queue_ok = final.get("queue") == exp["queue"]
        if not queue_ok:
            notes.append(f"queue {final.get('queue')}, expected {exp['queue']}")

    reply = stated.get("reply_to_customer") or final.get("reply") or ""
    missing = [g for g in scenario.get("reply_must_include", []) if not any(matches(reply, alt) for alt in g)]
    forbidden = [t for t in phrases(scenario.get("reply_must_not")) if matches(reply, t)]
    # As in reconciliation: a bare $500.00 is allowed only as an amount the tools actually refunded.
    leaks = policy.internal_terms(reply, [to_cents(a["amount"]) for a in actions if a.get("amount") is not None])
    wrong_dates = wrong_window_dates(reply, scenario)
    reply_ok = not missing and not forbidden and not leaks and not wrong_dates
    for g in missing:
        notes.append(f"reply lacks one of {g}")
    if forbidden:
        notes.append(f"reply contains {forbidden}")
    if leaks:
        notes.append(f"reply names internal terms: {leaks}")
    if wrong_dates:
        last = window_last_day(scenario)
        notes.append(f"reply gives {wrong_dates} as the end of the refund window; its last day is "
                     f"{last.day} {last:%B %Y}")
    layout = layout_ok(reply)
    if not layout:
        notes.append("layout: the reply is not on separate lines with the Fernway Support sign-off on its own line")

    called = result.get("tools_called") or []
    bad_tools = [t for t in scenario.get("tools_must_not", []) if t in called]
    if bad_tools:
        notes.append(f"called {bad_tools}")
    consistent = bool(final.get("consistent"))
    if final and not consistent:
        notes.append(f"flagged: {final.get('flags')}")
    violations = len(result.get("violations") or [])
    if violations:
        notes.append(f"EXECUTED VIOLATIONS: {result['violations']}")

    passed = (decision_ok and outcome_ok and amount_ok is not False and charge_ok is not False
              and queue_ok is not False and reply_ok and layout and not bad_tools and consistent and violations == 0)
    return Grade(scenario["id"], scenario.get("category", ""), want, stated.get("decision"), enforced,
                 final.get("decision"), decision_ok, outcome_ok, amount_ok, charge_ok, queue_ok, reply_ok,
                 not bad_tools, consistent, int(result.get("refused_attempts") or 0), violations, passed, notes,
                 layout)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1], 1)


def cold_cache_usd(row: dict) -> float | None:
    """An estimate of what a ticket would have cost if its first turn had found nothing in the prompt cache (a
    ticket arriving more than 5 minutes after the last one): the first turn's cache reads re-priced at the
    cache-write rate. Later turns stay as recorded, since they read what the first turn wrote. None without a trace."""
    trace = row.get("trace")
    if not isinstance(trace, dict):
        return None
    cost = float(row.get("cost_usd") or trace.get("cost_usd") or 0.0)
    turns = trace.get("turns") or []
    if not turns:
        return cost
    first = turns[0]
    spec = _SPECS.get(first.get("model_served") or "") or MODELS.get(row.get("model") or trace.get("model") or "")
    if spec is None:
        return None
    reads = int((first.get("usage") or {}).get("cache_read_input_tokens") or 0)
    return cost + reads * (spec.cache_write - spec.cache_read) / 1_000_000


@lru_cache(maxsize=1)
def _billing() -> Billing:
    return Billing.load()


def ignored_verified_sender(row: dict, billing: Billing | None = None) -> bool | None:
    """The helpdesk's verified sender is a known customer, yet the ticket ended with a decision other than
    not_refund_request without ever calling find_customer (in the 6 Nov run: Haiku on d01, run 1, which asked the
    verified billing contact to write in again). None without a trace."""
    trace = row.get("trace")
    if not isinstance(trace, dict):
        return None
    decision = (trace.get("stated") or {}).get("decision")
    sender = (trace.get("ticket") or {}).get("from")
    if not sender or decision in (None, policy.NOT_REFUND_REQUEST):
        return False
    known = (billing or _billing()).customer_for_email(sender) is not None
    return known and "find_customer" not in (trace.get("tools_called") or [])


def first_turn_cache_reads(rows: list[dict]) -> int | None:
    """Tickets whose first turn read the cached prompt prefix (a warm cache). None without traces."""
    firsts = [(r["trace"].get("turns") or [None])[0] for r in rows if isinstance(r.get("trace"), dict)]
    if not firsts:
        return None
    return sum(bool(t and (t.get("usage") or {}).get("cache_read_input_tokens")) for t in firsts)


def fallback_armed_turns(rows: list[dict]) -> int | None:
    """Model turns sent with the server-side refusal fallback armed (the beta call path). None when the traces
    don't record it (runs made before the field existed)."""
    turns = [t for r in rows if isinstance(r.get("trace"), dict) for t in (r["trace"].get("turns") or [])]
    if not any("fallback_armed" in t for t in turns):
        return None
    return sum(bool(t.get("fallback_armed")) for t in turns)


def run_shape(row: dict) -> tuple:
    """What one run of a scenario did: the stated decision, the stated charge and the tools of each model turn.

    Tools are compared turn by turn, sorted within a turn: two calls sent in parallel in one turn are the same step
    in either order, while a finish sent in the same turn as an action is a different path from a finish one turn
    later. Rows without per-turn data fall back to the flat tool sequence."""
    trace = row.get("trace") if isinstance(row.get("trace"), dict) else {}
    stated = trace.get("stated") or {}
    turns = trace.get("turns") or []
    if turns:
        tools = tuple(tuple(sorted(c.get("name") or "" for c in (t.get("tool_calls") or []))) for t in turns)
    else:
        tools = tuple(trace.get("tools_called") or ())
    return (stated.get("decision") or row.get("stated"), stated.get("charge_id"), tools)


def summarise(rows: list[dict], billing: Billing | None = None) -> dict:
    """Headline metrics for one model's result rows (one row per ticket run).

    A ticket the budget cut off part way says nothing about the model, so it is kept out of the metrics and
    counted under budget_stops; its spend is in the run's spent_usd. The trace-based counts (cold-cache cost,
    armed fallback turns, ignored verified sender) are None when the rows carry no trace.

    The runs of one scenario are far from independent (second-run audit), so the summary also counts scenarios:
    those where every run passed, and those whose runs differ in the stated decision, charge or tool sequence.
    """
    stopped = [r for r in rows if r.get("end_reason") == "budget_stop"]
    rows = [r for r in rows if r.get("end_reason") != "budget_stop"]
    n = len(rows)

    def share(xs: list) -> float | None:
        return round(sum(bool(x) for x in xs) / len(xs), 4) if xs else None

    cold = [c for c in (cold_cache_usd(r) for r in rows) if c is not None]
    ignored = [x for x in (ignored_verified_sender(r, billing) for r in rows) if x is not None]
    categories = sorted({r["category"] for r in rows})
    by_sid: dict[str, list[dict]] = {}
    for r in rows:
        by_sid.setdefault(r["sid"], []).append(r)
    return {
        "n": n,
        "pass_rate": share([r["passed"] for r in rows]),
        "decision_accuracy": share([r["decision_ok"] for r in rows]),
        "outcome_accuracy": share([r["outcome_ok"] for r in rows]),
        "reply_check_pass_rate": share([r["reply_ok"] for r in rows]),
        # None for rows graded before the layout check existed (re-grading fills it in)
        "layout_pass_rate": share([r["layout_ok"] for r in rows if "layout_ok" in r]),
        "scenarios": len(by_sid),
        "scenarios_all_runs_passed": sum(all(r["passed"] for r in rs) for rs in by_sid.values()),
        "scenarios_varying": sorted(sid for sid, rs in by_sid.items() if len({run_shape(r) for r in rs}) > 1),
        "refused_attempts_per_100": round(100 * sum(r["refused_attempts"] for r in rows) / n, 1) if n else None,
        "executed_violations": sum(r["violations"] for r in rows),
        "inconsistent_finishes": sum(not r["consistent"] for r in rows),
        "turn_cap_hits": sum(r.get("end_reason") == "turn_cap" for r in rows),
        "budget_stops": len(stopped),
        "errors": sum(bool(r.get("error")) or r.get("end_reason") == "api_error" for r in rows),
        "server_fallback_tickets": sum(bool(r.get("server_fallback_used")) for r in rows),
        "model_turns": sum(int(r.get("turns") or 0) for r in rows),
        "server_fallback_armed_turns": fallback_armed_turns(rows),
        "ignored_verified_sender": sum(ignored) if ignored else None,
        "tool_calls_per_ticket": round(sum(r.get("tool_calls", 0) for r in rows) / n, 2) if n else None,
        "usd_per_1000_tickets": round(1000 * sum(r.get("cost_usd", 0.0) for r in rows) / n, 3) if n else None,
        "first_turn_cache_reads": first_turn_cache_reads(rows),
        # An estimate, not a measurement: see cold_cache_usd().
        "usd_per_1000_tickets_cold_cache_estimate": round(1000 * sum(cold) / len(cold), 3) if cold else None,
        "latency_p50_ms": pct([r["latency_ms"] for r in rows if r.get("latency_ms") is not None], 50),
        "latency_p95_ms": pct([r["latency_ms"] for r in rows if r.get("latency_ms") is not None], 95),
        "by_category": {c: share([r["passed"] for r in rows if r["category"] == c]) for c in categories},
        "failures": sorted({r["sid"] for r in rows if not r["passed"]}),
    }
