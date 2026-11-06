"""The CLI and the benchmark runner and report, end to end with a fake client (no network, no key)."""

from __future__ import annotations

import json

import pytest

from bench import report
from bench.run import load_scenarios, run
from rta import agent, cli, config
from rta import llm as llm_module
from rta.ledger import CostLedger
from rta.llm import LLM
from tests.conftest import FakeClient, bad_request, finish, response, tool_use
from tests.test_agent_loop import StopAfter

ESCALATE = finish("escalated", "Thanks for your message. A colleague will review your request and reply to you "
                               "here.\n\nFernway Support")


@pytest.fixture(autouse=True)
def _fresh_system_message_cache(monkeypatch):
    monkeypatch.setattr(agent, "_SYSTEM_MESSAGES_OK", {})


def test_charges_shows_what_the_rules_say(capsys):
    assert cli.main(["charges", "--from", "ana@blueheron.example", "--today", "2026-11-04"]) == 0
    out = capsys.readouterr().out
    assert "Blue Heron Studio" in out and "billing contact" in out
    assert "FW-10013" in out and "refund_issued (monthly_within_7_days)" in out
    assert "FW-10012" in out and "not_most_recent_monthly_charge" in out


def test_charges_for_an_unknown_sender(capsys):
    cli.main(["charges", "--from", "nobody@mailbox.example"])
    assert "needs_details" in capsys.readouterr().out


