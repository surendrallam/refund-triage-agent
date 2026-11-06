# refund-triage-agent

A prototype agent for one business process: refund requests that arrive by email. Claude reads the email, looks
up the customer and the charge, and writes the reply. Code decides eligibility, amounts, identity and limits, and
anything over the limit or outside the policy goes to a person.

**The model does the language. Code does the rules.**

The company (Fernway, a project planning, time tracking and invoicing app), its customers, its policies and every
address in this repository are fictional.

## What this prototype covers

- **The workflow**: how a support agent handles refund emails today, and the part the prototype automates
  ([The workflow](#the-workflow)).
- **When to use AI and when to use code**: each step, who does it, and why
  ([AI or code, step by step](#ai-or-code-step-by-step)).
- **A simple agent architecture**: one model, seven strict tools, reconciliation in code
  ([Architecture](#architecture)).
- **Tools and APIs**: two systems behind the tools, a billing system and the public help documents
  ([`rta/tools.py`](rta/tools.py)).
- **Tool calling**: the Messages API tool-use loop with a turn cap ([`rta/agent.py`](rta/agent.py)).
- **Tests on representative scenarios**: 30 benchmark tickets with deterministic grading, and an offline test suite
  ([The benchmark](#the-benchmark), [`tests/`](tests/)).
- **Limitations and failure cases**: including the failures code can't catch ([Limits](#limits)).
- **A production plan and a next-phase estimate**: [docs/PRODUCTION.md](docs/PRODUCTION.md), with an example
  estimate.

## The workflow

Today a support agent reads each refund email, finds the customer and the charge in the billing system, checks the
refund policy, then issues the refund, declines with the reason, asks for missing details, or escalates. Most of
that is reading and writing. A small part is rules that must be exact: day windows, amounts, who may ask, the
approval limit, accounts with an open chargeback.

The prototype automates the reading, the lookups and the drafting. The rules stay in code, with tests at their
boundaries. The agent works against a sandbox: each ticket's refunds and approvals go to that ticket's own ledger,
nothing changes the billing data, and no money moves.

## AI or code, step by step

| Workflow step | Today | In the prototype | Why |
|---|---|---|---|
| Read the email and work out what is asked | Support agent | **Model** | Free text: tone, typos, several asks, missing details. |
| Know who is asking | Agent checks the address | **Code** | The tools act only for the helpdesk's verified sender. Every lookup and action stays inside that sender's account, whatever the email says, and lookups say nothing about other accounts. |
| Find the customer and the charge | Agent searches billing | **Model asks, code answers** | Matching "my November invoice" to a charge is language; the data comes only from billing. |
| Decide eligibility | Agent reads the policy | **Code** (`rta/policy.py`) | Day windows, "most recent charge" and charge status must be exact and testable at day 7, 8, 30 and 31. |
| Decide the amount | Agent | **Code** | Only the full amount of the charge. The model can't change it. |
| Apply the approval limit, chargeback and currency rules | Agent follows the internal procedure | **Code** | Money and risk rules, staff only. Code routes to the approval, Risk or Tier 2 queue. |
| Issue the refund or send it for approval | Agent | **Model requests, code executes** | The tools check every rule again, and that the charge is one the email names, and refuse anything else. |
| Spot what the policy doesn't cover: partial amounts, credits, disputes, several charges | Agent's judgement | **Model** chooses "escalated"; **code** routes it | Recognising these needs language. Deciding them needs a person. |
| Write the reply | Agent | **Model** | Language, grounded in the rules' answer and the policy text. |
| Keep internal roles, queues and limits out of the reply | Agent's care | **Code** (a pattern check) | A reply that names them, or uses one of the paraphrases its tests cover, is withheld and the ticket goes to a person. |
| Check that what the agent said matches what it did | Nobody | **Code** | The model's summary is a claim; the sandbox ledger is the record. A mismatch becomes an escalation. |
| Log each refund with the ticket number and the reason | Agent | **Code** | Audit trail. |

## Architecture

```
 email + helpdesk ticket details (verified sender, date)
        |
        v
 +-----------------------+   tool calls    +--------------------------------------------------+
 | Claude                | --------------> | rta/tools.py (bound to the verified sender)      |
 | Messages API, strict  |                 |   find_customer, list_charges   -> billing data  |
 | tools, <= 8 turns     | <-------------- |   check_refund_eligibility      -> rta/policy.py |
 +-----------+-----------+   JSON results  |   search_policy                 -> kb/*.md       |
             |                             |   issue_refund, send_for_approval                |
             | finish(decision, reply)     |     -> named invoice and policy checked again    |
             |                             |     -> sandbox ledger (this ticket only)         |
             v                             +--------------------------------------------------+
 +----------------------------------------+
 | rta/reconcile.py: finish vs. ledger,   |
 | decline backed by the rules, reply     | --> final decision, queue, reply, flags, JSON log
 | guard, independent audit of actions    |
 +----------------------------------------+
```

- **Tools** (`rta/tools.py`): seven small JSON schemas with `strict: true`. `check_refund_eligibility` returns the
  code's verdict: eligible or not, a reason code, the only refundable amount, whether a person must approve it, and
  the decision the ticket should end with.
- **Rules** (`rta/policy.py`): the 2026 refund policy and the internal refund procedure as plain functions.
- **Loop** (`rta/agent.py`): at most 8 model turns, then the code escalates. Effort `low` by default. The system
  prompt and tools are cached; the ticket's date and verified sender travel in a separate system message.
- **Reconciliation** (`rta/reconcile.py`): after `finish`, code compares the stated decision with the ledger and the
  rules. Anything inconsistent becomes an escalation with a flag, and the model's reply is not sent.
- **The trace records every step**: tool calls, results, tokens, cost and latency, one JSON file per ticket.

## Try it

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
source .venv/bin/activate
python -m pytest -q                                            # offline: no key, no network

rta charges --from ana@blueheron.example --today 2026-11-04    # what the rules say about each charge (free)
rta scenarios                                                  # the 30 benchmark tickets

cp .env.example .env                                           # paste your Claude API key after RTA_ANTHROPIC_API_KEY=
rta triage --scenario m01                                      # one benchmark ticket through the agent, graded
rta triage --from omar@kestrelworks.example --subject "Refund" \
  --body "Please refund invoice FW-10041, our annual plan." --today 2026-11-06 --model sonnet
```

`rta triage` prints the trace (every tool call and result, tokens, cost, latency), the model's decision, the final
decision after reconciliation, what the tools actually did, and the reply. It writes the full trace to
`logs/<ticket>.json`, also when the budget stops a ticket part way, and never sends a call whose worst case could
pass `--budget` (default $0.50). A ticket costs a fraction of a cent on Claude Haiku 5.5 (an estimate; see
[docs/RUNBOOK.md](docs/RUNBOOK.md), which also explains why long Opus tickets need `--budget 1`).

## The benchmark

30 tickets in [`bench/scenarios.yaml`](bench/scenarios.yaml), each with the date it arrived, the sender, the email
and the expected outcome. For 24 of the 30 tickets the test suite checks the expected outcome against
`rta/policy.py`. The other 6 (partial amount, disputed charge, goodwill credit, missing invoice, two billing
questions) depend on what the email asks: the tests check their sender, routing and candidate charges, and
DECISIONS.md (gaps 4, 12 and 13) records each choice. Each ticket also carries correct replies in other words that
must pass its reply checks, and wrong replies that must fail them.

| Category | Tickets | What they test |
|---|---|---|
| Monthly charges | 5 | eligible, day 7 and day 8, not the most recent charge, over the limit |
| Annual plans | 5 | under and over the limit, day 30 and day 31, after 30 days (auto-renewal) |
| Seat additions | 4 | within and after 30 days, over the limit, on a monthly plan (policy gap) |
| Nothing to refund | 2 | already refunded, failed charge |
| Routed by the rules | 2 | open chargeback (Risk), charge in EUR (Tier 2) |
| Identity | 2 | a request about someone else's account, an unknown sender |
| Judgement on the request | 4 | no invoice number with two candidates, partial amount, disputed charge, goodwill credit |
| Traps | 4 | the superseded 14-day policy, a forum's "60 days", a prompt injection, an angry but eligible customer |
| Not refund requests | 2 | a billing question, a refund-policy question |

Grading is deterministic, with no LLM judge (`bench/grade.py`). Per ticket: the decision the model stated, the
outcome the tools enforced, the amount, the charge (on refunds, approvals, declines and escalations that name one;
not on needs_details), the reply checks (required facts, forbidden phrases, internal terms, and on a declined charge
the refund window's last day), the reply's layout (line breaks, with the Fernway Support sign-off on its own line),
tools that must not be called, and consistency. The queue is checked too, but code sets it (`escalation_route()` at
reconciliation, and `send_for_approval` accepts only the approval queue), so it is a routing invariant the grader
confirms, not a model skill. Per model, over 30 scenarios x 3 runs: pass rate, decision accuracy, reply-check and
layout pass rates, refused attempts per 100 tickets, executed violations (must be 0), tool calls per ticket, $ per
1,000 tickets (measured with a warm prompt cache, plus a cold-cache estimate), latency p50/p95, model turns sent with
the refusal fallback armed, and tickets that ignored the helpdesk's verified sender. The runs of one scenario are far
from independent, so the report also counts scenarios where every run passed and scenarios whose runs differ in
decision, charge or tool sequence. The report grades every stored trace again with the current grader, lists each
verdict that changed since the run, and shows for each miss the text each forbidden check matched.

```bash
python -m bench.run --models haiku,sonnet,opus --runs 3 --budget 7.5 --concurrency 4 --name my-run
python -m bench.report runs/my-run     # writes bench/results/my-run.md and .json (--name NAME to rename)
```

Give each run its own `--name` (without one, the name gets a timestamp): `bench.run` refuses to overwrite the stored
traces of an earlier run.

<!-- RESULTS -->
### Results

30 scenarios x 3 runs per model, run on 6 Nov 2026 ($2.56 of API spend) and graded without an LLM judge. Every
number and every miss is in [bench/results/third-run.md](bench/results/third-run.md).

| | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
|---|---|---|---|
| Pass rate (every check) | 100% (90 of 90) | 100% (90 of 90) | 98.9% (89 of 90) |
| Scenarios where all 3 runs passed | 30 of 30 | 30 of 30 | 29 of 30 |
| Right decision, charge and amount | 90 of 90 | 90 of 90 | 90 of 90 |
| Executed rule violations | 0 | 0 | 0 |
| API cost per 1,000 tickets, warm cache (measured) | $0.60 | $9.22 | $18.62 |
| API cost per 1,000 tickets, cold cache (estimate) | $1.06 | $18.90 | $38.19 |
| Latency per ticket, p50 / p95 | 5.6 / 8.3 s | 7.2 / 10.2 s | 8.5 / 11.7 s |

- **This is the third run.** Claude agents audited each run before publishing. Each audit found replies the grader
  had passed, so the agent and the grader were fixed twice (DECISIONS.md, "Audit of the first run" and "Audit of the
  second run"). Re-graded with today's grader, the first run scores Haiku 82, Sonnet 66 and Opus 90 of 90, and the
  second 87, 69 and 90. Most of Sonnet's misses there are replies written on one line. The third run's audit found
  no wrong decision and one reply to fix in the grader ("Audit of the third run").
- **The one graded miss:** Opus told a prospective customer "If you approve a refund, it goes back to ..." (q02 run 2).
- **What the checks don't grade.** The third run's audit recorded 7 replies a support lead would still edit:
  - 4 of 9 replies on the other-account ticket (x01) said something about that account that no tool established
    (Haiku 3, Sonnet 1);
  - one Haiku reply explained how to replace a card, which no document covers (q01);
  - one Opus reply mentioned an approval step the public policy doesn't (q02);
  - one Haiku reply read as if the annual term had ended (a05).

  60 of 90 Sonnet replies also separate their paragraphs with single line breaks: fine as plain text, joined into one
  block by a helpdesk that renders Markdown.
- **Cost.** Haiku passed the same checks as Sonnet at about a fifteenth of the cost on this benchmark. Runs 2 and 3
  of each scenario reuse run 1's email from the prompt cache, which makes the warm figures about 3% low ($0.62,
  $9.52 and $19.23 without it).

## Limits

- One fictional workflow with a small policy and 30 tickets. A real client's process needs its own rules,
  data and ticket set before any number here transfers.
- Sandbox only: no helpdesk or billing integration, and payouts are not modelled.
  [docs/PRODUCTION.md](docs/PRODUCTION.md) lists what changes for production, with an example estimate.
- One charge per ticket, one email per ticket (no follow-up conversation), English only, no attachments.
- Identity is the sender address the helpdesk passes in. Checking that the address is genuine (SPF, DKIM, signed-in
  users) is the helpdesk's job; this prototype doesn't do it.
- **Failure cases the code can't catch:**
  - The model misreads the request and the rules allow what it does. Example: the customer asks for half of a
    charge (k01) and the model refunds all of it, or the email names no invoice and the model refunds the wrong one
    of two eligible charges (n01). Every code check and the audit pass; only the benchmark's forbidden-tool check
    catches it. When the email does name an invoice, the code refuses an action on any other charge.
  - Each ticket starts from the fixture's state. A refund the agent issues goes to that ticket's trace only, and
    nothing writes it back to `data/billing.json`, so a second email about the same charge would get a second refund.
    Production checks the billing provider and uses idempotency keys ([docs/PRODUCTION.md](docs/PRODUCTION.md)).
  - A refund can run while its reply is withheld: the model's finish doesn't match what the tools did, or the budget
    stops the ticket after the refund. The ticket goes to a person with the refund on record, and that person must
    follow up with the customer.
- The reply guard is a list of patterns. It catches the internal names and the paraphrases listed in its tests
  (`tests/test_policy.py`), not every wording a model could invent. The same holds for the grader's reply checks:
  each catches the wordings its tests list.
- Some facts are not in the documents, so no reply check can grade them: what cancelling does to an unpaid, failed
  invoice and its retries, what a refund does to the plan and its renewal, and how a customer replaces the card on
  file (DECISIONS.md, gaps 16 to 18). The prompt tells the model to state only facts a tool or a document gives, but
  nothing checks that it does. In the first run, before that rule, five r02 replies said that cancelling stops the
  retries; in the second run, after it, four Haiku replies still stated such facts (a03 run 3, r02 run 2, q01 runs 1
  and 2), and two more (x01 runs 2 and 3) broke the line against saying anything about another account
  (DECISIONS.md, "Audit of the second run"). The third run's audit recorded the same kinds again ([Results](#results)).
- The system prompt briefs the model on the superseded 14-day policy and the forum's 60 days. Tickets o01 and f01
  test whether it applies that briefing, not whether it would spot such claims unaided.
- The reply checks look for required facts and forbidden claims. They can't judge tone.
- Costs in this README are estimates. A benchmark report measures them with a warm prompt cache (tickets seconds
  apart) and adds a cold-cache estimate, the closer figure for emails that arrive more than 5 minutes apart.

## Repository map

| Path | What it is |
|---|---|
| `rta/policy.py` | The rules: identity, eligibility, amounts, limits, routing, the reply guard |
| `rta/tools.py` | Tool schemas and the guarded code that runs them; the sandbox ledger |
| `rta/agent.py` | System prompt, the tool-use loop, the trace |
| `rta/reconcile.py` | Reconciliation of the finish with the ledger, and the audit |
| `rta/billing.py`, `data/billing.json` | The fictional billing system: 18 customers, 32 charges, one open chargeback |
| `rta/kb.py`, `kb/` | BM25 search over three public help documents |
| `rta/llm.py`, `rta/ledger.py`, `rta/config.py` | API calls with a hard budget, cost ledger, refusal fallback, prices, key loading |
| `bench/` | Scenarios, grader, runner, report |
| `DECISIONS.md` | Every design choice and every policy gap, with the choice made |
| `docs/PRODUCTION.md`, `docs/RUNBOOK.md` | The road to production; how to run everything |

MIT licence.
