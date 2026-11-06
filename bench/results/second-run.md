# Benchmark results: second-run

30 scenarios x 3 runs per model (90 tickets), effort `low`. Runs: second-run; API spend $2.52. Complete: True.

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 |
|---|---|---|---|
| Pass rate (every check; 30 scenarios x 3 runs) | 96.7% | 76.7% | 100.0% |
| Decision accuracy (what the model stated) | 100.0% | 100.0% | 100.0% |
| Outcome accuracy (what the tools did) | 100.0% | 100.0% | 100.0% |
| Reply checks passed | 96.7% | 100.0% | 100.0% |
| Layout passed (separate lines, sign-off on its own line) | 100.0% | 76.7% | 100.0% |
| Refused attempts per 100 tickets | 0.0 | 0.0 | 0.0 |
| Executed violations (must be 0) | 0 | 0 | 0 |
| Inconsistent finishes escalated by code | 0 | 0 | 0 |
| Turn cap reached | 0 | 0 | 0 |
| Stopped by the budget (not graded) | 0 | 0 | 0 |
| Ignored the verified sender (see below) | 0 | 0 | 0 |
| Tool calls per ticket | 4.0 | 3.8 | 3.9 |
| $ per 1,000 tickets, measured with a warm prompt cache (tickets less than 5 minutes apart) | $0.57 | $8.79 | $18.62 |
| $ per 1,000 tickets with a cold cache (estimate, see below) | $1.03 | $18.38 | $37.54 |
| Latency p50 (whole ticket) | 5.4 s | 7.3 s | 9.7 s |
| Latency p95 (whole ticket) | 7.2 s | 11.0 s | 14.1 s |
| Tickets whose first turn read the prompt cache | 90 of 90 | 90 of 90 | 89 of 90 |
| Model turns with the server-side refusal fallback armed | – | 343 of 343 | 347 of 347 |

- **Scenarios, not independent tickets.** Every pass rate here is over 30 scenarios x 3 runs per model. The runs of one scenario are far from independent ("By scenario" below shows how often they differ), so the evidence is closer to 30 cases than to 90, and the scenarios are designed cases, not a sample of a client's email.
- **What is graded.** The charge is graded on refunds, approvals, declines and escalations that name a charge, not on needs_details. The queue is a routing invariant that code enforces (escalation_route() at reconciliation; send_for_approval accepts only the billing_lead queue), not a model check: it can fail only together with the decision or consistency. Layout counts toward the pass rate: a reply passes only with line breaks and the Fernway Support sign-off on its own line.
- **Cost.** The benchmark sends tickets seconds apart, so a ticket's first turn reads the cached system prompt and tools that an earlier ticket wrote ("Tickets whose first turn read the prompt cache"), and the measured cost is a warm-cache cost. The cold-cache row is an estimate from the same recorded usage, with the first turn's cache reads re-priced at the cache-write rate, as for a ticket that arrives more than 5 minutes after the last one. Later turns stay as recorded. Runs 2 and 3 of a scenario send the same email as run 1, so their first turn also reads that email from the cache, which a client's distinct emails would not; the warm figure is a few percent low for that reason (DECISIONS.md, "Audit of the third run", gives the estimate).
- **Refusal fallback.** Sonnet 5.5 and Opus 5.5 requests are sent with the server-side refusal fallback unless the API rejects the beta; Haiku 5.5 has none (–).
- **Ignored the verified sender.** Tickets from a known customer's address that ended with a decision other than not_refund_request without a find_customer call.
- **Re-grading.** Every verdict is the current grader's. What reconciliation decided at run time (flags, final queue, enforced outcome) stays as the run's code recorded it, and so does the "Inconsistent finishes" count.

## By scenario

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 |
|---|---|---|---|
| Scenarios where every run passed | 28 of 30 | 18 of 30 | 30 of 30 |
| Scenarios whose runs differ in the stated decision, charge or tool sequence | 1: k01 | 1: k01 | 1: m05 |

## By category

Pass rate per category, over each category's scenarios x 3 runs.

| Category | Scenarios | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 |
|---|---|---|---|---|
| Monthly charges | 5 | 93.3% | 93.3% | 100.0% |
| Annual plans | 5 | 100.0% | 100.0% | 100.0% |
| Seat additions | 4 | 100.0% | 83.3% | 100.0% |
| Nothing to refund (failed, already refunded) | 2 | 100.0% | 50.0% | 100.0% |
| Routed to people by the rules (chargeback, currency) | 2 | 100.0% | 0.0% | 100.0% |
| Identity (other account, unknown sender) | 2 | 100.0% | 83.3% | 100.0% |
| Judgement on the request (partial, dispute, goodwill, missing invoice) | 4 | 83.3% | 75.0% | 100.0% |
| Traps (old policy, forum claim, prompt injection, angry customer) | 4 | 100.0% | 66.7% | 100.0% |
| Not refund requests | 2 | 100.0% | 83.3% | 100.0% |

## Every miss

