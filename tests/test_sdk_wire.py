"""The real anthropic SDK client, served by an in-process mock transport (no network).

This checks what the fake client can't: that the SDK accepts the request the agent builds (strict tools, adaptive
thinking with summarized display, effort, top-level cache_control, a mid-conversation system message, the
fallback beta) and that response blocks parsed by the SDK go back on the wire unchanged in the next request.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from rta import agent, policy
from rta.agent import triage
from rta.config import FALLBACK_BETA
from rta.ledger import CostLedger
from rta.llm import LLM
from tests.conftest import ticket

USAGE = {"input_tokens": 50, "output_tokens": 40, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0}


def message(model: str, content: list[dict], stop_reason: str = "tool_use") -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": USAGE}


def tool(id_: str, name: str, input_: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


@pytest.fixture(autouse=True)
def _fresh_system_message_cache(monkeypatch):
    monkeypatch.setattr(agent, "_SYSTEM_MESSAGES_OK", {})


def client_with(script: list[dict], seen: list[httpx2.Request]) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=script[len(seen) - 1])

    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    return anthropic.Anthropic(api_key="test-key-not-real", base_url="https://api.anthropic.com", http_client=http,
                               max_retries=0)


@pytest.mark.parametrize("model,model_id,beta", [("haiku", "claude-haiku-5-5", False),
                                                 ("opus", "claude-opus-5-5", True)])
def test_the_sdk_accepts_the_request_and_round_trips_blocks(billing, kb, tmp_path, model, model_id, beta):
    reply = ("Hi Ana,\n\nI've refunded invoice FW-10013 ($190.00). It goes back to your original payment method "
             "within 5 to 10 business days.\n\nFernway Support")
    script = [
        message(model_id, [{"type": "thinking", "thinking": "Look up the sender.", "signature": "c2lnLTE="},
                           tool("toolu_01", "find_customer", {"email": "ana@blueheron.example"})]),
        message(model_id, [tool("toolu_02", "list_charges", {"customer_id": "cus_101"})]),
        message(model_id, [tool("toolu_03", "check_refund_eligibility", {"charge_id": "ch_1013"})]),
        message(model_id, [tool("toolu_04", "issue_refund", {"charge_id": "ch_1013", "amount": 190.0,
                                                              "reason": "Within 7 days"})]),
        message(model_id, [{"type": "text", "text": "Done."},
                           tool("toolu_05", "finish", {"decision": "refund_issued", "charge_id": "ch_1013",
                                                       "amount": 190.0, "reply_to_customer": reply,
                                                       "internal_note": "monthly_within_7_days"})]),
    ]
    seen: list[httpx2.Request] = []
    llm = LLM(CostLedger(path=tmp_path / "c.csv", budget_usd=5), client=client_with(script, seen))
    trace = triage(ticket("ana@blueheron.example", "2026-11-04", body="Please refund FW-10013."), llm, billing, kb,
                   model=model)

    assert trace.final.consistent and trace.enforced == policy.REFUND_ISSUED and len(seen) == 5
    bodies = [json.loads(r.content) for r in seen]
    first = bodies[0]
    assert first["model"] == model_id and first["cache_control"] == {"type": "ephemeral"}
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"] == {"effort": "low"} and all(t["strict"] for t in first["tools"])
    assert [m["role"] for m in first["messages"]] == ["user", "system"]
    if beta:
        assert first["fallbacks"] == "default" and FALLBACK_BETA in seen[0].headers.get("anthropic-beta", "")
        assert seen[0].url.path == "/v1/messages"
    else:
        assert "fallbacks" not in first and "anthropic-beta" not in seen[0].headers

    # The thinking block and the tool call go back exactly as the API sent them.
    echoed = bodies[1]["messages"][2]
    assert echoed["role"] == "assistant"
    assert echoed["content"][0] == {"type": "thinking", "thinking": "Look up the sender.", "signature": "c2lnLTE="}
    assert echoed["content"][1] == tool("toolu_01", "find_customer", {"email": "ana@blueheron.example"})
    result = bodies[1]["messages"][3]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_01"
    # Each request extends the previous one: the history is append-only on the wire too.
    for earlier, later in zip(bodies, bodies[1:], strict=False):
        assert later["messages"][:len(earlier["messages"])] == earlier["messages"]
    assert trace.cost_usd > 0 and trace.turns[0].thinking == "Look up the sender."
