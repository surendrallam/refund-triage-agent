"""Shared fixtures: the fictional billing data, the knowledge base, and a fake Anthropic client.

Nothing here calls the network. The fake client returns scripted responses shaped like Messages API responses
(content blocks with a type, stop_reason, model, usage) and records a deep copy of every request.
"""

from __future__ import annotations

import copy
import itertools
from datetime import date
from types import SimpleNamespace
from typing import Any, Callable

import anthropic
import httpx2
import pytest

from rta import config
from rta.billing import Billing
from rta.kb import KB
from rta.tools import Sandbox, Ticket, Toolbox

_ids = itertools.count(1)


@pytest.fixture(scope="session")
def billing() -> Billing:
    return Billing.load()


@pytest.fixture(scope="session")
def kb() -> KB:
    return KB.load()


@pytest.fixture(autouse=True)
def _isolated_costs(tmp_path, monkeypatch):
    """Every test writes its cost ledger to a temporary file, never to runs/costs.csv, and settings read at run
    time never come from a developer's own .env file (tests that need one point ENV_FILE at a temporary file)."""
    monkeypatch.setenv("RTA_COSTS_CSV", str(tmp_path / "costs.csv"))
    monkeypatch.delenv("RTA_BUDGET_USD", raising=False)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "absent.env")


def ticket(sender: str, today: str | date, tid: str = "T-test", subject: str = "Refund",
           body: str = "Please refund my last invoice.") -> Ticket:
    return Ticket(tid, sender, subject, body, date.fromisoformat(today) if isinstance(today, str) else today)


def toolbox(billing: Billing, kb: KB, sender: str, today: str) -> Toolbox:
    return Toolbox(billing, kb, ticket(sender, today), Sandbox())


def usage(inp: int = 1200, out: int = 150, cw: int = 0, cr: int = 0, iterations: Any = None) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=cw,
                           cache_read_input_tokens=cr, iterations=iterations)


def tool_use(name: str, input: dict, id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id or f"toolu_{next(_ids):04d}", name=name, input=input)


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t, citations=None)


def thinking(t: str = "") -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=t, signature="sig-abc")


def response(*blocks: Any, stop_reason: str | None = None, model: str = "claude-haiku-5-5",
             usage_: Any = None) -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(getattr(b, "type", "") == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, model=model, usage=usage_ or usage())


def finish(decision: str, reply: str, charge_id: str | None = None, amount: float | None = None,
           note: str = "test") -> SimpleNamespace:
    return tool_use("finish", {"decision": decision, "charge_id": charge_id, "amount": amount,
                               "reply_to_customer": reply, "internal_note": note})


def bad_request(message: str) -> anthropic.BadRequestError:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(message, response=httpx2.Response(400, request=req),
                                     body={"error": {"message": message}})


def server_error(message: str = "overloaded") -> anthropic.InternalServerError:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.InternalServerError(message, response=httpx2.Response(500, request=req), body=None)


class FakeClient:
    """Stands in for anthropic.Anthropic. Each scripted item is a response, an exception to raise, or a
    callable that gets the request kwargs and returns one of those. A `default` callable answers once the
    script runs out."""

    def __init__(self, responses: list | None = None, default: Callable[[dict], Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.requests: list[tuple[str, dict]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(("messages", copy.deepcopy(kwargs)))
        return self._next(kwargs)

    def _beta_create(self, **kwargs: Any) -> Any:
        self.requests.append(("beta", copy.deepcopy(kwargs)))
        return self._next(kwargs)

    def _next(self, kwargs: dict) -> Any:
        if self.responses:
            item = self.responses.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("FakeClient ran out of scripted responses")
        if callable(item) and not isinstance(item, BaseException):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def kwargs(self) -> list[dict]:
        return [k for _, k in self.requests]
