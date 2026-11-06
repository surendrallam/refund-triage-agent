"""One place for Claude API calls: budget check, refusal fallback, cost ledger, latency.

Pattern adapted from the author's grounded-rag-assistant repository.

Server-side refusal fallback (Sonnet 5.5 and Opus 5.5): when the requested model declines on a policy classifier,
the API runs the same request on a fallback model within the same call. The top-level `usage` then covers only
the attempt that produced the returned message; `usage.iterations` has one entry per attempt, and each attempt
bills at its own model's rates. So a call is costed per attempt, and the budget reserves the worst case of both
attempts before the call is sent. Each call reports whether the fallback was armed (the beta call path). When the
API rejects the beta, the wrapper turns it off for that model, and the first call of that model that succeeds
afterwards (normally the retry without it) carries a note for the ticket's trace, so a failed retry can't lose it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from rta.config import FALLBACK_BETA, FALLBACK_MODELS, MODELS, ModelSpec, make_client
from rta.ledger import CostLedger, estimate_prompt_tokens, worst_case_usd

_BY_ID: dict[str, ModelSpec] = {m.model_id: m for m in MODELS.values()} | FALLBACK_MODELS
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def cost_of(spec: ModelSpec, usage: Any) -> float:
    """List-price cost of one usage block at one model's rates."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    rates = (spec.input, spec.output, spec.cache_write, spec.cache_read)
    if spec.long_context_from and spec.long_rates and inp + cw + cr > spec.long_context_from:
        rates = spec.long_rates
    return (inp * rates[0] + out * rates[1] + cw * rates[2] + cr * rates[3]) / 1_000_000


def _attempts(usage: Any) -> list[Any]:
    """The per-attempt usage entries of a fallback-served response, or [] when no fallback ran."""
    entries = [e for e in (getattr(usage, "iterations", None) or [])
               if getattr(e, "type", None) in ("message", "fallback_message")]
    return entries if any(getattr(e, "type", None) == "fallback_message" for e in entries) else []


def call_cost(requested: ModelSpec, served_id: str | None, usage: Any) -> float:
    """List-price cost of one call, every attempt included.

    Without a fallback this is the top-level usage at the serving model's rates. After a fallback, each attempt
    in usage.iterations is priced at its own model's rates; an entry that names no model (or a model missing from
    the price table) is priced at the dearest candidate, so the ledger errs high. Whether a declined attempt is
    billed depends on when it declined (Anthropic's refusal billing notes); counting it keeps the budget hard.
    """
    served = _BY_ID.get(served_id or "", requested)
    attempts = _attempts(usage)
    if not attempts:
        return cost_of(served, usage)
    dearest = (requested, served, *FALLBACK_MODELS.values())
    total = 0.0
    for e in attempts:
        spec = _BY_ID.get(getattr(e, "model", None) or "")
        total += cost_of(spec, e) if spec else max(cost_of(s, e) for s in dearest)
    return total


def billed_usage(usage: Any) -> Any:
    """Token counts summed over every attempt of a fallback-served call; the usage itself otherwise."""
    attempts = _attempts(usage)
    if not attempts:
        return usage
    return SimpleNamespace(**{k: sum(int(getattr(e, k, 0) or 0) for e in attempts) for k in _TOKEN_FIELDS})


def fallback_used(response: Any) -> bool:
    """True when the server-side refusal fallback served this response (a fallback block or iteration)."""
    if any(getattr(b, "type", None) == "fallback" for b in (getattr(response, "content", None) or [])):
        return True
    iterations = getattr(getattr(response, "usage", None), "iterations", None) or []
    return any(getattr(e, "type", None) == "fallback_message" for e in iterations)


@dataclass
class CallResult:
    response: Any
    cost_usd: float
    latency_ms: float
    model_served: str
    fallback_used: bool
    usage: Any = None  # token counts billed for the call, every attempt included
    fallback_armed: bool = False  # the request that produced the response carried the refusal fallback (beta path)
    notes: list[str] = field(default_factory=list)  # for the ticket's trace, e.g. the fallback being turned off


class LLM:
    """Wraps the Anthropic client. Every call is checked against the budget and logged."""

    def __init__(self, ledger: CostLedger | None = None, client: Any = None) -> None:
        self.client = client if client is not None else make_client()
        self.ledger = ledger
        self._fallback_ok = {k: v.server_fallback for k, v in MODELS.items()}
        self._fallback_lock = threading.Lock()
        # Trace notes per model, kept until a call of that model succeeds (the note that the fallback was turned off
        # must not be lost when the retry without it fails).
        self._pending_notes: dict[str, list[str]] = {}

    def worst_case(self, spec: ModelSpec, prompt_tokens: int, max_tokens: int) -> float:
        """The most one call can cost: the requested model's worst case, plus the dearest fallback model's when a
        server-side fallback can run (a declined attempt and the fallback attempt can both bill)."""
        worst = worst_case_usd(spec, prompt_tokens, max_tokens)
        if self._fallback_ok.get(spec.key):
            worst += max(worst_case_usd(m, prompt_tokens, max_tokens) for m in FALLBACK_MODELS.values())
        return worst

    def create(self, spec: ModelSpec, *, max_tokens: int, label: str = "", **kwargs: Any) -> CallResult:
        reserved = 0.0
        if self.ledger is not None:
            prompt = estimate_prompt_tokens(kwargs.get("system"), kwargs.get("tools"), kwargs.get("messages"))
            reserved = self.ledger.reserve(self.worst_case(spec, prompt, max_tokens))
        started = time.perf_counter()
        try:
            response, armed, notes = self._send(spec, max_tokens=max_tokens, **kwargs)
        except BaseException:
            if self.ledger is not None:
                self.ledger.release(reserved)
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        served = getattr(response, "model", None) or spec.model_id
        cost = call_cost(spec, served, response.usage)
        usage = billed_usage(response.usage)
        if self.ledger is not None:
            with self.ledger.labelled(label):
                self.ledger.record(served, usage, cost, latency_ms, getattr(response, "stop_reason", None), reserved)
        return CallResult(response, cost, latency_ms, served, fallback_used(response), usage, armed, notes)

    def _send(self, spec: ModelSpec, **kwargs: Any) -> tuple[Any, bool, list[str]]:
        """The response, whether the refusal fallback was armed for it, and notes for the trace."""
        import anthropic

        kwargs["model"] = spec.model_id
        if self._fallback_ok.get(spec.key):
            try:
                # Server-side refusal fallback: if the model declines on a policy classifier, the API retries
                # the same request on a fallback model within the same call ("default" picks the model by
                # refusal category). Claude Haiku 5.5 has no server-side fallback, so it never takes this path.
                response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
                return response, True, []
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                with self._fallback_lock:  # not available for this key; carry on without it, and say so once
                    if self._fallback_ok.get(spec.key, False):
                        self._pending_notes.setdefault(spec.key, []).append(
                            f"server-side refusal fallback turned off for {spec.label}: the API rejected it "
                            f"({type(e).__name__}: {str(e)[:200]}); later calls go without it")
                    self._fallback_ok[spec.key] = False
        response = self.client.messages.create(**kwargs)
        with self._fallback_lock:  # the call succeeded, so the notes reach a trace now
            notes = self._pending_notes.pop(spec.key, [])
        return response, False, notes
