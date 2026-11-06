"""Cost ledger, budget, LLM wrapper and API-key loading. No network."""

from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest

from rta import config
from rta.config import FALLBACK_BETA, FALLBACK_MODELS, MODELS, MissingKeyError, api_key, setting
from rta.ledger import BudgetExceeded, CostLedger, estimate_prompt_tokens, worst_case_usd
from rta.llm import LLM, _BY_ID, billed_usage, call_cost, cost_of, fallback_used
from tests.conftest import FakeClient, bad_request, response, server_error, text, usage


def test_cost_of_uses_every_token_kind():
    u = usage(inp=10_000, out=10_000, cw=10_000, cr=10_000)  # a 30K-token prompt: Haiku's base rate card
    assert cost_of(MODELS["haiku"], u) == pytest.approx(10_000 * (0.10 + 0.50 + 0.125 + 0.01) / 1e6)
    assert cost_of(MODELS["opus"], u) == pytest.approx(10_000 * (4 + 20 + 5 + 0.20) / 1e6)


def test_haiku_long_prompts_use_the_higher_rate_card():
    assert cost_of(MODELS["haiku"], usage(inp=150_000, out=0)) == pytest.approx(150_000 * 0.50 / 1e6)


def test_turns_served_by_a_fallback_model_are_priced_as_that_model():
    assert _BY_ID["claude-opus-4-8"].input == 5.00 and _BY_ID["claude-sonnet-5"].output == 10.00


def test_worst_case_covers_the_prompt_at_the_write_rate_and_full_output():
    assert worst_case_usd(MODELS["sonnet"], 1000, 8192) == pytest.approx((1000 * 2.50 + 8192 * 10) / 1e6)
    assert estimate_prompt_tokens("x" * 3000) == len('"' + "x" * 3000 + '"') // 3 + 300


def test_ledger_records_each_call_and_refuses_past_the_budget(tmp_path):
    client = FakeClient([response(text("ok"), usage_=usage(inp=1000, out=100))] * 2)
    ledger = CostLedger(path=tmp_path / "c.csv", budget_usd=1.0)
    llm = LLM(ledger, client=client)
    result = llm.create(MODELS["haiku"], max_tokens=1000, label="t1", messages=[{"role": "user", "content": "hi"}])
    assert result.cost_usd == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6) and result.model_served
    rows = list(csv.DictReader((tmp_path / "c.csv").open()))
    assert len(rows) == 1 and rows[0]["label"] == "t1" and ledger.reserved_usd == 0
    # a new ledger on the same file starts from the recorded total
    assert CostLedger(path=tmp_path / "c.csv", budget_usd=1.0).total_usd == pytest.approx(result.cost_usd)
    tight = LLM(CostLedger(path=tmp_path / "d.csv", budget_usd=0.001), client=client)
    with pytest.raises(BudgetExceeded):
        tight.create(MODELS["opus"], max_tokens=8192, messages=[{"role": "user", "content": "hi"}])
    assert len(client.requests) == 1


def test_run_budget_is_separate_from_the_file_budget(tmp_path):
    ledger = CostLedger(path=tmp_path / "c.csv", budget_usd=100, run_budget_usd=0.01)
    ledger.reserve(0.009)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(0.002)


def test_reservation_is_released_when_the_call_fails(tmp_path):
    ledger = CostLedger(path=tmp_path / "c.csv", budget_usd=1.0)
    llm = LLM(ledger, client=FakeClient([RuntimeError("network down")]))
    with pytest.raises(RuntimeError):
        llm.create(MODELS["haiku"], max_tokens=100, messages=[])
    assert ledger.reserved_usd == 0 and ledger.total_usd == 0


def test_haiku_never_sends_fallbacks_and_sonnet_and_opus_do(tmp_path):
    client = FakeClient([response(text("ok"))] * 3)
    llm = LLM(None, client=client)
    results = [llm.create(MODELS[key], max_tokens=10, messages=[]) for key in ("haiku", "sonnet", "opus")]
    kinds = [(kind, kw.get("fallbacks"), kw.get("betas")) for kind, kw in client.requests]
    assert kinds == [("messages", None, None), ("beta", "default", [FALLBACK_BETA]), ("beta", "default", [FALLBACK_BETA])]
    assert [r.fallback_armed for r in results] == [False, True, True] and all(r.notes == [] for r in results)


