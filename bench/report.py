"""Turn benchmark runs into committed results: bench/results/<name>.md and bench/results/<name>.json.

    python -m bench.report runs/second-run                  # writes second-run.md and second-run.json
    python -m bench.report runs/main --name first-run       # the 6 Nov run, written as first-run.md and .json
    python -m bench.report runs/third-run runs/third-run-extra --name third-run-merged   # merges two runs

Runs with the same settings (runs, effort, scenario ids) merge into one table. Every stored trace is graded again
with the grader and scenarios in the repository, so a report's verdicts match them when it is written; every ticket
whose verdict differs from the one stored when it ran is listed at the end, with the grader's note, and every miss
shows the text each forbidden check matched. What reconciliation decided at run time (flags, final queue, enforced
outcome) stays as recorded. No API call is made.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from bench.grade import grade, matches, normalise, phrases, summarise
from bench.run import load_scenarios
from rta.config import MODELS, ROOT

CATEGORY_LABELS = {
    "monthly": "Monthly charges", "annual": "Annual plans", "seats": "Seat additions",
    "nothing_to_refund": "Nothing to refund (failed, already refunded)",
    "routed": "Routed to people by the rules (chargeback, currency)", "identity": "Identity (other account, unknown sender)",
    "request": "Judgement on the request (partial, dispute, goodwill, missing invoice)",
    "traps": "Traps (old policy, forum claim, prompt injection, angry customer)", "questions": "Not refund requests",
}
SAME_SETTINGS = ("runs", "effort", "scenario_ids")


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _setting(config: dict, key: str):
    value = config.get(key)
    return sorted(value) if isinstance(value, list) else value


def regrade(rows: list[dict]) -> list[dict]:
    """Grade every stored trace again; return the tickets whose verdict differs from the stored one, each with the
    grader's notes then (stored) and now."""
    scenarios = {s["id"]: s for s in load_scenarios()}
    changed = []
    for r in rows:
        g = grade(scenarios[r["sid"]], r["trace"])
        if g.passed != r["passed"]:
            changed.append({"model": r["model"], "sid": r["sid"], "run": r["run"], "was": r["passed"],
                            "now": g.passed, "notes_then": list(r.get("notes") or []), "notes": g.notes})
        r.update(g.as_dict())
    return changed


_LONG_PATTERN = re.compile(r"""(['"])re:((?:(?!\1).){60})(?:(?!\1).)+\1""")


def short(note: str) -> str:
    """A grader note with each long regular expression cut to its first 60 characters (the JSON keeps them whole)."""
    return _LONG_PATTERN.sub(lambda m: f"{m.group(1)}re:{m.group(2)}…{m.group(1)}", note)


def matched(scenario: dict, reply: str) -> list[str]:
    """The text each forbidden phrase or pattern matched in the reply. Many patterns share their first 60
    characters (the sentence-scope prefix), so the shortened pattern alone doesn't say which check failed."""
    t, out = normalise(reply), []
    for p in phrases(scenario.get("reply_must_not")):
        if p.startswith("re:"):
            m = re.search(p[3:], t, re.I)
            if m:
                text = m.group(0).strip(" .;:!?")
                out.append(text if len(text) <= 100 else "…" + text[-99:])  # the end of a match is the claim
        elif matches(reply, p):
            out.append(p)
    return out


def _reply(r: dict) -> str:
    trace = r["trace"]
    return (trace.get("stated") or {}).get("reply_to_customer") or (trace.get("final") or {}).get("reply") or ""


def _armed(summary: dict, m: str) -> str:
    if not MODELS[m].server_fallback:
        return "–"
    armed = summary["models"][m].get("server_fallback_armed_turns")
    return "not recorded" if armed is None else f"{armed} of {summary['models'][m].get('model_turns')}"


