"""Settings: paths, models, prices, agent defaults and API-key loading.

The ModelSpec / price table / key-loading pattern is adapted from the author's grounded-rag-assistant repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BILLING_PATH = Path(os.environ.get("RTA_BILLING_JSON", ROOT / "data" / "billing.json"))
KB_DIR = Path(os.environ.get("RTA_KB_DIR", ROOT / "kb"))
SCENARIOS_PATH = ROOT / "bench" / "scenarios.yaml"
RUNS_DIR = ROOT / "runs"
LOGS_DIR = ROOT / "logs"
ENV_FILE = ROOT / ".env"


def setting(name: str, default: str | None = None) -> str | None:
    """An optional setting (RTA_MODEL, RTA_EFFORT, RTA_TODAY, RTA_BUDGET_USD, RTA_COSTS_CSV).

    Order: the environment, then the project's .env file, then the default. The .env file is read directly with
    dotenv_values and never loaded into the environment (the same way api_key() reads it).
    """
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    if ENV_FILE.exists():
        from dotenv import dotenv_values

        value = dotenv_values(ENV_FILE).get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


# The sample billing data is frozen in time, so "today" defaults to a fixed date. This keeps demos reproducible.
TODAY = setting("RTA_TODAY", "2026-11-06")

API_BASE_URL = "https://api.anthropic.com"
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # gates fallbacks="default" (the array form uses another header)


@dataclass(frozen=True)
class ModelSpec:
    key: str  # short name used on the CLI and in results: haiku | sonnet | opus
    model_id: str
    label: str
    input: float  # USD per million input tokens
    output: float  # USD per million output tokens
    cache_write: float  # USD per million tokens written to the 5-minute cache
    cache_read: float  # USD per million tokens read from the cache
    server_fallback: bool  # supports the server-side refusal fallback parameter
    long_context_from: int | None = None  # prompt size (tokens) where a higher rate card starts
    long_rates: tuple[float, float, float, float] | None = None  # input, output, write, read


# Anthropic list prices for the Claude API, USD per million tokens, checked 6 Nov 2026 at
# https://platform.claude.com/docs/en/about-claude/pricing
# Cache writes are 1.25x input (5-minute TTL). Cache reads are 0.1x input, except 0.05x on Sonnet 5.5 and
# Opus 5.5 ($0.10 and $0.20). Check the page again before a paid run.
MODELS: dict[str, ModelSpec] = {
    "haiku": ModelSpec(
        "haiku", "claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50, 0.125, 0.01, False,
        long_context_from=100_000, long_rates=(0.50, 2.50, 0.625, 0.05),
    ),
    "sonnet": ModelSpec("sonnet", "claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00, 2.50, 0.10, True),
    "opus": ModelSpec("opus", "claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00, 5.00, 0.20, True),
}

# Models the server-side refusal fallback can hand a turn to. Not selectable on the CLI; listed so that a
# turn served by one of them is costed at its own list price (Anthropic's model documentation).
FALLBACK_MODELS: dict[str, ModelSpec] = {
    "claude-opus-5": ModelSpec("opus-5", "claude-opus-5", "Claude Opus 5", 5.00, 25.00, 6.25, 0.50, False),
    "claude-opus-4-8": ModelSpec("opus-4-8", "claude-opus-4-8", "Claude Opus 4.8", 5.00, 25.00, 6.25, 0.50, False),
    "claude-sonnet-5": ModelSpec("sonnet-5", "claude-sonnet-5", "Claude Sonnet 5", 2.00, 10.00, 2.50, 0.20, False),
}

DEFAULT_MODEL = setting("RTA_MODEL", "haiku")
DEFAULT_EFFORT = setting("RTA_EFFORT", "low")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TURNS = 8  # model turns per ticket; after that the code escalates the ticket
MAX_TOKENS = 8192  # per model turn: room for adaptive thinking, a tool call and the reply


class MissingKeyError(RuntimeError):
    pass


def api_key() -> str | None:
    """Find the Claude API key without exporting anything to the shell.

    Order: the RTA_ANTHROPIC_API_KEY environment variable, then RTA_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY in
    the project's .env file. The .env file is read directly with dotenv_values and never loaded into the
    environment. An ANTHROPIC_API_KEY exported in the shell is deliberately not read.
    """
    key = os.environ.get("RTA_ANTHROPIC_API_KEY")
    if key and key.strip():
        return key.strip()
    if ENV_FILE.exists():
        from dotenv import dotenv_values

        values = dotenv_values(ENV_FILE)
        key = values.get("RTA_ANTHROPIC_API_KEY") or values.get("ANTHROPIC_API_KEY")
        if key and key.strip():
            return key.strip()
    return None


def make_client(max_retries: int = 5, timeout: float = 120.0):
    """Anthropic client pinned to the public API endpoint and the project's key."""
    import anthropic

    key = api_key()
    if not key:
        raise MissingKeyError(
            "No API key found. Put RTA_ANTHROPIC_API_KEY=... in the project's .env file (see .env.example)."
        )
    base_url = os.environ.get("RTA_ANTHROPIC_BASE_URL", API_BASE_URL)
    return anthropic.Anthropic(api_key=key, base_url=base_url, max_retries=max_retries, timeout=timeout)