def test_unavailable_fallback_is_switched_off_for_that_model(tmp_path):
    client = FakeClient([bad_request("fallbacks: not enabled for this organization"), response(text("ok")),
                         response(text("ok"))])
    llm = LLM(None, client=client)
    first = llm.create(MODELS["sonnet"], max_tokens=10, messages=[])
    second = llm.create(MODELS["sonnet"], max_tokens=10, messages=[])
    assert [k for k, _ in client.requests] == ["beta", "messages", "messages"]
    # the response came without the fallback, and the call that turned it off says so, once
    assert (first.fallback_armed, second.fallback_armed) == (False, False)
    assert len(first.notes) == 1 and "turned off for Claude Sonnet 5.5" in first.notes[0] and second.notes == []


def test_the_fallback_note_survives_a_failed_retry(tmp_path):
    """The beta is rejected and the retry without it fails: the next call of that model that succeeds carries the
    note, so the trace still says the fallback was turned off (review of the audit changes)."""
    client = FakeClient([bad_request("fallbacks: not available for this API key"), server_error(),
                         response(text("ok")), response(text("ok")), response(text("ok"))])
    llm = LLM(None, client=client)
    with pytest.raises(Exception, match="overloaded"):
        llm.create(MODELS["sonnet"], max_tokens=10, messages=[])
    opus = llm.create(MODELS["opus"], max_tokens=10, messages=[])  # another model: its fallback stays on
    second = llm.create(MODELS["sonnet"], max_tokens=10, messages=[])
    third = llm.create(MODELS["sonnet"], max_tokens=10, messages=[])
    assert [k for k, _ in client.requests] == ["beta", "messages", "beta", "messages", "messages"]
    assert opus.fallback_armed and opus.notes == []
    assert not second.fallback_armed and len(second.notes) == 1 and "turned off for Claude Sonnet 5.5" in second.notes[0]
    assert third.notes == []


def test_other_bad_requests_are_not_swallowed(tmp_path):
    llm = LLM(None, client=FakeClient([bad_request("messages: invalid")]))
    with pytest.raises(Exception, match="invalid"):
        llm.create(MODELS["opus"], max_tokens=10, messages=[])


def test_fallback_detection():
    assert fallback_used(response(SimpleNamespace(type="fallback"), text("x")))
    via_usage = response(text("x"), usage_=usage(iterations=[SimpleNamespace(type="message"),
                                                             SimpleNamespace(type="fallback_message")]))
    assert fallback_used(via_usage) and not fallback_used(response(text("x")))


def attempt(kind: str, model: str | None, inp: int, out: int) -> SimpleNamespace:
    return SimpleNamespace(type=kind, model=model, input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=0, cache_read_input_tokens=0)


def fallback_usage() -> SimpleNamespace:
    """Opus 5.5 declined after 3,000 output tokens; Opus 5 served the turn. Top-level usage is the served attempt."""
    return usage(inp=1000, out=500, iterations=[attempt("message", "claude-opus-5-5", 1000, 3000),
                                                attempt("fallback_message", "claude-opus-5", 1000, 500)])


def test_a_fallback_call_is_costed_per_attempt_at_each_models_rates():
    both = (1000 * 4 + 3000 * 20) / 1e6 + (1000 * 5 + 500 * 25) / 1e6
    assert call_cost(MODELS["opus"], "claude-opus-5", fallback_usage()) == pytest.approx(both)
    summed = billed_usage(fallback_usage())
    assert (summed.input_tokens, summed.output_tokens) == (2000, 3500)
    # An attempt that names no model is priced at the dearest candidate.
    unnamed = usage(inp=0, out=100, iterations=[attempt("message", None, 0, 100),
                                                attempt("fallback_message", "claude-opus-5", 0, 100)])
    assert call_cost(MODELS["opus"], "claude-opus-5", unnamed) == pytest.approx(2 * 100 * 25 / 1e6)
    # Without a fallback, only the top-level usage counts (iterations of one attempt add nothing).
    plain = usage(inp=1000, out=500, iterations=[attempt("message", "claude-opus-5-5", 1000, 500)])
    assert call_cost(MODELS["opus"], "claude-opus-5-5", plain) == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


