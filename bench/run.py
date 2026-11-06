"""Run the benchmark: every scenario through the full agent loop, graded without an LLM judge.

    python -m bench.run --models haiku,sonnet,opus --runs 3 --budget 7.5 --concurrency 4 --name third-run
    python -m bench.run --models haiku --runs 1 --ids m01,a02,c01,i01,q01 --budget 0.2 --name smoke-3   # smoke test

Writes runs/<name>/results.jsonl (one line per ticket run, with its full trace), runs/<name>/summary.json and
runs/<name>/costs.csv. Every API call is checked against --budget before it is sent (rta/ledger.py); the
expected cost of a full run is worked out in docs/RUNBOOK.md. A run never overwrites stored traces: it refuses to
start when runs/<name>/results.jsonl already holds a run (runs/ is not committed, so nothing else keeps them).

The run stops early when the budget would be passed, or on an API error every ticket would hit (a bad key, a
rejected request). The ticket that hit it is still recorded, with its trace and spend; tickets the budget cut
off part way are kept out of the quality metrics (bench/grade.py) and counted as budget_stops.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import yaml

from bench.grade import grade, summarise
from rta.agent import triage
from rta.billing import Billing
from rta.config import EFFORTS, MODELS, RUNS_DIR, SCENARIOS_PATH
from rta.kb import KB
from rta.ledger import CostLedger
from rta.llm import LLM
from rta.tools import Ticket


def load_scenarios(ids: list[str] | None = None, path: Path | str = SCENARIOS_PATH) -> list[dict]:
    # libyaml's safe loader when PyYAML has it: the same safe subset, about ten times faster on this file, which the
    # tests read hundreds of times.
    scenarios = yaml.load(Path(path).read_text(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))["scenarios"]
    if ids:
        unknown = sorted(set(ids) - {s["id"] for s in scenarios})
        if unknown:
            raise SystemExit(f"unknown scenario id(s): {', '.join(unknown)}")
        scenarios = [s for s in scenarios if s["id"] in ids]
    return scenarios


def find_scenario(sid: str) -> dict | None:
    return next((s for s in load_scenarios() if s["id"] == sid), None)


def ticket_for(scenario: dict, run_no: int | None = None) -> Ticket:
    suffix = f"-r{run_no}" if run_no else ""
    return Ticket(f"S-{scenario['id']}{suffix}", scenario["from"].strip().lower(), scenario["subject"].strip(),
                  scenario["body"].strip(), date.fromisoformat(str(scenario["today"])))


def row_for(model: str, run_no: int, scenario: dict, result: dict) -> dict:
    g = grade(scenario, result)
    return {
        "model": model, "run": run_no, **g.as_dict(),
        "end_reason": result.get("end_reason"), "error": result.get("error"),
        "cost_usd": result.get("cost_usd", 0.0), "latency_ms": result.get("latency_ms"),
        "tool_calls": result.get("tool_call_count", 0), "turns": result.get("turn_count", 0),
        "server_fallback_used": result.get("server_fallback_used", False),
        "trace": result,
    }


def row_usd(rows: list[dict]) -> float:
    return sum(r.get("cost_usd") or 0.0 for r in rows)


def run(models: list[str], runs: int, effort: str, budget: float, scenarios: list[dict], out: Path,
        concurrency: int = 4, llm: LLM | None = None) -> dict:
    stored = out / "results.jsonl"
    if stored.exists() and stored.stat().st_size > 0:
        raise SystemExit(f"{stored} already holds a run; give this run a new --name (bench.run would overwrite it)")
    out.mkdir(parents=True, exist_ok=True)
    if llm is None:
        llm = LLM(CostLedger(path=out / "costs.csv", run_budget_usd=budget))
    billing, kb = Billing.load(), KB.load()
    stop = threading.Event()
    spent_before = llm.ledger.run_usd if llm.ledger is not None else 0.0

    def spent() -> float:
        return llm.ledger.run_usd - spent_before if llm.ledger is not None else 0.0
    jobs = [(m, r, s) for m in models for r in range(1, runs + 1) for s in scenarios]

    def one(model: str, run_no: int, scenario: dict) -> dict | None:
        if stop.is_set():
            return None
        ticket = ticket_for(scenario, run_no)
        try:
            result = triage(ticket, llm, billing, kb, model=model, effort=effort,
                            label=f"{model}:{scenario['id']}:r{run_no}").as_dict()
        except Exception as e:  # a crash in one ticket is recorded, and the run goes on
            result = {"error": f"{type(e).__name__}: {e}"}
        if result.get("end_reason") == "budget_stop" or result.get("api_error_fatal"):
            stop.set()  # the ticket is still recorded below, with what it spent and any action it took
            print(f"STOP ({model} {scenario['id']} run {run_no}): {(result.get('errors') or ['?'])[-1]}")
        return row_for(model, run_no, scenario, result)

    rows: list[dict] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool, (out / "results.jsonl").open("w") as f:
        futures = [pool.submit(one, *j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            if row is None:
                continue
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if i % 10 == 0 or i == len(jobs):
                print(f"{i}/{len(jobs)} done, ${spent():.3f} spent")

    summary = {
        "config": {"models": models, "runs": runs, "effort": effort, "scenarios": len(scenarios),
                   "scenario_ids": [s["id"] for s in scenarios], "concurrency": concurrency, "budget_usd": budget},
        "complete": not stop.is_set() and len(rows) == len(jobs),
        # Everything the ledger recorded for this run, including tickets the budget cut off part way.
        "spent_usd": round(spent() if llm.ledger is not None else row_usd(rows), 4),
        "graded_tickets_usd": round(row_usd([r for r in rows if r.get("end_reason") != "budget_stop"]), 4),
        "wall_seconds": round(time.time() - started),
        "models": {},
    }
    for m in models:
        mine = [r for r in rows if r["model"] == m]
        if mine:
            summary["models"][m] = {"label": MODELS[m].label, **summarise(mine)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="haiku", help="comma-separated: haiku,sonnet,opus")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--effort", default="low", choices=list(EFFORTS))
    ap.add_argument("--budget", type=float, required=True, help="USD cap for this run")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--ids", default="", help="comma-separated scenario ids (default: all)")
    ap.add_argument("--name", default="")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    for m in models:
        if m not in MODELS:
            raise SystemExit(f"unknown model {m!r}; choose from {', '.join(MODELS)}")
    scenarios = load_scenarios([i.strip() for i in a.ids.split(",") if i.strip()] or None)
    name = a.name or time.strftime("%Y%m%d-%H%M%S") + f"-{'-'.join(models)}-{a.effort}"
    summary = run(models, a.runs, a.effort, a.budget, scenarios, RUNS_DIR / name, a.concurrency)
    print(json.dumps({m: {k: v for k, v in s.items() if k not in ("by_category",)}
                      for m, s in summary["models"].items()}, indent=2))
    print(f"spent ${summary['spent_usd']} in {summary['wall_seconds']} s -> runs/{name}")


if __name__ == "__main__":
    main()