def build(run_dirs: list[Path], name: str) -> tuple[dict, str]:
    summaries = [json.loads((d / "summary.json").read_text()) for d in run_dirs]
    cfg = dict(summaries[0]["config"])
    for d, s in zip(run_dirs[1:], summaries[1:], strict=True):
        for k in SAME_SETTINGS:
            if _setting(s["config"], k) != _setting(cfg, k):
                raise SystemExit(f"{d} differs from {run_dirs[0]} in {k}: {s['config'].get(k)!r} vs {cfg.get(k)!r}")
    rows = [json.loads(line) for d in run_dirs for line in (d / "results.jsonl").read_text().splitlines()
            if line.strip()]
    changed = regrade(rows)
    models = list(dict.fromkeys(m for s in summaries for m in s["config"]["models"]))
    summary = {
        "config": {**cfg, "models": models},
        "runs_merged": [d.name for d in run_dirs],
        "complete": all(s["complete"] for s in summaries),
        "spent_usd": round(sum(s["spent_usd"] for s in summaries), 4),
        "models": {m: {"label": MODELS[m].label, **summarise([r for r in rows if r["model"] == m])} for m in models},
        "regraded_changes": changed,
    }
    labels = [summary["models"][m]["label"] for m in models]
    size = f"{cfg['scenarios']} scenarios x {cfg['runs']} runs"
    lines = [f"# Benchmark results: {name}", "",
             f"{size} per model ({cfg['scenarios'] * cfg['runs']} tickets), effort `{cfg['effort']}`. Runs: "
             f"{', '.join(summary['runs_merged'])}; API spend ${summary['spent_usd']:.2f}. "
             f"Complete: {summary['complete']}.", "",
             "| | " + " | ".join(labels) + " |", "|---|" + "---|" * len(models)]
    metric_rows = [
        (f"Pass rate (every check; {size})", "pass_rate", pct),
        ("Decision accuracy (what the model stated)", "decision_accuracy", pct),
        ("Outcome accuracy (what the tools did)", "outcome_accuracy", pct),
        ("Reply checks passed", "reply_check_pass_rate", pct),
        ("Layout passed (separate lines, sign-off on its own line)", "layout_pass_rate", pct),
        ("Refused attempts per 100 tickets", "refused_attempts_per_100", lambda v: f"{v:.1f}"),
        ("Executed violations (must be 0)", "executed_violations", str),
        ("Inconsistent finishes escalated by code", "inconsistent_finishes", str),
        ("Turn cap reached", "turn_cap_hits", str),
        ("Stopped by the budget (not graded)", "budget_stops", str),
        ("Ignored the verified sender (see below)", "ignored_verified_sender", str),
        ("Tool calls per ticket", "tool_calls_per_ticket", lambda v: f"{v:.1f}"),
        ("$ per 1,000 tickets, measured with a warm prompt cache (tickets less than 5 minutes apart)",
         "usd_per_1000_tickets", lambda v: f"${v:.2f}"),
        ("$ per 1,000 tickets with a cold cache (estimate, see below)", "usd_per_1000_tickets_cold_cache_estimate",
         lambda v: f"${v:.2f}"),
        ("Latency p50 (whole ticket)", "latency_p50_ms", lambda v: f"{v / 1000:.1f} s"),
        ("Latency p95 (whole ticket)", "latency_p95_ms", lambda v: f"{v / 1000:.1f} s"),
    ]
    for label, key, fmt in metric_rows:
        vals = [summary["models"][m].get(key) for m in models]
        lines.append(f"| {label} | " + " | ".join("–" if v is None else fmt(v) for v in vals) + " |")
    lines.append("| Tickets whose first turn read the prompt cache | " + " | ".join(
        "–" if summary["models"][m].get("first_turn_cache_reads") is None
        else f"{summary['models'][m]['first_turn_cache_reads']} of {summary['models'][m]['n']}" for m in models) + " |")
    armed = [_armed(summary, m) for m in models]
    lines.append("| Model turns with the server-side refusal fallback armed | " + " | ".join(armed) + " |")
    fallback = ("- **Refusal fallback.** Sonnet 5.5 and Opus 5.5 requests are sent with the server-side refusal "
                "fallback unless the API rejects the beta; Haiku 5.5 has none (–).")
    if "not recorded" in armed:
        fallback += (" \"Not recorded\": the traces predate the per-turn `fallback_armed` field, so this report can't "
                     "show whether the fallback was armed.")
    lines += ["",
              f"- **Scenarios, not independent tickets.** Every pass rate here is over {size} per model. The runs "
              "of one scenario are far from independent (\"By scenario\" below shows how often they differ), so the "
              f"evidence is closer to {cfg['scenarios']} cases than to {cfg['scenarios'] * cfg['runs']}, and the "
              "scenarios are designed cases, not a sample of a client's email.",
              "- **What is graded.** The charge is graded on refunds, approvals, declines and escalations that name a "
              "charge, not on needs_details. The queue is a routing invariant that code enforces (escalation_route() "
              "at reconciliation; send_for_approval accepts only the billing_lead queue), not a model check: it can "
              "fail only together with the decision or consistency. Layout counts toward the pass rate: a reply "
              "passes only with line breaks and the Fernway Support sign-off on its own line.",
              "- **Cost.** The benchmark sends tickets seconds apart, so a ticket's first turn reads the cached system "
              "prompt and tools that an earlier ticket wrote (\"Tickets whose first turn read the prompt cache\"), and "
              "the measured cost is a warm-cache cost. The cold-cache row is an estimate from the same recorded "
              "usage, with the first turn's cache reads re-priced at the cache-write rate, as for a ticket that "
              "arrives more than 5 minutes after the last one. Later turns stay as recorded. Runs 2 and 3 of a "
              "scenario send the same email as run 1, so their first turn also reads that email from the cache, "
              "which a client's distinct emails would not; the warm figure is a few percent low for that reason "
              "(DECISIONS.md, \"Audit of the third run\", gives the estimate).",
              fallback,
              "- **Ignored the verified sender.** Tickets from a known customer's address that ended with a decision "
              "other than not_refund_request without a find_customer call.",
              "- **Re-grading.** Every verdict is the current grader's. What reconciliation decided at run time "
              "(flags, final queue, enforced outcome) stays as the run's code recorded it, and so does the "
              "\"Inconsistent finishes\" count."]
    lines += ["", "## By scenario", "",
              "| | " + " | ".join(labels) + " |", "|---|" + "---|" * len(models),
              "| Scenarios where every run passed | " + " | ".join(
                  f"{summary['models'][m]['scenarios_all_runs_passed']} of {summary['models'][m]['scenarios']}"
                  for m in models) + " |",
              "| Scenarios whose runs differ in the stated decision, charge or tool sequence | " + " | ".join(
                  (f"{len(v)}: {', '.join(v)}" if v else "0") for v in
                  (summary["models"][m]["scenarios_varying"] for m in models)) + " |"]
    lines += ["", "## By category", "", f"Pass rate per category, over each category's scenarios x {cfg['runs']} runs.",
              "", "| Category | Scenarios | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(models)]
    counts: dict[str, set] = {}
    for r in rows:
        counts.setdefault(r["category"], set()).add(r["sid"])
    for cat, label in CATEGORY_LABELS.items():
        if cat in counts:
            lines.append(f"| {label} | {len(counts[cat])} | " +
                         " | ".join(pct(summary["models"][m]["by_category"].get(cat)) for m in models) + " |")
    lines += ["", "## Every miss", ""]
    misses = [r for r in rows if not r["passed"] and r.get("end_reason") != "budget_stop"]  # as in the metrics
    if not misses:
        lines.append("None.")
    order = {m: i for i, m in enumerate(models)}
    by_id = {s["id"]: s for s in load_scenarios()}
    for r in sorted(misses, key=lambda r: (order[r["model"]], r["sid"], r["run"])):
        reply = ((r["trace"].get("stated") or {}).get("reply_to_customer") or "").replace("\n", " ")
        hits = matched(by_id[r["sid"]], _reply(r))
        lines.append(f"- **{r['model']} {r['sid']} run {r['run']}**: {'; '.join(short(n) for n in r['notes'])}"
                     + (f". Matched: {'; '.join(repr(h) for h in hits)}" if hits else "")
                     + (f". Reply: {reply[:200]}" if reply else ""))
    lines += ["", "## Verdicts changed by re-grading", ""]
    if not changed:
        lines.append("None: every stored verdict stands under the current grader and scenarios.")
    else:
        lines += [f"{len(changed)} tickets have a verdict that differs from the one recorded when they ran, because "
                  "the grader or the scenarios changed since (DECISIONS.md records each grader and scenario "
                  "change). The note is the current grader's for a new miss, and the one recorded at run time for "
                  "a new pass.", ""]
        for c in sorted(changed, key=lambda c: (c["sid"], order[c["model"]], c["run"])):
            note = c["notes"] if not c["now"] else [f"was: {n}" for n in c.get("notes_then") or []]
            lines.append(f"- {c['sid']} {c['model']} run {c['run']}: {'pass' if c['was'] else 'miss'} -> "
                         f"{'pass' if c['now'] else 'miss'}"
                         + (f". {'; '.join(short(n) for n in note)}" if note else ""))
    return summary, "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    run_dirs = [Path(d) for d in a.run_dirs]
    name = a.name or run_dirs[0].name
    summary, md = build(run_dirs, name)
    out = ROOT / "bench" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / f"{name}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