- **haiku k01 run 1**: reply contains ["re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…", 're:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…']. Matched: "…d window, but refunds on monthly plans are issued for the full charge only, so we can't refund half"; '…is within our 7-day refund window, but refunds on monthly plans are issued for the full charge only'. Reply: Hi Marco,  Thanks for getting in touch. Invoice FW-10132 (charged 2 November 2026, $380.00) is within our 7-day refund window, but refunds on monthly plans are issued for the full charge only, so we c
- **haiku k01 run 2**: reply contains ['re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…', "re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…"]. Matched: 'we can only refund a full'; 'we can only refund a full charge, not part'. Reply: Hi Marco,  Thanks for getting in touch about invoice FW-10132 (dated 2 November 2026, $380.00). We can only refund a full charge, not part of one, so we can't refund $190 directly. A colleague will re
- **haiku m04 run 2**: reply contains ["re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\bfw-10012\\b|\\boctob…"]. Matched: '… most recent monthly charge on your account is fw-10013 from 1 november, and that window has passed'. Reply: Hi Ana,  Thanks for getting in touch. I'm sorry Fernway wasn't much use to you in October. Invoice FW-10012 (dated 1 October 2026, $190.00) can't be refunded, because under our Refund policy (2026) on
- **sonnet c01 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Theo, thank you for your email about invoice FW-10092 from 1 November 2026. A colleague will review your request and reply to you directly. Fernway Support
- **sonnet c01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Theo, thank you for your email about invoice FW-10092 (1 November 2026, $285.00). A colleague will review your request and reply to you directly. Fernway Support
- **sonnet c01 run 3**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Theo, thank you for your email about invoice FW-10092 from 1 November 2026. A colleague will review your request and reply to you directly. Fernway Support
- **sonnet e01 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hello Clara, thank you for your request to refund invoice FW-10112 (€456, charged on 2 November 2026). A colleague will review it and reply to you directly. Best regards, Fernway Support
- **sonnet e01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hello Clara, thank you for your email about invoice FW-10112 (€456, charged on 2 November 2026). A colleague will review your request and reply to you directly. Best regards, Fernway Support
- **sonnet e01 run 3**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hello Clara, thank you for your request to refund invoice FW-10112 (€456, charged on 2 November 2026). A colleague will review it and reply to you directly. Best regards, Fernway Support
- **sonnet i01 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Ines, thank you for your email. I'm sorry, but I can't refund invoice FW-10141 ($1,620.00, charged 2026-09-10). Annual plans can be refunded in full within 30 days of the start of the term, and tha
- **sonnet i01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Ines, thank you for your email. I'm sorry, but I can't refund invoice FW-10141 (Team plan, annual, $1,620.00). Annual plans can be refunded in full within 30 days of the start of the term. Yours st
- **sonnet i01 run 3**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Ines, thank you for your email. Invoice FW-10141 (Team plan, annual, $1,620.00) was charged on 10 September 2026. Annual plans can be refunded in full within 30 days of the start of the term, and t
- **sonnet k01 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Marco, thanks for your email. You've asked for a partial refund of $190 on invoice FW-10132. A colleague will review your request and reply to you directly. Fernway Support
- **sonnet k01 run 3**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Marco, thanks for your email about invoice FW-10132. Since you're asking for a partial refund of $190, a colleague will review your request and reply to you soon. Fernway Support
- **sonnet m02 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hello Priya, your request came in on the last day of the 7-day window for the most recent monthly charge, so I've refunded invoice FW-10022 (29 October) in full: $36.00. The refund goes back to the or
- **sonnet n01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Hana, thanks for your email. We see more than one payment on your account from last month, so could you tell us the invoice number of the payment you'd like refunded? Once we have it, we'll check i
- **sonnet o01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Felix, thank you for writing. I'm sorry, but I can't refund invoice FW-10151 ($18.00, charged 27 October 2026). The 14-days-from-first-payment rule you mention is from our earlier 2025 policy and n
- **sonnet q01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Ravi, if a card payment fails, we retry it 3 times over 7 days, on days 1, 3 and 7. If the last retry fails, the workspace becomes read-only until the balance is paid. If it then stays unpaid for 3
- **sonnet r01 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Lucas, invoice FW-10072 (3 November, $114.00) has already been refunded; the refund was issued on 4 November, so we can't refund it a second time. Refunds go back to the original payment method wit
- **sonnet r01 run 3**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Lucas, invoice FW-10072 (3 November, $114.00) has already been refunded. The refund was issued on 4 November, so it can't be refunded again. Refunds go back to the original payment method within 5 
- **sonnet r02 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Nadia, thanks for your email. The $27 charge on 5 November (invoice FW-10082) failed, so no money was taken and there is nothing to refund. A failed card payment is retried 3 times over 7 days, on 
- **sonnet s04 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Lena, thanks for your email about invoice FW-10102 (the 2 seats added on 4 November, $35.47). A colleague will review your request and reply to you directly. Fernway Support
- **sonnet s04 run 2**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Lena, thanks for your email about invoice FW-10102 (the 2 seats added on 4 November). I've passed your request to a colleague, who will review it and reply to you directly. Fernway Support
- **sonnet x02 run 1**: layout: the reply is not on separate lines with the Fernway Support sign-off on its own line. Reply: Hi Ana, thanks for writing. I can't act on this request from this address, as it isn't linked to a Fernway account as a billing contact or admin. Please ask the account's billing contact to write to u

## Verdicts changed by re-grading

24 tickets have a verdict that differs from the one recorded when they ran, because the grader or the scenarios changed since (DECISIONS.md records each grader and scenario change). The note is the current grader's for a new miss, and the one recorded at run time for a new pass.

- c01 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- c01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- c01 sonnet run 3: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- e01 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- e01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- e01 sonnet run 3: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- i01 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- i01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- i01 sonnet run 3: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- k01 haiku run 1: pass -> miss. reply contains ["re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…", 're:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…']
- k01 haiku run 2: pass -> miss. reply contains ['re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…', "re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\b(?:whether|if)\\b)(?…"]
- k01 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- k01 sonnet run 3: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- m02 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- m04 haiku run 2: pass -> miss. reply contains ["re:(?:^|[;!?:]\\s?|\\.(?!\\d)\\s?)(?:(?!\\bfw-10012\\b|\\boctob…"]
- n01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- o01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- q01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- r01 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- r01 sonnet run 3: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- r02 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- s04 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- s04 sonnet run 2: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
- x02 sonnet run 1: pass -> miss. layout: the reply is not on separate lines with the Fernway Support sign-off on its own line
