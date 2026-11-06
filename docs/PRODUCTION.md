# From prototype to production

The prototype keeps the split that matters: the model reads and writes, code decides and acts. Production keeps
that split and replaces the fictional parts (the JSON billing file, the sandbox ledger, the command line) with the
client's real systems, plus the controls a money-moving process needs.

## What stays the same

- The rules module (`rta/policy.py`), rewritten against the client's real policy and internal procedures, with tests
  at every boundary.
- Tools bound to the helpdesk's verified sender, action tools that check the rules again, and one action per
  ticket.
- Reconciliation of the model's finish against what was executed, the reply guard, and the independent audit.
- The benchmark, extended with anonymised real tickets.

## What changes

**Helpdesk integration.** Tickets arrive from the helpdesk (a webhook or a polling job) with the verified sender,
the ticket id and the thread. The agent's reply goes back as a draft or as a sent reply, depending on the rollout
stage below. Escalations become helpdesk assignments to the right group (approvals, Risk, Tier 2) with the internal
note attached. Follow-up emails on the same ticket need the conversation history, which the prototype doesn't
handle.

**Billing integration.** Read-only lookups for customers, charges, invoices and disputes come from the billing
provider's API instead of `data/billing.json`. Refunds go through the provider's refund endpoint. Currency handling
needs a rule from the client: either a limit per currency or a conversion source.

**Idempotency keys.** Every refund call carries a key derived from the ticket id and the charge id, so a retry or a
duplicate webhook can never refund twice. Before refunding, the code also checks the provider for an existing
refund of that charge.

**Approval UI.** Refunds above the limit land in an approval queue that shows the request, the rules' verdict, the
amount and the draft reply. The approver accepts or rejects with one click; on acceptance the refund executes
through the same guarded code path, with its own idempotency key, and the reply is sent.

**Audit log.** An append-only record of every refund with the ticket number and the reason (the internal procedure
requires both), plus every model turn, tool call and result, decision, flag and approval. It is retained for the
client's finance and dispute periods and readable by finance and support leads.

**PII handling.** Send the model only what a ticket needs: the email, the account's plan and charges. Never send
card details or full payment data. Check that the API terms and data-retention settings match the client's data
policy before any real ticket is processed. Redact or expire traces on the client's schedule, restrict who can
read them, and keep the data in the client's region where that is required.

**Retries and timeouts.** SDK retries for rate limits and server errors, a time limit per ticket, and a
dead-letter queue: a ticket that fails twice goes to a person with its trace. All tools are safe to retry, and the
refund call is idempotent.

**Monitoring and evals on live traffic.** Dashboards for the decision mix, escalation rate, refused attempts,
inconsistent finishes, turn-cap hits, cost per ticket and latency. An alert on any executed violation, which must
stay at zero. A weekly review of a sample of finished tickets, with every new failure added to the benchmark. Every
prompt, model or rule change runs the benchmark before release.

**Rollout: shadow, then assisted, then automatic.**

1. **Shadow.** The agent runs on real tickets without acting. People handle every ticket as today, and the agent's
   decisions are compared with theirs. Exit when the decision agreement and reply quality on a reviewed sample meet
   a bar the client sets, and executed violations are zero.
2. **Assisted.** The agent prepares the action and the reply. A person approves each one with a click. Exit when
   people accept most proposals unchanged for the classes that will go automatic.
3. **Automatic.** The agent acts alone on the cleanest classes first (for example, an eligible monthly refund under a
   lower limit than the agent's), and everything else stays assisted. Each new class goes automatic only after the
   same review.

## What the next phase could cost

An **example estimate**, to show the structure. It is not a quote: the real numbers depend on the client's
helpdesk and billing systems, how many policies there are, and what discovery finds.

| Work item | Hours (example range) |
|---|---|
| Discovery: walk through the real workflow, policies, edge cases and data access with the support team | 6–10 |
| Rules: port the real refund policy and internal procedures, boundary tests | 8–14 |
| Helpdesk integration: ticket intake, draft or send replies, assignments, internal notes | 12–20 |
| Billing integration: read customers, charges and disputes; refunds with idempotency keys | 16–24 |
| Approval UI in the helpdesk: queue view, approve or reject, audit entries | 10–16 |
| Audit log, PII handling and data retention | 8–12 |
| Evaluation set from anonymised real tickets (about 100), grader updates | 10–16 |
| Monitoring, alerts and cost tracking | 6–10 |
| Shadow-mode run with weekly reviews (two weeks) | 8–12 |
| Hardening, documentation and handover | 6–10 |
| **Total** | **90–144** |

API costs come on top. The benchmark report gives the cost per 1,000 tickets for each model, measured with a warm
prompt cache (tickets seconds apart). For a client whose emails arrive more than 5 minutes apart, use the report's
cold-cache estimate, which on the 6 Nov run was about twice the measured cost on Sonnet and Opus.