def test_scenarios_lists_every_ticket(capsys):
    assert cli.main(["scenarios"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == len(load_scenarios())


def test_triage_without_a_key_explains_and_makes_no_call(monkeypatch, capsys):
    monkeypatch.setattr(config, "api_key", lambda: None)
    assert cli.main(["triage", "--scenario", "m01"]) == 2
    assert "RTA_ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_triage_needs_an_email_or_a_scenario(capsys):
    assert cli.main(["triage", "--from", "ana@blueheron.example"]) == 2


def test_triage_end_to_end_with_a_fake_client(monkeypatch, tmp_path, capsys):
    client = FakeClient([
        response(tool_use("find_customer", {"email": "ana@blueheron.example"})),
        response(tool_use("list_charges", {"customer_id": "cus_101"})),
        response(tool_use("check_refund_eligibility", {"charge_id": "ch_1013"})),
        response(tool_use("issue_refund", {"charge_id": "ch_1013", "amount": 190.0, "reason": "Within 7 days"})),
        response(finish("refund_issued", "Hi Ana,\n\nI've refunded invoice FW-10013, $190.00. It goes back to your "
                                         "original payment method within 5 to 10 business days.\n\nFernway Support",
                        "ch_1013", 190.0)),
    ])
    monkeypatch.setattr(llm_module, "make_client", lambda: client)
    monkeypatch.setattr(cli, "LOGS_DIR", tmp_path / "logs")
    assert cli.main(["triage", "--scenario", "m01"]) == 0
    out = capsys.readouterr().out
    assert "Final decision:   refund_issued" in out and "Scenario m01: expected refund_issued -> PASS" in out
    log = json.loads((tmp_path / "logs" / "S-m01.json").read_text())
    assert log["enforced_outcome"] == "refund_issued" and log["actions"][0]["ticket_id"] == "S-m01"


def test_triage_stopped_by_the_budget_still_writes_its_log(monkeypatch, tmp_path, capsys):
    client = FakeClient([response(tool_use("find_customer", {"email": "ana@blueheron.example"}))])
    monkeypatch.setattr(llm_module, "make_client", lambda: client)
    monkeypatch.setattr(cli, "LOGS_DIR", tmp_path / "logs")
    assert cli.main(["triage", "--scenario", "m01", "--budget", "0.0001"]) == 3
    assert client.requests == [] and "Stopped before the next API call" in capsys.readouterr().err
    log = json.loads((tmp_path / "logs" / "S-m01.json").read_text())
    assert (log["end_reason"], log["final"]["decision"], log["final"]["flags"]) == (
        "budget_stop", "escalated", ["no_finish:budget_stop"])


def test_bench_run_requires_a_budget(monkeypatch):
    import sys

    import bench.run as br

    monkeypatch.setattr(sys, "argv", ["bench.run", "--models", "haiku"])
    with pytest.raises(SystemExit) as e:
        br.main()
    assert e.value.code == 2


def test_a_budget_stop_records_the_ticket_and_ends_the_run(tmp_path):
    scenarios = load_scenarios(["k01", "w01", "q01"])  # k01 passes with an escalation; w01 is cut off
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(StopAfter(1, path=tmp_path / "costs.csv", budget_usd=5.0, run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "stopped"
    summary = run(["haiku"], 1, "low", 1.0, scenarios, out, concurrency=1, llm=llm)
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert [r["end_reason"] for r in rows] == ["finished", "budget_stop"] and len(client.requests) == 1
    assert not summary["complete"] and summary["spent_usd"] == pytest.approx(llm.ledger.run_usd, abs=1e-4)
    s = summary["models"]["haiku"]
    assert (s["n"], s["budget_stops"], s["pass_rate"]) == (1, 1, 1.0)  # the cut-off ticket isn't graded


def test_an_error_every_ticket_would_hit_stops_the_run(tmp_path):
    scenarios = load_scenarios(["k01", "w01", "m01"])
    client = FakeClient(default=lambda kw: bad_request("tools.0.input_schema: invalid"))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    summary = run(["haiku"], 1, "low", 1.0, scenarios, tmp_path / "runs" / "bad", concurrency=1, llm=llm)
    assert len(client.requests) == 1 and not summary["complete"]
    assert summary["models"]["haiku"]["errors"] == 1


def test_a_run_never_overwrites_stored_traces(tmp_path):
    out = tmp_path / "runs" / "main"
    out.mkdir(parents=True)
    (out / "results.jsonl").write_text('{"sid": "k01"}\n')
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=out / "costs.csv", run_budget_usd=1.0), client=client)
    with pytest.raises(SystemExit, match="new --name"):
        run(["haiku"], 1, "low", 1.0, load_scenarios(["k01"]), out, concurrency=1, llm=llm)
    assert (out / "results.jsonl").read_text() == '{"sid": "k01"}\n' and client.requests == []
    assert not (out / "summary.json").exists()


def test_report_merges_runs_only_over_the_same_tickets(tmp_path):
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    a, b, c = (tmp_path / "runs" / n for n in ("a", "b", "c"))
    run(["haiku"], 1, "low", 1.0, load_scenarios(["k01", "w01"]), a, concurrency=1, llm=llm)
    run(["sonnet"], 1, "low", 1.0, list(reversed(load_scenarios(["k01", "w01"]))), b, concurrency=1, llm=llm)
    run(["opus"], 1, "low", 1.0, load_scenarios(["k01", "m01"]), c, concurrency=1, llm=llm)
    merged, _ = report.build([a, b], "ab")  # same tickets in another order: fine
    assert merged["config"]["models"] == ["haiku", "sonnet"]
    with pytest.raises(SystemExit, match="scenario_ids"):
        report.build([a, c], "ac")  # as many tickets, but different ones


def test_runner_and_report_offline(tmp_path, monkeypatch):
    scenarios = load_scenarios(["k01", "w01", "m01"])
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "fake"
    summary = run(["haiku"], 2, "low", 1.0, scenarios, out, concurrency=2, llm=llm)
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and summary["complete"] and len(client.requests) == 6
    s = summary["models"]["haiku"]
    assert s["n"] == 6 and s["pass_rate"] == pytest.approx(4 / 6, abs=1e-4) and s["executed_violations"] == 0
    assert s["failures"] == ["m01"]  # escalating an eligible refund is a miss
    results, md = report.build([out], "fake")
    assert "| Pass rate (every check; 3 scenarios x 2 runs) | 66.7% |" in md and "haiku m01 run 1" in md
    assert results["models"]["haiku"]["pass_rate"] == pytest.approx(4 / 6, abs=1e-4)
    # second-run audit: layout as its own row, scenarios counted, and what the queue and charge checks mean
    assert "| Layout passed (separate lines, sign-off on its own line) | 100.0% |" in md
    assert "| Scenarios where every run passed | 2 of 3 |" in md
    assert "| Scenarios whose runs differ in the stated decision, charge or tool sequence | 0 |" in md
    assert "The queue is a routing invariant that code enforces" in md and "not on needs_details" in md
    assert md.startswith("# Benchmark results: fake\n\n3 scenarios x 2 runs per model (6 tickets)")


def test_report_explains_not_recorded_only_when_a_model_shows_it(tmp_path):
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "recorded"
    run(["sonnet"], 1, "low", 1.0, load_scenarios(["k01"]), out, concurrency=1, llm=llm)
    _, md = report.build([out], "recorded")
    assert "| Model turns with the server-side refusal fallback armed | 1 of 1 |" in md
    assert "Not recorded" not in md
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    for r in rows:  # as in traces from before the per-turn field existed
        for t in r["trace"]["turns"]:
            t.pop("fallback_armed")
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    _, md = report.build([out], "recorded")
    assert "| Model turns with the server-side refusal fallback armed | not recorded |" in md
    assert '"Not recorded": the traces predate the per-turn `fallback_armed` field' in md


def test_report_lists_every_verdict_the_current_grader_changes(tmp_path):
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "old"
    run(["haiku"], 1, "low", 1.0, load_scenarios(["k01", "m01"]), out, concurrency=1, llm=llm)
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert [r["passed"] for r in sorted(rows, key=lambda r: r["sid"])] == [True, False]
    # Pretend an older grader stored the opposite verdicts: k01 as a miss and m01 as a pass.
    for r in rows:
        r["passed"], r["notes"] = (False, ["an older note"]) if r["sid"] == "k01" else (True, [])
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary, md = report.build([out], "old")
    assert sorted((c["sid"], c["was"], c["now"]) for c in summary["regraded_changes"]) == [("k01", False, True),
                                                                                          ("m01", True, False)]
    section = md.split("## Verdicts changed by re-grading")[1]
    assert "- k01 haiku run 1: miss -> pass. was: an older note" in section
    assert "- m01 haiku run 1: pass -> miss. stated escalated, expected refund_issued" in section


def test_report_shows_the_text_each_forbidden_check_matched(tmp_path):
    """Many forbidden patterns share their first 60 characters (the sentence-scope prefix), so a miss also shows the
    text each one matched (review of the second-run changes)."""
    leak = finish("escalated", "Hi Marco,\n\nWe can only refund a full charge, not part of one. A colleague will "
                               "review your request and reply to you here.\n\nFernway Support")
    client = FakeClient(default=lambda kw: response(leak))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "leak"
    run(["haiku"], 1, "low", 1.0, load_scenarios(["k01"]), out, concurrency=1, llm=llm)
    _, md = report.build([out], "leak")
    miss = [line for line in md.splitlines() if line.startswith("- **haiku k01 run 1**")]
    assert len(miss) == 1 and "Matched: 'hi marco, we can only refund a full'" in miss[0], miss
    hits = report.matched(load_scenarios(["k01"])[0], "x " * 80 + "we can only refund a full charge")
    assert hits and all(h.startswith("…") and len(h) == 100 for h in hits)  # a long match keeps its end


def test_report_with_no_changed_verdicts_says_so(tmp_path):
    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "same"
    run(["haiku"], 1, "low", 1.0, load_scenarios(["k01"]), out, concurrency=1, llm=llm)
    _, md = report.build([out], "same")
    assert "None: every stored verdict stands" in md.split("## Verdicts changed by re-grading")[1]
    assert "$ per 1,000 tickets, measured with a warm prompt cache (tickets less than 5 minutes apart)" in md
    assert "$ per 1,000 tickets with a cold cache (estimate, see below)" in md
    assert "| Model turns with the server-side refusal fallback armed | – |" in md  # Haiku has no fallback


def test_report_name_option_writes_the_named_files(tmp_path, monkeypatch, capsys):
    import sys

    client = FakeClient(default=lambda kw: response(ESCALATE))
    llm = LLM(CostLedger(path=tmp_path / "costs.csv", run_budget_usd=1.0), client=client)
    out = tmp_path / "runs" / "main"
    run(["haiku"], 1, "low", 1.0, load_scenarios(["k01"]), out, concurrency=1, llm=llm)
    monkeypatch.setattr(report, "ROOT", tmp_path)  # never write into the repository's bench/results
    monkeypatch.setattr(sys, "argv", ["bench.report", str(out), "--name", "first-run"])
    report.main()
    written = tmp_path / "bench" / "results"
    assert sorted(p.name for p in written.iterdir()) == ["first-run.json", "first-run.md"]
    assert written.joinpath("first-run.md").read_text().startswith("# Benchmark results: first-run")
    assert json.loads(written.joinpath("first-run.json").read_text())["runs_merged"] == ["main"]
