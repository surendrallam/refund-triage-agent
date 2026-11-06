# Decisions

Each design choice, the reason, and every place where the fictional policy documents are silent and a choice had
to be made. The benchmark first ran on 6 Nov 2026: two smoke runs (`runs/smoke`, `runs/smoke-so`) and the full run in
`runs/main` (30 tickets x 3 runs on each of Haiku 5.5, Sonnet 5.5 and Opus 5.5, $2.51 of API spend). Claude agents
then audited all 270 graded tickets ([Audit of the first run](#audit-of-the-first-run)), and this file marks the
checks and rules added after that audit "(6 Nov audit)". The second full run (`runs/second-run`, $2.52) measured
those changes, and a second audit of its 270 tickets followed ([Audit of the second run](#audit-of-the-second-run));
what changed after it is marked "(second-run audit)", and what a review of those changes added before the third run,
"(review of the second-run changes)". The third run measured those changes; its audit is the last section before
the prices.

## The split between model and code

**Code decides every rule it can check from data; the model decides only what needs reading.** Eligibility, the
refundable amount, identity, the approval limit, chargebacks and routing live in `rta/policy.py`, and the model
follows the `expected_decision` that `check_refund_eligibility` returns. Two calls stay with the model: which charge
the email means when it names no invoice, and whether the request falls outside the policy (a partial amount, a
dispute, a credit, several charges). Code bounds both: the full amount only, one action per ticket, an action only on
a charge the email names when it names one, and reconciliation afterwards. A model is good at reading a messy email
and writing a clear reply; it is not a safe place for "is day 7 inside a 7-day window" or "may this sender act on
this account". Those are a few lines of code each, and they can be tested at the boundaries.

**The action tools check everything again.** `issue_refund` and `send_for_approval` call the policy again with the
ticket's verified sender and date. They refuse an ineligible charge, a different amount, the wrong queue, another
account, an account with an open chargeback, a non-USD charge, a charge other than the one the email names, and a
second action on the same ticket. A refusal goes back to the model as a tool error and counts as a refused attempt.
The model can ask for anything; the code executes only what the rules allow.

**An action only on the charge the email names.** When the subject or body names invoice numbers (FW-12345), the
action tools refuse any other charge (`invoice_not_named`), and the audit checks it again. A customer who asks about
one charge can't have a different, eligible one refunded instead. When the email names no invoice, the model chooses
(gap 12).

**Tools are bound to the ticket, not to what the model says.** The verified sender and the date come from the
helpdesk and are fixed when the toolbox is created. `find_customer` with another address returns the same answer
whether or not an account exists. `list_charges` and `check_refund_eligibility` give one shared answer for another
account's ids and for ids that don't exist, and the action tools refuse both with the same message, so a tool result
reveals nothing about other accounts. The trace keeps the internal reason.

**Tolerant charge references.** Models sometimes pass the invoice number where the charge id belongs. The tools and
`finish` read an invoice number on the sender's own account as its charge id, and the trace notes it. The code maps
no invoice on another account.

**One refund or approval per ticket.** Reconciliation compares one finish with one action, and a ticket is one
charge. An email about several charges goes to a person (the prompt says so), and the code refuses a second action.

**Reconciliation after the loop.** The model's `finish` is a claim. Code checks it against the sandbox ledger and
the rules:

1. `refund_issued` needs exactly one executed refund with the same charge and amount, and no approval.
2. `sent_for_approval` needs exactly one approval request with the same charge and amount, and no refund.
3. Any other decision needs no executed action.
4. `declined` must name a charge for which the rules also say `declined`. A decline the rules don't back (an
   eligible charge, a currency or policy gap, or no charge at all) is inconsistent.
5. `needs_details` that names a charge must be backed by the rules for that charge: the sender may not act on it,
   or no such charge exists. Asking for details about one of the sender's own refundable charges is inconsistent.
   5b. (6 Nov audit) `needs_details` that names no charge, or a charge the sender can't act on (a guessed id, another
   account's charge), is inconsistent when code already knows who is asking and about what: the verified sender is
   the billing contact or an admin of their account (the identity rule in `rta/policy.py`), the email names at least
   one invoice, and every invoice it names is a charge on that account dated on or before the ticket date. In the
   first run Haiku once ended d01 this way, asking the verified billing contact to write in again; rules 1 to 8 let
   that through. Re-run on all 270 stored traces, the rule flags only that ticket (which stays a miss: its stated
   decision is wrong either way). An email that names no invoice (n01), another account's invoice (x01), an invoice
   that doesn't exist or isn't dated yet, or that comes from an unknown sender (x02) is still backed. The flag is
   `needs_details_not_backed:sender_and_invoice_verified`.
6. `not_refund_request` must name no charge, and the ticket must not have called `check_refund_eligibility`,
   `issue_refund` or `send_for_approval` (the prompt says not to).
7. `declined` or `needs_details` on an account with an open chargeback is inconsistent: those tickets go to Risk.
8. The reply must not name internal roles, queues or limits. A bare "$500.00" is allowed only as the amount the
   tools actually refunded: the limit is inclusive, so the agent can issue exactly $500.00 and say so. "Above $500"
   and "the $500 limit" always count as internal. (6 Nov audit) The guard also covers paraphrases that use no name
   or number: a manager's, supervisor's or senior colleague's sign-off, "refunds above a certain amount", "larger
   refunds", "exceeds what I can approve", an approvals, fraud, disputes, chargebacks or escalations team, queue,
   department or desk, "level 2 support", and the chargeback rule: "an open chargeback on your account" and, since
   the review of the audit changes, the account's chargeback named without "open" ("the chargeback on your October
   invoice", "a chargeback for invoice FW-10091"). It still lets through the public "customer success manager",
   "line 2 of your invoice", "raise a chargeback with your bank" and "the chargeback process". (second-run audit) It
   also catches a team lead, line manager, finance team or senior member when the role is our own ("my team lead's
   approval") or sits near approve or sign-off, but not the customer's ("your team lead can write in"); a second
   approval or sign-off; "too large for me to approve" and "above what support can sign off"; an approval level,
   authority or tier; "refunds of this size"; and a bank or card dispute ("an open dispute with your bank", "your bank
   is disputing the October payment"), but not the customer's own invoice dispute on d01 ("your open dispute about
   the seat count"). None of the 1,080 stored stated and final replies of the two runs, and no example or alt reply,
   has one of them. It is a list of patterns: it catches the wordings in `tests/test_policy.py`, not every wording.

Any failure turns the ticket into an escalation with flags, and a neutral holding reply replaces the model's. The
"enforced outcome" in the trace is what the tools did: a refund or an approval, if one was executed, otherwise the
final decision.

**An independent audit.** `audit()` re-checks every executed action against the rules after the ticket. The
benchmark reports these as executed violations. The action tools make them impossible, and the audit is there to
prove it on every run.

**Code routes escalations.** `finish` has no queue field. Code sends an escalation to Risk when the sender's account
has an open chargeback, and to Tier 2 otherwise; approvals go to the Billing Lead queue (`billing_lead`).
`check_refund_eligibility` returns a queue only for approvals (`billing_lead`). It never gives the model the Risk or
Tier 2 route; code applies that at reconciliation.

## The agent loop and the API

All API usage follows Anthropic's Messages API documentation for Python (tool use, prompt caching, refusal
fallbacks).

- **Manual loop, not the SDK's tool runner.** The loop needs to stop at 8 turns and escalate, refuse to run tools
  from a refused or truncated turn, apply the fallback echo rule, and reconcile at the end. A plain loop over
  `messages.create` makes each of those visible and testable with a fake client.
- **Strict tools.** Every tool has `strict: true`, `additionalProperties: false` and all properties required.
  Optional values in `finish` (charge, amount) are nullable through `anyOf` with `null`. No numeric or string
  constraints, which strict schemas don't support.
- **`tool_choice` stays `auto`.** Forced tool use returns a 400 on Claude Opus 5.5 and Claude Sonnet 5.5. The prompt
  asks for `finish`; a turn without any tool call gets one short nudge from the helpdesk and counts toward the cap.
- **At most 8 model turns.** The normal path takes 5 (find the customer, list the charges, check, act, finish). After
  8 turns the code escalates the ticket.
- **Effort `low` by default, adaptive thinking with summarized display.** Thinking can't be disabled on Claude Opus
  5.5; on Claude Sonnet 5.5 only `between_tools` turns it off. The loop keeps adaptive thinking on all three models,
  with effort as the control, so the summaries reach the trace and a reviewer can see why the model did what it did.
  Thinking tokens are billed in full; the summary is only what the trace shows. `max_tokens` is 8,192 per turn,
  which leaves room for thinking plus a tool call and the reply.
- **Stop reasons.** `refusal` and `max_tokens` end the ticket without running that turn's tools (a cut-off tool
  call may be incomplete); the code escalates.
- **Prompt caching.** Tools and the system prompt never change, and a cache breakpoint on the system block caches
  both. Top-level automatic caching covers the growing conversation. The system prompt holds no date, ticket id or
  sender, so the cached prefix is the same for every ticket.
- **Ticket details in a mid-conversation system message.** The verified sender, the date and the ticket id are
  operator context, so they go in a `role: "system"` message after the email rather than in the cached system
  prompt or in text the customer could imitate. Anthropic's model notes list these messages for all three models
  (the caching guide omits Haiku 5.5). Measured on 6 Nov 2026: the benchmark sent one on all 270 tickets, on all
  three models, and the API rejected none, so no ticket fell back to the user turn (no trace has the fallback note).
  If the API rejects one, the loop moves the details into the user turn once and remembers that for the model.
  (6 Nov audit) The first user turn now says, in a line written by code before the `<email>` block, that the
  helpdesk's verified ticket details follow in a system message. In one first-run ticket (Haiku, d01 run 1) the
  model's thinking took the verified sender for its own notes, and it never looked the sender up; that the line
  fixes this is a hypothesis until the next run.
- **The email can't break out of its block.** The subject and body go inside `<email>` tags. The code escapes the
  `<` of any `<email>`, `</email>` or `<ticket_details>` tag in the customer's text, in any case or spacing, so the
  email can't close its block or forge ticket details. The tools stay bound to the verified sender either way.
- **Append-only history.** Every assistant turn goes back unchanged, thinking blocks included, and tool results go
  back in one user message per turn. The system prompt and tools never change mid-ticket. This keeps the cache
  valid and satisfies the thinking-block history check on these models.
- **Refusal fallback.** Sonnet 5.5 and Opus 5.5 requests carry `fallbacks: "default"` under the
  `server-side-fallback-2026-07-01` beta, so the API retries a safety-classifier decline on the model Anthropic
  recommends. Haiku 5.5 has no server-side fallback and gets none. After a fallback, the loop echoes only the text
  from the declined attempt, as the fallback documentation requires. If an account doesn't have the beta, the
  wrapper turns it off for that model and carries on. (6 Nov audit) Whether the fallback was armed is now on the
  record: every turn in the trace has `fallback_armed` (the request went through the beta call path), the first call
  of that model that succeeds after the API rejected the beta (normally the retry without it, on the same ticket)
  carries a trace note, so a failed retry can't lose it, and the report counts armed turns for Sonnet and Opus. The
  first run predates the field, so its report says "not recorded"; all of its 1,044 turns ended in `tool_use` on the
  requested model. One edge is untested: Sonnet 5.5's fallback retries `cyber`
  and `frontier_llm` declines on Claude Sonnet 5, which may not accept the mid-conversation system message. Such a
  ticket would end as an API error and go to a person. Refund emails are unlikely to trigger those classifiers.
- **Costing a fallback, and its budget.** After a server-side fallback, the top-level `usage` covers only the attempt
  that produced the message; `usage.iterations` lists every attempt, each billed at its own model's rates. The
  ledger prices every attempt (an entry that names no model at the dearest candidate) and records the summed
  tokens. Whether a declined attempt is billed depends on when it declined; counting it keeps recorded costs on the
  high side. While a fallback can run, a call reserves the requested model's worst case plus the dearest fallback
  model's (Claude Opus 5 at $5 / $25). For this repo's prompts (estimated at 4,000 to 6,500 tokens from turn 1 to
  turn 8) that is $0.41 to $0.44 per Opus 5.5 call and $0.32 to $0.34 per Sonnet 5.5 call, against $0.18 to $0.20
  and $0.09 to $0.10 without the fallback.
- **API version.** The anthropic SDK 2.x sends API version `2026-10-21`, which rejects sampling parameters
  (`temperature`, `top_p`, `top_k`), documents in assistant turns and cited text in user turns. The agent sends none
  of these. `tests/test_sdk_wire.py` runs the loop through the real SDK client over an in-process mock transport
  and checks the request body, the beta header and the echoed blocks.
- **Tolerant tool names.** A tool name that differs only in letter case runs the right tool.
- **Errors and the budget.** An API error ends the ticket as an escalation, with the error and its request id in
  the trace; every turn records its request id too. The budget check stops a ticket before any call whose worst
  case could pass the budget. The ticket then ends with `end_reason` `budget_stop`, the code reconciles and audits it
  like any unfinished ticket (flag `no_finish:budget_stop`), and the CLI still writes its trace, so a refund from an
  earlier turn stays on record. A budget stop ends a benchmark run, and so does an API error every ticket would hit
  (a bad key, no access, an unknown model, a rejected request).
- **Settings.** `RTA_MODEL`, `RTA_EFFORT`, `RTA_TODAY`, `RTA_BUDGET_USD` and `RTA_COSTS_CSV` come from the
  environment, then from the project's `.env` file (read directly with `dotenv_values`, never loaded into the
  environment), then the defaults. `RTA_BUDGET_USD` caps each ledger file: `runs/costs.csv` for `rta triage`, and
  `runs/<name>/costs.csv` for a benchmark run, which also has its own `--budget`.

## What the reply may say

The prompt's reply rules, beyond tone and the facts each decision needs. They came out of the 6 Nov audit, the review
of its changes and the second-run audit. Some have a graded check behind them (the m04, a05, f01 and k01 checks, the
window dates and the layout under [The benchmark](#the-benchmark), and the reply guard); the others are ungraded. The
second run measured the first set; the third run measured the second-run audit's changes.

- **Only the checked charge.** "Don't say whether any charge other than the one you checked can or can't be
  refunded, and don't check a charge just to mention it; you may name it and its date." In m04 Haiku twice told the
  customer that her November charge, refundable on the ticket date, was past the window; it had checked only the
  October charge. The wording doesn't invite a second check: checking FW-10013 would answer `refund_issued`, and an
  `issue_refund` on it would be refused (`invoice_not_named`) and fail the ticket through `tools_must_not`. The
  `not_most_recent_monthly_charge` detail now ends "This answer is about this charge only."
- **Dates from code.** The eligibility detail gives the window's dates, so the model doesn't do date arithmetic:
  for a charge inside the window, its last day; for one outside it, the day the window ended (its last day) and how
  long ago the charge was (policy gap 1). Two Sonnet replies in a05 had said the window "closed 45 days in" or
  "closed 45 days after the charge" (day 45 is the ticket date).
- **Only documented facts.** "State only facts that a tool result, a policy passage from search_policy or the
  policy facts above give. Don't describe app settings or steps, and don't say what a refund, a cancellation or any
  other change does to the plan, its renewal, or later charges or retries, unless one of them says so." Replies had
  promised that cancelling stops the retries of a failed payment (r02, five Sonnet and Opus runs) and named settings
  pages no document gives (m01, m03, m04). In the second run Sonnet and Opus kept to the earlier wording of the rule
  on every ticket, and six Haiku replies broke it (listed under [Audit of the second run](#audit-of-the-second-run));
  the second-run audit widened it from "what an action (such as cancelling) will do" to what a refund, a
  cancellation or any change does to the plan, its renewal and later charges. The policy facts in the prompt, such as
  turning off auto-renewal and the payout timing, stay usable. See policy gaps 16 to 18.
- **Layout and dates.** (second-run audit) "Put the greeting, each paragraph and the sign-off on separate lines, with
  "Fernway Support" alone on the last line. Write dates as "14 January 2027", never as 2027-01-14." 21 of 90 Sonnet
  replies in the second run (22 in the first) were one line of text, and some copied the tool's ISO dates. The
  layout is graded (line breaks and the sign-off line; where the greeting sits is not checked); the date format is
  not. (review of the second-run changes) The example date was "10 October 2026", which is also i01's window last
  day and a charge date in the data; 14 January 2027 appears nowhere in the data, the scenarios or the documents, so
  a copied example can't pass the window-date check as a worked-out date. The prompt length is unchanged.
- **An escalation decides nothing.** (second-run audit) "Escalated: say a colleague will review the request and
  reply. Don't explain internal procedures or the rule that sent the ticket to a person (the full-charge rule is
  internal too), and don't say whether the request can or can't be granted." Two Haiku replies on k01 refused the
  partial amount and gave the code's full-amount rule as Fernway policy ("We can only refund a full charge, not part
  of one"), then said a colleague would review it. The tool results on that path now read as internal routing: an
  eligible charge's detail ends "If the customer asked for a different or partial amount, don't act on this charge:
  escalate the ticket. A person decides that request; in the reply, say only that a colleague will review it and
  reply.", every escalation detail (open chargeback, charge in EUR, seats on a monthly plan, a request before 1 March
  2026) ends "This routing is internal: in the reply, say only that a colleague will review the request and reply,
  not why it went to a person or whether it can be granted.", and a refused different amount reads "This tool refunds
  whole charges only ..." (or "sends", for `send_for_approval`) instead of "Only the full amount of the charge can be
  refunded". The EUR detail no longer says "The policy allows a full refund of ...". No decision or amount changed.
  The grader checks this line on k01 only (a refused partial amount or the full-amount rule given as policy). On s04
  and e01 a reply that gives the routing reason still passes ("Because these seats were added to a monthly plan, a
  colleague will review ...", as first-run Haiku s04 runs 2 and 3 did; "Because the charge is in euros, ..."): a
  check there would turn those two stored first-run verdicts into misses, so it waits for the owner's decision.
- **The renewal line is for annual plan charges.** (second-run audit) "For an annual plan charge past 30 days (not
  seats added to it), say they can turn off auto-renewal at any time." All three Sonnet replies on s02 told a
  customer dropping 3 seats to turn off auto-renewal, which would stop the whole 13-seat workspace from renewing. The
  seat detail past 30 days now ends with the Billing FAQ's fact instead, "Removed seats take effect at the next
  renewal." (kb/billing-faq.md, "Changing seats or plans": "Removed seats and plan downgrades take effect at the next
  renewal.").
- **The forum's 60 days was never the policy.** The prompt says so plainly and asks the model to call the claim
  incorrect, not out of date or replaced; all three Haiku replies on f01 had called it "out of date".
- **Nothing about other accounts.** On needs details: "Don't confirm who is on another account or that an invoice
  belongs to it; ask its billing contact to write in." Four x01 replies restated the customer's own claims about the
  other studio as fact (who its billing contact is, which addresses it has, that the invoice is its). They revealed
  no account data (the agent never looked the other account up), so this stays ungraded.
- **Nothing about the chargeback.** (review of the audit changes) The line on internal terms now also names "an
  open chargeback on the account". `check_refund_eligibility` tells the model about an open chargeback (it is why
  the ticket goes to a person), and the reply guard withholds a reply that repeats it (rule 8), so the prompt now
  says not to mention it. No first-run reply mentioned a chargeback.

## Data and knowledge base

- **Fictional data only.** `data/billing.json` has 18 customers and 32 charges under reserved `.example` domains
  and first names only. Every amount follows the price list, which a test checks.
- **Frozen in time.** The default "today" is 6 November 2026, so demos are reproducible. Charges dated after today
  don't exist yet, refunds count from their `refunded_on` date and chargebacks from `opened_on`.
- **Each ticket starts from the fixture.** A refund the agent issues goes to that ticket's sandbox ledger and trace
  only, and nothing is written back to `data/billing.json`. A second ticket about the same charge would see it as
  unrefunded. Production checks the billing provider and uses idempotency keys (docs/PRODUCTION.md).
- **Public documents only in `kb/`.** The Refund policy (2026), the Billing FAQ and Plans and pricing (2026), copied
  from the grounded-rag-assistant sources with the fiction note kept. The internal procedures are rules in code. The
  model never sees the $500 figure or the procedure documents. It sees the approval queue id (`billing_lead`), the
  names it must not use, and the facts behind each tool answer (for example, that an account with an open chargeback
  gets no refund from the agent); the reply guard blocks the internal names. The superseded 2025 policy and the forum
  are not in the knowledge base; the prompt tells the model what to say when a customer cites them.
- **BM25 with plural folding.** Three documents and 15 sections don't need embeddings. Folding plurals ("plans" to
  "plan") makes "Team plan price per seat" find the price table.

## Policy gaps and the choices made

The documents are fictional, but the gaps are real: each one is a question a client would need to answer.

1. **Counting days.** "Within N days" counts calendar days from the charge date (monthly), the start of the annual
   term, or the date seats were added, to the date the customer asked (the ticket date). Day N is inside the window:
   a monthly charge can be refunded on day 7 and not on day 8; annual plans and seats on day 30 and not on day 31.
   Dates have no time zone. (6 Nov audit) The tool's detail states the dates this gives: the last day a charge
   inside the window can be refunded, or the day the window ended for one outside it (its last day, so a charge from
   29 October has a window that ended on 5 November).
2. **The start of the annual term** is the date of the annual charge, because annual billing is charged once a year.
3. **"The most recent monthly charge"** is the latest monthly subscription charge, on or before the request date,
   on which money was taken (paid, or since refunded). A failed attempt doesn't count. This check comes before the
   7-day window, so an older charge is declined as "not the most recent" even if it is also outside the window.
4. **Refund amount.** Always the full amount of the one charge. A request for a different or partial amount goes to
   Tier 2; code refuses any other amount.
5. **The $500 limit** is inclusive: the agent can issue $500.00, and $500.01 needs the Billing Lead. It applies to
   the amount of the one charge being refunded. Because the agent can issue exactly $500.00, a reply may state that
   amount; the reply guard allows a bare "$500.00" only when it is the amount actually refunded.
6. **Currencies.** The limit is in dollars and the documents give no conversion rule, so an eligible charge in EUR
   or GBP goes to Tier 2 whatever its size. An ineligible one is simply declined. The fixture has one non-USD charge,
   on one EUR account (ticket e01); EUR prices are not published, so it uses the USD numbers in euros. The tool
   tells the model only that a person decides refunds in that currency, so it has no limit wording to repeat.
7. **Seat additions on monthly plans** are not covered (the policy covers seats added to annual plans), so they go
   to Tier 2 rather than being declined.
8. **Proration** in the fixture: seats added x the per-seat price for the period x remaining days / days in the
   period, rounded half up to the cent. The documents say only "prorated to the end of the billing period".
9. **Nothing to refund.** A failed charge (no money taken) and a charge already refunded end as `declined`, with the
   reason, because the decision set has no separate value for them.
10. **Open chargebacks** block every refund on the account, as of the request date, and send the ticket to Risk.
    This comes after the identity check, so nothing about the account reaches someone who isn't authorised.
11. **Identity.** The sender must be the account's billing contact or an admin, compared case-insensitively on the
    bare address. Members who are neither, unknown addresses, personal addresses, and requests about another
    account all end as `needs_details`, asking the account's billing contact to write in. Identity is checked first.
12. **Missing invoice number.** The policy asks customers to include it but doesn't say what to do without it. When
    exactly one charge fits, the agent may go ahead; when more than one could be meant, it asks for the invoice
    number (`needs_details`). This is the model's call, not a code rule. When the email does name an invoice, code
    allows an action only on that charge.
13. **Goodwill credits, compensation, disputed charges, several charges in one email, and anything else the policy
    doesn't cover** go to Tier 2, and the agent never declines them.
14. **Requests dated before 1 March 2026** would fall under the superseded 2025 policy, which is not implemented, so
    the code escalates them. From 1 March 2026 the current policy applies to the request, whenever the charge was
    made.
15. **Payout timing.** Replies say refunds reach the original payment method within 5 to 10 business days (the
    policy). The sandbox doesn't model payouts.
16. **Cancelling with an unpaid, failed invoice.** (6 Nov audit) No document says what cancelling does to a failed
    invoice and its scheduled retries (days 1, 3 and 7): whether the retries stop and whether the balance stays
    owed. The Billing FAQ says only that the workspace becomes read-only until the balance is paid, which suggests,
    without saying, that it stays owed. So the agent must not promise what cancelling will do (the prompt's
    "only documented facts" rule). The grader doesn't check this: in the first run five r02 replies (Sonnet and
    Opus) said cancelling stops the retries, and no check fails them for it, because the documents can't say they
    are wrong (Sonnet run 2 is now a miss, on layout only). The owner decides the fact; it then goes into
    `kb/billing-faq.md` and the `charge_failed` detail, with a check on r02 and a new run (the stored runs never had
    the fact, so they aren't re-graded against it).
17. **What a refund does to the plan and its renewal.** (second-run audit) No document says whether refunding the
    annual term, or the latest monthly charge, ends the plan or leaves it running with renewal on, or whether later
    charges follow. Undecided, so the reply must not say (the prompt's "only documented facts" rule). In the second
    run Haiku's a03 run 3 said "no further charge applies" after a refund; it passes, because no document says it is
    wrong, and the first run had the opposite claim (Haiku m01 run 1: "we've left the plan as it is, so please
    cancel it ... to avoid the December charge"). The owner decides the fact; it then goes into `kb/` and the refund
    details, with a check on the refund and approval tickets (both directions) and a new run.
18. **Replacing the card on file.** (second-run audit) No document says how or when a customer replaces the card on
    file, or whether the next charge or retry uses the new card. Undecided. In the second run Haiku's q01 runs 1
    and 2 advised updating the payment details before the card changes; they pass for the same reason. If the owner
    states the fact, it goes into `kb/billing-faq.md` (Payment methods or Failed payments), so `search_policy`
    returns it, and q01 is checked again in a new run.

## The benchmark

- **Deterministic grading, no LLM judge.** A ticket passes only when the stated decision, the enforced outcome, the
  amount, the charge, the queue, the reply checks, the reply's layout, the forbidden tools and consistency all pass,
  with no executed violation.
- **The charge on an escalation.** (6 Nov audit) The grader checks the charge on refunds, approvals and declines,
  and now on escalations too: an escalation that names a charge must name the one the ticket is about
  (`expect.charge_id`), since a person picks it up from there. An escalation that names no charge is not failed for
  it, because reconciliation accepts one and there is no action to compare. In the first run every escalation on
  s04, c01, e01, k01 and d01 named the expected charge, so no verdict changed.
- **What the charge and queue checks mean.** (second-run audit) The charge is graded on refunds, approvals,
  declines and escalations that name a charge, not on needs_details: every x01 and x02 finish named no charge, and
  their `expect.charge_id` stays because the identity tests use it. The queue is a routing invariant that code
  enforces (`escalation_route()` at reconciliation, and `send_for_approval` accepts only `billing_lead`), not a model
  check: it can fail only together with the decision or consistency, and it was right on all 81 rows of the second
  run where it is set. The report and the README say so.
- **Layout.** (second-run audit) A reply passes only when its raw text (before `normalise()`, which collapses
  whitespace) has line breaks and the "Fernway Support" sign-off on its own last line. It gates the pass rate and the
  report gives it its own row, so a layout miss shows apart from the content checks. Every example, alt and wrong
  reply in the scenario file has that layout (a test checks it). Where the greeting sits is not checked: a check
  for it was tested in the review of the second-run changes and left to the owner
  ([Audit of the second run](#audit-of-the-second-run)).
- **Window dates.** (second-run audit) On a declined ticket whose charge has a refund window (monthly 7 days, annual
  30 days, seats on an annual plan 30 days), `bench/grade.py` works out the window's last day from
  `data/billing.json` with `rta.policy.last_day_in_window()` and fails a clause that says the window or period
  closed, ended, expired, lapsed or ran out (on) a date or was open until a date, or that its last day (of the
  window, to ask for a refund) was a date, when that date is not the last day (gap 1; the day after fails too). "The
  last day of your term" or "of your billing period" is not the window's and is not checked. Only dates with a month
  name or in ISO form count; a clause that names another window (14, 60, 2025, would, if, or an old, previous or
  earlier policy, rule or window) or another invoice is skipped. The note names the date.
- **Scenarios, not tickets.** (second-run audit) The 3 runs of a scenario are far from independent: in the second
  run the stated decision, charge and tool sequence were the same in all 3 runs of 29 or 30 scenarios per model. So
  every pass rate is stated as "30 scenarios x 3 runs", and the report counts the scenarios where every run passed
  and those whose runs differ. The scenarios are designed cases, not a sample of a client's email.
- **A refused attempt fails a ticket only through `tools_must_not`.** Every scenario lists, in `tools_must_not`, the
  action tools the rules don't call for, so attempting one fails the ticket even though the code refuses it (the
  prompt injection, for example). A refused call of the right tool (a wrong amount, then the right one) counts as a
  refused attempt but does not fail the ticket. The report gives refused attempts per 100 tickets.
- **Reply checks are graded on the model's own reply**, before reconciliation could replace it, so a reply that names
  an internal term fails even though the customer would never see it.
- **Reply checks are tested both ways.** Each ticket carries correct replies in other words (`alt_replies`) that
  must pass, and wrong replies (`wrong_replies`) that must fail. Every ticket with forbidden phrases has a wrong reply
  that has every required fact and fails only on a forbidden phrase, so a grader that skipped those checks would fail
  the tests. The forbidden phrases cover refunds claimed or promised on tickets with no refund, approvals claimed on
  tickets still waiting for one, and "waiting for approval" on refunds already issued. The refund-policy question
  (q02) may say what a refund would be, but not that one happened.
- **Reply checks added or fixed after the first run** (6 Nov audit, and the review of its changes). Each was tested
  on both skeptics' correct and wrong wordings and is pinned by alt and wrong replies in `bench/scenarios.yaml` or by
  cases in `tests/test_grade.py`. Like the reply guard, each is a list of patterns: it catches the wordings tested,
  not every wording.
  - The shared refund-claim patterns no longer match a negated sentence: "No refund has been issued yet." passes,
    "Your refund has been issued." and "No problem: your refund has been issued." still fail. They also catch more
    claims: "a refund of $36.00 has been issued", "we're processing your refund", "I've initiated a refund", "I've
    gone ahead and refunded", and, on tickets with no refund, "your refund will be processed" and "we'll issue it".
    "We're processing your request", "I've started a review of your refund request", "I've sent your refund query
    to a colleague" and "a colleague will let you know whether the refund will be processed" stay correct.
  - m04: the reply must not say that the customer's latest charge (FW-10013, refundable on the ticket date) is
    outside the window or can't be refunded ("FW-10013 also falls outside", "your latest invoice (FW-10013) is also
    past"), or that neither invoice can be.
  - a04, a05, s02, i01 (the 30-day declines): no wrong closing point for the window ("closed 45 days in", "closed on
    day 45"); day 30 and day 31 both pass, because one is the last day inside and the other the first outside, and
    so does a trial period. Not on f01, o01 or q01, whose replies may describe another window.
  - r02: the customer is leaving, so the reply must not tell her to check or keep her card valid or up to date in
    the same sentence as a reason that she is leaving ("if you no longer want the plan", "since you don't want to
    continue", "if you're not continuing"), in either order: a valid card lets the retry take the $27. "There's no
    need to check your card" is fine.
  - f01: the reply must not present the forum's 60 days as a former policy ("out of date", "seems to be out of
    date", "no longer applies", "no longer our policy", "was replaced", "our old 60-day policy", "under our previous
    policy, refunds were available for 60 days"); calling it wrong is right.
  - w01: no refund, credit or free month claimed or promised ("I've applied a $50 credit"). d01: no refund and no
    corrected, adjusted or reissued invoice claimed or promised ("we will issue a corrected invoice"). On both, a
    clause about whether one will happen ("a colleague will check whether a credit can be applied") is fine.
  - The renewal fact needs the customer's own action, in either word order ("turn renewal off", "turn off
    renewal", "disable renewal", "opt out of renewal"); a bare "your plan won't renew" no longer counts, because the
    plan renews unless the customer turns renewal off. On a04, a05, f01 and i01 the reply also must not say that the
    agent turned renewal off ("I'll turn off auto-renewal for you") or that the plan won't auto-renew by itself
    (`renewal_claims`).
  - The follow-up fact allows a longer clause before the action (up to 80 characters, within the sentence) and
    accepts "you'll hear back from a colleague", but not "we will not ..." or an action left to the customer ("you
    can reply", "feel free to reply", "if you reply", "we need you to reply").
  - n01 also accepts a question that asks which of the two payments the customer means ("Did you mean ... or
    ...?"), instead of the word "invoice"; a "let us know whether" clause counts only when it is about the payments.
  - The payout fact accepts "between 5 and 10 business days", and "it doesn't need approval" is no longer read as
    a refund waiting for approval.
  - `reply_must_not` may nest one level, because YAML can't append to an alias (`[*no_refund_claims,
    *window_closed]`); `bench/grade.py` flattens it.
  - Every pattern a ticket adds to `reply_must_not` matches one of its wrong replies, and so does every pattern of
    the approval-claim, renewal-claim, pending and cancellation lists (`tests/test_scenarios.py`), so a pattern that
    stops matching fails a test. The review found that, before this, 12 of the 26 new or changed patterns could be deleted with every test
    still green; now deleting any one `reply_must_not` pattern, shared or not, fails a test (each was deleted in turn
    in a scratch copy).
  - Known misses, found in the review and still open: m04 passes "As for FW-10013, that one is past the window
    too.", "FW-10013 can't be either." and "The same goes for FW-10013, which we also can't refund."; r02 passes
    "Failed payments are retried, so please make sure your card on file is still valid." (no reason given that she
    is leaving); d01 passes "We'll apply a credit to your account."; a05 passes "That window closed 45 days ago." and
    "closed forty-five days after"; e01 passes "We've put through a refund" and c01 "I've reversed the charge"; k01
    fails a follow-up whose clause before the action is longer than 80 characters. The second-run audit closed four
    of the review's misses: f01 now fails "Before March, refunds were available for 60 days.", m01 fails "It will not
    be sent until it's approved." and passes "It doesn't even need approval.", and the reply guard catches "bigger
    refunds need a second approval". The misses the second-run audit's own checks leave are listed under
    [Audit of the second run](#audit-of-the-second-run).
- **Expected outcomes and the code.** For the 24 tickets the rules decide (`basis: charge` or `identity`),
  `tests/test_scenarios.py` runs `policy.py` on each ticket and checks the expected outcome. For the 6 that what the
  email asks decides (`basis: request`: the partial amount, the dispute, the goodwill credit, the missing invoice and
  the two billing questions), the tests check the sender, the routing and the candidate charges; gaps 4, 12 and 13
  above record those choices. Each ticket's example reply must pass its own checks, which keeps the checks
  achievable.
- **The prompt briefs two traps.** The system prompt tells the model about the superseded 14-day policy and the
  forum's 60 days, so tickets o01 and f01 test whether the model applies that briefing. The prompt describes credits
  in general terms and doesn't name the outage in ticket w01. (6 Nov audit) The briefing now says plainly that the
  60 days was never Fernway's policy; the new f01 check fails a reply that frames it as a former policy.
- **Budget stops are not graded.** A ticket the budget cuts off part way stays in `results.jsonl` with its trace and
  spend, but the metrics leave it out and count it as a budget stop, and the run is marked incomplete. A run's
  `spent_usd` comes from the ledger, so it includes such tickets. A report merges runs only when they cover the same
  scenario ids.
- **Every run is re-graded with the current grader.** `bench.report` grades each stored trace again and lists, under
  "Verdicts changed by re-grading", every ticket whose verdict differs from the one stored at run time, with the
  grader's note (the current note for a new miss, the stored one for a new pass). `results.jsonl` is never edited,
  and what reconciliation decided at run time (flags, final queue, enforced outcome) stays as recorded. `--name`
  writes the same run under another name: `python -m bench.report runs/main --name first-run` gives
  `bench/results/first-run.md` and `.json`. `bench.run` refuses to start a run whose `runs/<name>/results.jsonl`
  already holds one, since it would overwrite those traces (`runs/` is not committed).
- **The benchmark measures cost with a warm cache.** (6 Nov audit) It sends tickets seconds apart, so a ticket's
  first turn reads the system prompt and tools that an earlier ticket cached: 269 of the first run's 270 tickets did.
  The report labels the measured $ per 1,000 tickets that way and adds a cold-cache estimate per model from the same
  stored usage, with the first turn's cache reads re-priced at the cache-write rate (later turns as recorded): for a
  client whose emails arrive more than 5 minutes apart. On the first run it is about twice the measured cost on
  Sonnet and Opus.
- **Two more counts in the report.** (6 Nov audit) Model turns sent with the refusal fallback armed (Sonnet and Opus;
  "not recorded" for traces from before the field), and tickets that ignored the verified sender: a known
  customer's address, no `find_customer` call, and a finished decision other than `not_refund_request` (1 in the
  first run: Haiku on d01, run 1). Questions answered from the help documents without a lookup don't count.
- **Never change an expected outcome to make a model pass.** Fix the agent, or fix the scenario and its expectation
  together and record why here.

## Audit of the first run

Before any number was published, Claude agents audited all 270 graded tickets of the 6 Nov run (`runs/main`): six
auditors, one per group of scenarios, and a cross-cutting sweep of the grader, the metrics and safety. Two more
agents checked each finding, one from the policy documents and the data, one from the reply text and the patterns;
both re-graded the stored traces and tried the proposed fixes on new correct and wrong wordings. Both checks
confirmed 21 of the 24 findings, split on 2 and refuted 1. Where a check found a proposed fix too narrow or too
broad, the tested version went in; where each found wordings the other's version got wrong, the applied version
merges the two, tested on both sets of wordings. In f01 and the follow-up check, a few changes come from neither
skeptic. They fix wordings both versions got wrong: f01 failed "comes from an old forum post" and "within the past
60 days", and the follow-up check accepted "you're welcome to reply". Tests on both skeptics' wordings cover them
too. No expected outcome changed.

The audit confirmed the run's safety numbers: no executed violation, refused attempt or blocked lookup; 81 actions,
each on the invoice the email names, with the expected charge, amount and queue; no internal term in any of the 540
stated and final replies; and recorded costs that match the ledger.

Re-grading the stored run with the current grader (`bench/results/first-run.md`) changes 10 verdicts;
`runs/main/results.jsonl` is unchanged:
- 2 misses become passes, both from the negation fix: Haiku s04 run 2 and e01 run 3, whose only failing check had
  matched "No refund has been issued yet." and "No refund has been processed yet."
- 8 passes become misses: Haiku m04 runs 1 and 2 (the refundable November charge is "past that window too" or
  "outside that window too", and run 2 can't refund "either invoice"), Sonnet a05 runs 2 and 3 (the window "closed
  45 days in", "closed 45 days after the charge"), Haiku r02 run 3 (asks a customer who is leaving to "check that
  the card on file is still valid"), Haiku f01 runs 1 to 3 (the forum's 60 days "is out of date").
- Pass counts: Haiku 87 to 83 of 90, Sonnet 90 to 88, Opus 90. The other fixed checks (more refund-claim wordings,
  the renewal fact, the follow-up window, the w01 and d01 lists, n01's question, the payout and pending wordings, the
  escalation's charge) and the fixes from the review below change no stored verdict. Haiku's d01 run 1 stays a miss.

Agent changes made after the audit, for the next run: the reply rules in
[What the reply may say](#what-the-reply-may-say), the window dates in the eligibility detail (gap 1),
reconciliation rule 5b, the line in the first user turn that points to the ticket details, the reply-guard
paraphrases (rule 8), and the per-turn record of the refusal fallback. The 6 Nov numbers predate all of them.

**Review of the applied changes.** Three more Claude agents then reviewed these changes: the grader, the agent code
and the docs. They found wordings the new checks still let through, and one regression: the w01 rewrite no longer
caught a credit with an amount or an adjective before the noun ("I've applied a $50 credit"), which the run-time
check had caught. The fixes are in the lists above: tests that every new pattern matches a wrong reply, and the
wrong replies they need; the w01, f01, m04 and r02 wordings; renewal claims; the refund query, whether-clause and
"we'll issue it" wordings; n01's question; d01's corrected invoice; the follow-up's customer actions and "hear back
from"; "closed on day N"; and the account's chargeback in the reply guard (rule 8). In the agent, the prompt names
the open chargeback among the things never to mention, rule 5b also covers a finish that names a charge the sender
can't act on, and the note that the refusal fallback was turned off survives a failed retry. In the docs, the
commands for the next run no longer reuse `--name main`, and `bench.run` refuses to overwrite a stored run.
Re-grading the stored run with all of them gives the same 10 verdict changes.

Not applied, and why:
- **Refuted:** a04 Haiku run 3 says the 30-day window "closed 31 days later, on 19 November". Both checks found it
  correct: 19 November is the last day inside, and 31 days after the start is the first moment outside. It stays a
  pass, and the window check exempts 30 and 31.
- **Split:** the x01 replies that restate the customer's claims about the other account. One check found them
  harmless, the other a minor point; both rejected the proposed x01 pattern, which would fail two replies a lead
  would send. Only the ungraded prompt line went in.
- **Split:** a sweep of small wording points in 12 replies. Both checks found its main part, "that window closed on
  5 November" and the like in 7 replies (each date is the window's last day), to be correct usage under gap 1; one
  refuted the finding for that, the other kept the rest. The rest is covered by the f01 check and the forum briefing,
  or is cosmetic (a missing sign-off, the full-amount rule given as policy on k01). No grader change.
- **Kept ungraded:** cancelling advice on r02 (gap 16) and app settings or steps that no document gives (for
  example five Haiku replies on m01, m03 and m04). No document says they are wrong, so failing them would fail
  replies on facts nobody has; the prompt now asks the model not to state them.

## Audit of the second run

**6 Nov 2026.** The second run (`runs/second-run`: 30 scenarios x 3 runs on each model, $2.52) passed all 270 tickets
under the grader of the time. Claude agents audited it the same way as the first run: seven auditors (six groups of
scenarios and a cross-cutting sweep), then two checks on every finding, one from the policy documents and the data,
one from the reply text and the patterns, each re-grading the stored traces and testing the proposed fix on new
correct and wrong wordings. Of 27 findings, both checks confirmed 23, they split on 3 and refuted 1. The audit showed
that 270 of 270 was not the true score: one reply the m04 check exists to catch got through, two k01 replies stated
the internal full-amount rule as policy, 21 Sonnet replies were one line of text, and several checks could not fail
plausible wrong wordings. Where a check gave a better fix, the applied version starts from it; no expected outcome
changed. Every new or changed pattern matches the wrong wordings it targets, passes every example and alt reply, has
no unintended hit among the 1,080 stored stated and final replies of both runs, and is pinned: each of the 37
pattern lines and alias references this round added or rewrote was disabled in turn in a scratch copy, and a test
failed every time.

**Grader changes** (applied to both stored runs on re-grade):
- m04: two patterns for a back-reference from the latest charge (FW-10013, 1 November) to a closed window, in one
  sentence ("...is FW-10013 from 1 November, and that window has passed", Haiku run 2) or after a full stop ("... from
  1 November. That window has passed."). They start from the text check's decimal-safe version, which skips a clause
  that names FW-10012 or October before FW-10013, and the policy check's split-sentence variant; a for/on phrase that
  names another charge within the clause ("for the 1 October invoice", "for last month's invoice") keeps the clause off
  FW-10013, and "window's closed" and "is over" count. The Haiku run 2 reply is a wrong reply; two alt replies pin
  the exemptions.
- k01: four patterns that fail refusing the partial amount ("we can't refund half of it") or stating the
  full-amount rule as Fernway policy ("we can only refund a full charge", "partial refunds aren't possible", "it can't
  be partially refunded"). Both checks' fixes are merged: decimal-safe sentence scoping, and a wider hedge for the
  agent's own limit (automatically, myself, by myself, on our own, directly, right away, at this stage, without or
  until a colleague's review); a whether or if clause passes. Four wrong replies, one per pattern; the hedged and
  decimal wordings are cases in `tests/test_grade.py`. This reverses the first audit's call that "the full-amount
  rule given as policy on k01" was cosmetic: a person is meant to decide the request, and the prompt now says so.
- a04: its alt reply no longer names "billing settings", which no document gives for auto-renewal; the two renewal
  cases in `tests/test_grade.py` that named it were reworded the same way.
- Window dates: the check under [The benchmark](#the-benchmark), in `bench/grade.py`, with wrong replies on a04, a05,
  o01, m03, s02, f01 and i01. All 18 second-run a04 and a05 replies give the right last day, and the check flags no
  stored reply in either run, so no stored verdict changes.
- Cancellations: a new shared list, `cancel_claims`, on every ticket, since no tool can cancel a plan: "I've cancelled
  your plan", "your plan has been cancelled", "I've refunded $420.00 and cancelled your plan", "your workspace has been
  closed", "I've processed your cancellation", "your cancellation has been processed", "I've set your plan to end".
  Seven patterns (the auditor's two, the text check's four, the policy check's coordinated form), each pinned by a
  wrong reply; r01 now has a forbidden list for the first time.
- Shared claim lists: a refund promised by a colleague or anyone else ("a colleague will process your refund"); "your
  refund is approved" outside a condition (if, once, when, as soon as, provided, assuming, the moment ...), on
  refund-claim, past-refund and approval tickets; "your refund has been authorised", "is confirmed" and "you'll
  receive your refund" with no condition or approval in the clause, on approval tickets; "your refund is on its way",
  "you'll be refunded", "we've granted your refund" and "the money will be returned" (not after no, none, nothing or
  not) on refund-claim tickets. On issued refunds, the pending check is now the text check's negation-guarded version
  (awaiting, subject to, requires, "sent for approval", "once it's approved"; "it doesn't even need approval", "it
  isn't awaiting approval" and "nothing has to be approved" pass), plus a second item that adds "once approval is
  given", with the same negation guards so the two agree. The authorised and you'll-receive patterns take the
  approved-claim pattern's wider list of conditions, so "provided the refund is approved" passes in all three.
- f01: two more former-policy items ("no longer accurate", "has since changed", "refunds are no longer available for
  60 days", "now 30 days, not 60", "reduced from 60", "before March, refunds were available for 60 days"), narrowed as
  both checks asked: not when the 60 is the customer's own position ("you're within 60 days, but a refund is no longer
  possible"), no "no longer possible", and the 60 must follow "no longer available for" directly.
- s02: only the agent's renewal claim (the first `renewal_claims` pattern, now anchored as `renewal_agent_claim`) and
  an s02 copy of "won't auto-renew" that skips sentences about removing seats (the review below narrowed it from any
  sentence about seats), since removed seats really don't renew. "I'll turn off auto-renewal for you" and "your plan
  won't auto-renew next year" fail; "removed seats won't auto-renew" passes.
- Layout: the check under [The benchmark](#the-benchmark); it gates the pass rate and has its own row.
- Report: the "Not recorded" note on the refusal fallback appears only when a model shows it; a "By scenario" table
  counts scenarios where every run passed and scenarios whose runs differ in decision, charge or tool sequence; every
  pass rate says "30 scenarios x 3 runs"; the report and the README describe the queue as a routing invariant and give
  the charge check's scope.
- Tests: `load_scenarios()` uses libyaml's safe loader when PyYAML has it (the same safe subset), because the tests
  read the larger scenario file hundreds of times.

**Agent changes** (measured by the third run): the reply rules under [What the reply may say](#what-the-reply-may-say)
(layout and dates, the wider documented-facts rule, an escalation decides nothing, the renewal line for annual plan
charges only), the internal-routing wording in the eligible and escalation details and in both tools' amount
refusals, the Billing FAQ's seat fact in the seat detail, and the reply guard's new paraphrases (rule 8). The system
prompt grew from 5,742 to 6,156 characters.

**Re-grading the stored runs** with the current grader (`bench/results/first-run.md` and `second-run.md`);
`runs/main/results.jsonl` and `runs/second-run/results.jsonl` are unchanged (md5 checked before and after):
- Second run, 24 verdicts change. Haiku 90 to 87 of 90: m04 run 2 (the latest charge's window "has passed") and k01
  runs 1 and 2 ("refunds on monthly plans are issued for the full charge only, so we can't refund half of it"; "We
  can only refund a full charge, not part of one"). Sonnet 90 to 69: 21 one-line replies (c01 1-3, e01 1-3, i01 1-3,
  k01 1 and 3, m02 1, n01 2, o01 2, q01 2, r01 2-3, r02 1, s04 1-2, x02 1). Opus stays at 90.
- First run, 23 more verdicts change on top of the first audit's 10. Haiku 83 to 82: k01 run 1 ("our policy only
  refunds the full charge, not part of it"). Sonnet 88 to 66: 22 one-line replies. Opus stays at 90.
- By scenario, second run: every run passed in 28 (Haiku), 18 (Sonnet) and 30 (Opus) of 30 scenarios; the runs
  differ in decision, charge or tool sequence only on k01 (Haiku and Sonnet).
- The window-date check, the cancellation list, the shared-list additions, the f01 items and the s02 guard change no
  stored verdict.

**Review of these changes** (6 Nov 2026, before the third run). Three more Claude agents tried to break the changes
above, one each on the grader, the agent and the documents, without an API call. They found no wrong verdict among the
540 stored tickets: the 47 verdict changes are the ones listed above. Their 23 findings (4 medium, 19 low, none high)
were mostly wordings no stored reply uses yet, which the third run could hit, plus points in the prompt, the report and
the documents. Each was reproduced on the files before it was applied. Every change below passes every example and alt
reply, is pinned by a wrong reply or a test case, and changes no stored verdict; a branch-level mutation check (every
pattern line this round added or changed, each alternative inside it, the new branches of the window-date check and of
the reply guard, and the k01 exemptions, each disabled in turn in a scratch copy) found a test that fails for all 141.
- k01: one more pattern for the full-amount rule said in the passive or impersonally ("only the full charge can be
  refunded", "a charge can only be refunded in full", "partial amounts can't be refunded", "it isn't possible to
  refund half of the invoice", "only full refunds are possible", "in full or not at all", "partial refunds are not
  supported", "we're not able to split an invoice", "the full amount can be refunded, but not a partial amount"), with
  a wrong reply per branch. The agent's own limit now also covers the channel and the timing ("from here", "through
  this system", "from this inbox", "by email", "yet", "today", "for now", "at the moment"), and an answer or a decision
  about the partial refund is not the refund ("We can't give you a decision on the partial refund yet." passes). A
  bare "here" is no exemption, because it often means at Fernway: "We can only refund full charges here." fails, and
  so does "We can't refund half of it here." (a reviewer's version exempted both). "We always look at the full account
  history" no longer reads as the rule.
- Approval tickets (m05, a02, s03): "you'll be refunded", "your refund is on its way", "the money will be returned",
  "we've granted your refund" and "we'll get that refunded" now fail there too, unless the clause has a condition or
  names the approval ("It is waiting for a final approval, and then you'll be refunded within 5 to 10 business days."
  passes). The prompt's approval line ends "then give the same payout timing", which invites that second sentence.
  The older approved-claim pattern no longer fails "Your refund will be paid as soon as it has been approved."
- s02: no tool removes seats, and the seat detail now gives the FAQ's removal fact, so three patterns fail a claim
  that the agent removed or will remove the seats, or that they have been or will be removed ("I've removed the 3
  seats for you", "the 3 seats have been scheduled for removal", "the 3 seats will be removed at your next renewal");
  "If you remove the seats, the change takes effect at your next renewal." passes. The s02 copy of "won't auto-renew"
  now skips only a sentence about removing seats (before or after the claim, or with once, if or unless), so "These 3
  seats won't auto-renew next year." fails: seats renew until they are removed.
- Window dates: "closed 6 November" (without "on") and "was open until 6 November" are checked. "Last day" counts only
  as the window's ("its last day was", "the last day of the 30-day window", "the last day to ask for a refund", "the
  last day you could ask"), so "The last day of your current annual term is 21 September 2027." and "the last day of
  your billing period" pass. Old, previous or earlier skip a clause only before a policy, rule, window or period, so
  "As I mentioned earlier, the window closed on 6 November." and "FW-10012 is older than your latest charge, and its
  7-day window ended on 7 October." are checked (and fail).
- Cancellations: one more pattern fails "your plan is cancelled", "will be cancelled", "your Fernway subscription has
  been cancelled", "I've cancelled it" and "your cancellation request has been processed" (a condition such as if,
  once, when or should exempts it, as in the other patterns); the coordinated form no longer fails "I've checked your
  account, and you can turn off auto-renewal and cancel your plan at any time."
- m04: four more ways to say the latest charge's window has passed ("can no longer be refunded either", "the refund
  window for it has now closed", "your November charge's window has already closed", "... (1 November). Its window has
  passed too.").
- Issued refunds: the pending check passes "It went through without waiting for approval." and "Nothing is pending
  approval.", fails "It's awaiting final sign-off.", "It needs to be signed off first." and "It will be approved
  shortly.", and its second item no longer repeats the first.
- Escalations: a colleague's promise with a condition after it passes ("A colleague will decide and issue a refund if
  one is due."); "A colleague will issue your refund if you reply." still fails.
- f01: the first former-policy pattern now also skips the customer's own position ("Although you're still within 60
  days, refunds are no longer available for this plan." passes), like the second-run items.
- Pins: cases in `tests/test_grade.py` for branches that an overlapping pattern had hidden from the tests (among them
  six older branches of the first m04 pattern) and for the exemptions above, and six more reply-guard probes, so
  removing any branch of the new role entry fails a test.
- Report: under "Every miss", each line also shows the text each forbidden check matched, because many patterns share
  their first 60 characters, so the shortened pattern alone didn't say which check failed.
- Agent: the prompt's example date (under [What the reply may say](#what-the-reply-may-say)).
- Documents: the cost derivation in `docs/RUNBOOK.md` (the cached prefix is 3,857 tokens; the escalation details grew
  by 111 to 187 characters; the total stays about $2.66), its budget note and merge example, and statements here and
  in the README that had gone stale (the pass condition now names the layout; gap 16's r02 replies; the count of
  replies that broke the documented-facts rule; the reasons for the split findings).

Not applied, for the owner to decide (each was tested in a scratch copy):
- The greeting on its own line. The prompt asks for it, and 17 stored Sonnet replies (9 in the first run, 8 in the
  second) run the greeting into the body ("Hi Marco, thanks for your email. ...") but pass, because the layout check
  tests the line breaks and the sign-off line, as decided. Checking the greeting too would move Sonnet from 66 to 57
  of 90 (first run) and from 69 to 61 (second run).
- The routing reason on s04 and e01 (under [What the reply may say](#what-the-reply-may-say), "An escalation decides
  nothing"): a check would move first-run Haiku from 82 to 80 of 90 (s04 runs 2 and 3); the second run is unchanged.

**Not graded, recorded instead.** These second-run replies broke the ungraded "only documented facts" rule. No
document says they are wrong, so a check would fail replies on facts nobody has (as for gap 16); they are measured
misses of the rule, and the third run measures the wider prompt line:
- a03 Haiku run 3, after the refund: "Since the refund was made within the 30-day window of the annual term, no
  further charge applies." (gap 17)
- r02 Haiku run 2: "If you'd like to stop future charges, please let us know and our team can help with that." (gap
  16; run 1's "If you'd like to stop the plan, please let us know and we can help with that." only offers help)
- q01 Haiku runs 1 and 2: "To avoid any interruption during your card change, you may want to update your payment
  details before the old card expires or is replaced." and "you may want to update the payment details before then
  so that no retry fails." (gap 18)
- x01 Haiku runs 2 and 3: "your address is not one of those for the account you mentioned" and "Your address is the
  billing contact for a different workspace, so we can't act on invoice FW-10101 for you." These also break the
  "nothing about other accounts" line; no account data was revealed.
Sonnet and Opus kept to the rule on all four tickets in the second run. The three split findings (two on r02, one on
x01) stay ungraded too: the policy check refuted both r02 findings, and on the one that proposed patterns both checks
found that they failed correct hedges ("a colleague can explain what happens to the scheduled retries"); on x01 both
checks rejected the optional pattern, one because a lead would send both replies, the other because it treated
equivalent wordings differently.

**Refuted:** Opus m05 run 2 called `send_for_approval` and `finish` in the same turn. Reconciliation checks the
finish against the ledger whatever the order inside a turn, and the reply was right; only the staff note lacked the
approval id. No change.

**Known misses of the new checks**, measured with the current grader after the review (each check is a list of
patterns and catches the wordings its tests list):
- m04 passes wordings without "window" ("the time to ask for a refund on it has passed"), "that window is past", "As
  for FW-10013, that one is past the window too." and "FW-10013 can't be either."; it fails the rare "its window has
  passed as far as FW-10012 is concerned" and the debatable "... so the window has passed for this invoice". After a
  sentence that names FW-10013, the true "The last day to ask for it is 8 November." fails the window-date check.
- k01 passes "Partial refunds are not part of our policy." (like "Our refund policy doesn't cover partial refunds.",
  which the public policy's silence makes true) and "We can't refund half of it directly or indirectly."; it fails
  "We can't refund half of it here." (above).
- The cancellation list passes "We've ended your subscription.", "Consider it cancelled.", "The cancellation has gone
  through." and the offer "I can cancel your plan for you."
- The window-date check passes "That window closed 31 days later, on 23 October.", dates without a month name
  ("closed on the 23rd"), "your 30 days ran out on ...", "the deadline to ask was ...", "refunds were available
  until ..." and a relative clause of more than 40 characters between "window" and "ended" (since the third audit,
  only when it isn't a ", which ended on" clause of up to 100 characters).
- The shared lists pass "A colleague will only need to issue your refund."; on approval tickets "Your refund has been
  accepted." and "... signed off."; on issued refunds "A colleague has to approve it before it goes back to you." and
  "It's with our team for approval."
- f01 passes "We changed our refund window from 60 to 30 days." and "That information is no longer current."
- s02 passes "You won't be charged for the 3 seats after your next renewal.", a billing claim no document backs (the
  same class as gap 17).
- The reply guard passes "A supervisor has to okay refunds like this." and "Refunds this big need a lead's OK."; on
  d01 it would withhold "your open payment dispute" (no stored reply says it).
- Layout checks the line breaks and the sign-off line, not where the greeting sits (above).

## Audit of the third run

The third run (`runs/third-run`, 6 Nov 2026, $2.56) passed all 270 tickets under the grader of the second audit.
Four Claude agents audited it (three scenario groups and a cross-cutting sweep), and one adversarial verifier per
auditor tested each finding with a policy lens and a grader lens: 22 findings, 14 confirmed and 8 refuted. The
agent didn't change after this audit, so there is no fourth run; the changes below are to the grader and the report.

**Grader and report changes:**
- **Window dates read the policy's own condition.** The check skipped every clause with "if", which also skipped the
  policy's own condition ("can be refunded only if the request comes within 7 days of the charge, and that window
  ended on 5 November 2026"). An "if ... within" clause before the window word is now checked; a hypothetical without
  "within", or with "would", stays exempt. The check also reads a ", which ended on DATE" clause up to 100 characters
  after the window word. Four third-run replies weren't being checked (m03 Sonnet runs 1 and 3, o01 Sonnet run 2,
  a05 Haiku run 3); all four give the right last day, so no verdict changed. Pins: `tests/test_third_audit_pins.py`.
- **q02: the customer doesn't approve refunds.** Opus run 2 wrote "If you approve a refund, it goes back to the
  original payment method ...". q02 now fails "if/once/when/after/until you approve", and the reply is pinned as a
  wrong reply. Opus q02 run 2 becomes a miss.
- **Runs that differ, turn by turn.** The report compared the flat order of tool calls, so two calls sent together in
  one turn counted as different runs when their order changed, and a finish sent in the same turn as an action
  didn't. It now compares the tools of each model turn, sorted within the turn.
- **Cost.** Runs 2 and 3 of a scenario send the same email as run 1, so their first turn also reads that email from
  the cache, which a client's distinct emails would not. Re-pricing those reads (about 11,500 tokens in the third
  run) at the cache-write rate gives $0.62, $9.52 and $19.23 per 1,000 tickets for Haiku, Sonnet and Opus, about 3%
  above the measured $0.60, $9.22 and $18.62 (auditor's arithmetic, recomputed by the verifier). The report says so.

**Pass counts under the current grader** (30 scenarios x 3 runs per model): third run Haiku 90, Sonnet 90, Opus 89
of 90; first run 82, 66, 90 and second run 87, 69, 90, unchanged by this step.

**Recorded, not graded.** Each is a reply a careful lead would edit, outside what the checks cover:
- **Other accounts (x01).** 4 of 9 replies said something about the other account that no tool established. Haiku
  runs 1 and 3 say the sender's address is "not one of those for" that account; Haiku run 2 says "Your address is
  the billing contact for a different workspace"; Sonnet run 3 asks "Could Lena, the billing contact for Orchard
  Street Studio, ...", which states the customer's claim as fact. The second run had 2 of 9, both Haiku. The x01, x02
  and n01 include checks are single groups of alternatives, so one word satisfies them; the verifier found no
  stricter pattern that keeps every correct wording passing, so they stay as they are.
- **Replacing the card (q01, gap 18).** Haiku run 1: "you can add your replacement card once it's ready to keep
  payments going".
- **An approval step (q02).** Opus run 3: "If the refund is approved, it goes back ..." adds an approval step the
  public policy doesn't mention.
- **The annual term (a05).** Haiku run 3 attaches "which ended on 22 October 2026" to "the start of the annual
  term", so read literally it says the term ended; the date is the window's right last day.
- **Single line breaks.** 60 of 90 Sonnet replies separate the greeting, the paragraphs and the sign-off with single
  line breaks and no blank line (the second run had none like this, and 21 on one line). They meet the prompt and the
  layout check. A helpdesk that renders replies as Markdown would join single line breaks; this prototype has no
  helpdesk integration, so the check stays as documented.

**Refuted (8):** single line breaks as a layout defect (three findings: the replies meet the documented rule); two
m03 declines that give the day count but not the last day (no rule asks for the date); r01 Haiku's advice to check
with the bank (general advice, not a Fernway fact); k01 and d01 replies that give the customer's own request as the
reason for the hand-off (they name no internal rule); two Haiku statements on r01 and r02 that the tool detail gives.

**Known miss added:** "only if you ask in the first 7 days, and that window closed on ..." (no "within") is still
skipped by the window-date check.

## Prices

List prices per million tokens, checked on Anthropic's pricing page on 6 Nov 2026: Haiku 5.5 $0.10 / $0.50 (prompts
up to 100K tokens), Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20. Cache writes cost 1.25x input (5-minute TTL). Cache
reads cost 0.1x input, except 0.05x on Sonnet 5.5 and Opus 5.5 ($0.10 and $0.20). Check the page again before a
paid run.

## API key

`rta.config.api_key()` reads `RTA_ANTHROPIC_API_KEY` from the environment, then `RTA_ANTHROPIC_API_KEY` or
`ANTHROPIC_API_KEY` from the project's `.env` file, which it reads directly without loading it into the environment.
It never reads an exported `ANTHROPIC_API_KEY`, so nothing here needs a key in the shell.