def test_the_ledger_records_every_attempt_of_a_fallback_call(tmp_path):
    ledger = CostLedger(path=tmp_path / "c.csv", budget_usd=5.0)
    client = FakeClient([response(text("ok"), model="claude-opus-5", usage_=fallback_usage())])
    result = LLM(ledger, client=client).create(MODELS["opus"], max_tokens=4000, messages=[])
    want = (1000 * 4 + 3000 * 20) / 1e6 + (1000 * 5 + 500 * 25) / 1e6
    assert result.cost_usd == pytest.approx(want) and ledger.total_usd == pytest.approx(want)
    row = list(csv.DictReader((tmp_path / "c.csv").open()))[0]
    assert (row["model"], row["output_tokens"]) == ("claude-opus-5", "3500")
    assert result.usage.output_tokens == 3500 and ledger.reserved_usd == 0


def test_the_reservation_covers_a_fallback_attempt_too(tmp_path):
    messages = [{"role": "user", "content": "hi"}]
    prompt = estimate_prompt_tokens(None, None, messages)
    alone = worst_case_usd(MODELS["opus"], prompt, 8192)
    fallback = max(worst_case_usd(m, prompt, 8192) for m in FALLBACK_MODELS.values())
    assert fallback == worst_case_usd(FALLBACK_MODELS["claude-opus-5"], prompt, 8192)  # the dearest fallback
    ledger = CostLedger(path=tmp_path / "c.csv", budget_usd=alone + fallback / 2)
    llm = LLM(ledger, client=FakeClient([response(text("ok"), model="claude-opus-5-5")]))
    assert llm.worst_case(MODELS["opus"], prompt, 8192) == pytest.approx(alone + fallback)
    with pytest.raises(BudgetExceeded):
        llm.create(MODELS["opus"], max_tokens=8192, messages=messages)
    llm._fallback_ok["opus"] = False  # e.g. after the API said the beta isn't enabled for this key
    llm.create(MODELS["opus"], max_tokens=8192, messages=messages)  # the requested model's worst case fits
    assert llm.worst_case(MODELS["haiku"], prompt, 8192) == worst_case_usd(MODELS["haiku"], prompt, 8192)


# --------------------------------------------------------------------------------------------- API key

@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.delenv("RTA_ANTHROPIC_API_KEY", raising=False)
    return path


def test_key_from_the_environment_variable_first(env_file, monkeypatch):
    env_file.write_text("RTA_ANTHROPIC_API_KEY=from-file\n")
    monkeypatch.setenv("RTA_ANTHROPIC_API_KEY", " from-env ")
    assert api_key() == "from-env"


def test_key_from_the_env_file_without_exporting_it(env_file, monkeypatch):
    env_file.write_text("RTA_ANTHROPIC_API_KEY=from-file\nANTHROPIC_API_KEY=other\n")
    assert api_key() == "from-file"
    import os

    assert "RTA_ANTHROPIC_API_KEY" not in os.environ


def test_plain_anthropic_key_in_the_env_file_is_the_last_resort(env_file):
    env_file.write_text("ANTHROPIC_API_KEY=plain\n")
    assert api_key() == "plain"


def test_an_exported_anthropic_key_is_ignored(env_file, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "exported")
    assert api_key() is None
    with pytest.raises(MissingKeyError):
        config.make_client()


def test_optional_settings_come_from_the_environment_then_the_env_file(env_file, monkeypatch, tmp_path):
    import os

    monkeypatch.delenv("RTA_BUDGET_USD", raising=False)
    monkeypatch.delenv("RTA_COSTS_CSV", raising=False)
    assert setting("RTA_BUDGET_USD", "40") == "40"  # no .env yet: the default
    env_file.write_text(f"RTA_BUDGET_USD=5\nRTA_COSTS_CSV={tmp_path / 'from-env-file.csv'}\n")
    assert setting("RTA_BUDGET_USD", "40") == "5" and "RTA_BUDGET_USD" not in os.environ  # read, never exported
    ledger = CostLedger()
    assert ledger.budget_usd == 5.0 and ledger.path == tmp_path / "from-env-file.csv"
    monkeypatch.setenv("RTA_BUDGET_USD", "7")
    assert setting("RTA_BUDGET_USD", "40") == "7" and CostLedger().budget_usd == 7.0  # the environment wins
