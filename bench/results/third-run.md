# Benchmark results: third-run

30 scenarios x 3 runs per model (90 tickets), effort `low`. Runs: third-run; API spend $2.56. Complete: True.

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 |
|---|---|---|---|
| Pass rate (every check; 30 scenarios x 3 runs) | 100.0% | 100.0% | 98.9% |
| Decision accuracy (what the model stated) | 100.0% | 100.0% | 100.0% |
| Outcome accuracy (what the tools did) | 100.0% | 100.0% | 100.0% |
| Reply checks passed | 100.0% | 100.0% | 98.9% |
| Layout passed (separate lines, sign-off on its own line) | 100.0% | 100.0% | 100.0% |
| Refused attempts per 100 tickets | 0.0 | 0.0 | 0.0 |
| Executed violations (must be 0) | 0 | 0 | 0 |
| Inconsistent finishes escalated by code | 0 | 0 | 0 |
| Turn cap reached | 0 | 0 | 0 |
| Stopped by the budget (not graded) | 0 | 0 | 0 |
| Ignored the verified sender (see below) | 0 | 0 | 0 |
| Tool calls per ticket | 4.0 | 3.9 | 3.9 |
| $ per 1,000 tickets, measured with a warm prompt cache (tickets less than 5 minutes apart) | $0.60 | $9.22 | $18.62 |
| $ per 1,000 tickets with a cold cache (estimate, see below) | $1.06 | $18.90 | $38.19 |
| Latency p50 (whole ticket) | 5.6 s | 7.2 s | 8.5 s |
| Latency p95 (whole ticket) | 8.3 s | 10.2 s | 11.7 s |
| Tickets whose first turn read the prompt cache | 86 of 90 | 88 of 90 | 89 of 90 |
| Model turns with the server-side refusal fallback armed | – | 347 of 347 | 347 of 347 |

- **Scenarios, not independent tickets.** Every pass rate here is over 30 scenarios x 3 runs per model. The runs of one scenario are far from independent ("By scenario" below shows how often they differ), so the evidence is closer to 30 cases than to 90, and the scenarios are designed cases, not a sample of a client's email.
- **What is graded.** The charge is graded on refunds, approvals, declines and escalations that name a charge, not on needs_details. The queue is a routing invariant that code enforces (escalation_route() at reconciliation; send_for_approval accepts only the billing_lead queue), not a model check: it can fail only together with the decision or consistency. Layout counts toward the pass rate: a reply passes only with line breaks and the Fernway Support sign-off on its own line.
- **Cost.** The benchmark sends tickets seconds apart, so a ticket's first turn reads the cached system prompt and tools that an earlier ticket wrote ("Tickets whose first turn read the prompt cache"), and the measured cost is a warm-cache cost. The cold-cache row is an estimate from the same recorded usage, with the first turn's cache reads re-priced at the cache-write rate, as for a ticket that arrives more than 5 minutes after the last one. Later turns stay as recorded. Runs 2 and 3 of a scenario send the same email as run 1, so their first turn also reads that email from the cache, which a client's distinct emails would not; the warm figure is a few percent low for that reason (DECISIONS.md, "Audit of the third run", gives the estimate).
- **Refusal fallback.** Sonnet 5.5 and Opus 5.5 requests are sent with the server-side refusal fallback unless the API rejects the beta; Haiku 5.5 has none (–).
- **Ignored the verified sender.** Tickets from a known customer's address that ended with a decision other than not_refund_request without a find_customer call.
- **Re-grading.** Every verdict is the current grader's. What reconciliation decided at run time (flags, final queue, enforced outcome) stays as the run's code recorded it, and so does the "Inconsistent finishes" count.

## By scenario

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 |
|---|---|---|---|
| Scenarios where every run passed | 30 of 30 | 30 of 30 | 29 of 30 |
| Scenarios whose runs differ in the stated decision, charge or tool sequence | 4: a05, f01, k01, q01 | 1: d01 | 1: s03 |

## By category

Pass rate per category, over each category's scenarios x 3 runs.

| Category | Scenarios | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 |
|---|---|---|---|---|
| Monthly charges | 5 | 100.0% | 100.0% | 100.0% |
| Annual plans | 5 | 100.0% | 100.0% | 100.0% |
| Seat additions | 4 | 100.0% | 100.0% | 100.0% |
| Nothing to refund (failed, already refunded) | 2 | 100.0% | 100.0% | 100.0% |
| Routed to people by the rules (chargeback, currency) | 2 | 100.0% | 100.0% | 100.0% |
| Identity (other account, unknown sender) | 2 | 100.0% | 100.0% | 100.0% |
| Judgement on the request (partial, dispute, goodwill, missing invoice) | 4 | 100.0% | 100.0% | 100.0% |
| Traps (old policy, forum claim, prompt injection, angry customer) | 4 | 100.0% | 100.0% | 100.0% |
| Not refund requests | 2 | 100.0% | 100.0% | 83.3% |

## Every miss

- **opus q02 run 2**: reply contains ['re:\\b(?:if|once|when|after|until) you approve\\b']. Matched: 'if you approve'. Reply: Hi Felix,  Thanks for considering annual billing. Under our Refund policy (2026), an annual plan can be refunded in full within 30 days of the start of the annual term. After those 30 days, annual pla

## Verdicts changed by re-grading

1 tickets have a verdict that differs from the one recorded when they ran, because the grader or the scenarios changed since (DECISIONS.md records each grader and scenario change). The note is the current grader's for a new miss, and the one recorded at run time for a new pass.

- q02 opus run 2: pass -> miss. reply contains ['re:\\b(?:if|once|when|after|until) you approve\\b']
