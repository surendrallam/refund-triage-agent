# Runbook

## 1. Set up

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
source .venv/bin/activate
cp .env.example .env            # then paste your Claude API key after RTA_ANTHROPIC_API_KEY=
```

`rta.config.api_key()` reads the key from `.env`. Never export `ANTHROPIC_API_KEY` in your shell: the code
doesn't read it from there, and other tools on your machine would pick it up.

## 2. Tests

```bash
python -m pytest -q             # offline: no key, no network
```

They cover the rules at every day boundary, the tool guards, reconciliation, the agent loop with a fake Anthropic
client (including the 8-turn cap), the real SDK client over an in-process mock transport (the request on the
wire), the grader, and the scenario file against the rules.

## 3. The command line

Free, no API call:

```bash
rta charges --from ana@blueheron.example --today 2026-11-04    # each charge and what the rules say about it
rta scenarios                                                  # the benchmark tickets
```

One ticket through the agent (paid; a fraction of a cent on Haiku, an estimate from section 5):

```bash
rta triage --scenario m01                                      # a benchmark ticket, graded
rta triage --scenario i01 --model opus                         # the prompt-injection ticket on Opus
rta triage --from omar@kestrelworks.example --subject "Refund" \
  --body "Please refund invoice FW-10041, our annual plan." --today 2026-11-06
rta triage --from ... --subject ... --body-file email.txt --json   # full trace as JSON
```

Options: `--model haiku|sonnet|opus` (default haiku), `--effort low|medium|high|xhigh|max` (default low),
`--today YYYY-MM-DD` (default 2026-11-06, the date the sample data is frozen at), `--budget USD` (default 0.5).
`RTA_MODEL`, `RTA_EFFORT` and `RTA_TODAY`, in the shell or in `.env`, change the defaults.

The CLI writes every trace to `logs/<ticket>.json` and appends every API call to `runs/costs.csv`. The ledger
refuses any call whose worst case could pass `--budget` or the file's total (`RTA_BUDGET_USD`, default $40). The
ticket then stops, goes to a person, and still gets its trace; the CLI exits with code 3. While a server-side
fallback can run, an Opus 5.5 call reserves up to about $0.44 (DECISIONS.md), so a long Opus ticket can reach the
default $0.50: use `--budget 1` for Opus.

## 4. Benchmark

Smoke test first (5 tickets on Haiku, estimated at about $0.01):

```bash
python -m bench.run --models haiku --runs 1 --ids m01,a02,c01,i01,q01 --budget 0.2 --name smoke-3
```

Full run (30 scenarios x 3 runs x 3 models = 270 tickets; the third run cost $2.56, against an estimate of about
$2.66 worked out from the second run's measured usage in section 5). Give a new run its own name:

```bash
python -m bench.run --models haiku,sonnet,opus --runs 3 --budget 7.5 --concurrency 4 --name my-run
python -m bench.report runs/my-run     # writes bench/results/my-run.md and my-run.json
```

Give every run a new `--name`. A run writes `runs/<name>/results.jsonl` and `summary.json` from scratch and appends
to its `costs.csv`, so `bench.run` refuses to start when `runs/<name>/results.jsonl` already holds a run.
`runs/main`, `runs/second-run`, `runs/smoke`, `runs/smoke-so` and `runs/smoke-2` hold the 6 Nov runs, and `runs/` is
not committed, so nothing else keeps them.

`runs/<name>/results.jsonl` holds one line per ticket run with its full trace; `summary.json` holds the per-model
metrics and `spent_usd`, everything the run's ledger recorded; `costs.csv` holds every call. Each run has its own ledger
file, so `--budget` is the cap that counts for a run, as long as `RTA_BUDGET_USD` (default $40, settable in `.env`) is
at least the `--budget`: the ledger refuses a call that would pass either. The total applies to each ledger file
separately and does not add up across runs. A run stops early when the next call could pass `--budget`, or on an API
error every ticket would hit (a bad key, a rejected request). The ticket that hit it stays in `results.jsonl`; a ticket
the budget cut off part way is left out of the metrics and counted as a budget stop.

`bench.report` grades every stored trace again with the current grader and scenarios, so the verdicts in a report match
them when it is written, and it makes no API call. What reconciliation decided at run time (flags, final queue, enforced
outcome) stays as the run's code produced it: under today's rule 5b, Haiku's d01 run 1 would also count as an
inconsistent finish. It never edits `results.jsonl`: every ticket whose verdict differs from the one stored when it ran
is listed under "Verdicts changed by re-grading", with the grader's note, and each miss shows the text each forbidden
check matched. It merges runs only when they cover the same scenario ids. `--name` sets the name of the files it writes,
which is the first run directory's name by default:

```bash
python -m bench.report runs/main --name first-run    # bench/results/first-run.md and first-run.json
python -m bench.report runs/third-run runs/third-run-extra --name third-run-merged   # two runs in one report
```

`first-run.md` (`runs/main`) and `second-run.md` (`runs/second-run`) are the two 6 Nov runs re-graded with the current
grader (DECISIONS.md, "Audit of the first run" and "Audit of the second run"). Under it the pass counts, each over 30
scenarios x 3 runs, are Haiku 82, Sonnet 66 and Opus 90 of 90 for the first run, and Haiku 87, Sonnet 69 and Opus 90
of 90 for the second; Sonnet loses 22 and 21 one-line replies to the layout check. Run the same commands again after
any change to `bench/grade.py`, `bench/scenarios.yaml` or the reply guard in `rta/policy.py`.

The report's cost row is a warm-cache measurement: the benchmark sends tickets seconds apart, so each ticket's
first turn reads the cached system prompt and tools. The row below it is an estimate for a cold cache
(tickets more than 5 minutes apart), from the same stored usage with the first turn's cache reads priced as cache
writes. Paste the table from `bench/results/<name>.md` into the README under `<!-- RESULTS -->`.

## 5. What a full run should cost (estimate)

**Measured.** The first run cost $2.51 and the second $2.52 (`runs/second-run/summary.json`). With a warm prompt
cache the second run measured $0.57, $8.79 and $18.62 per 1,000 tickets on Haiku, Sonnet and Opus; the report's
cold-cache estimate from the same usage is $1.03, $18.38 and $37.54.

**What changed for the third run (measured on the code):** the system prompt grew from 5,742 to 6,156 characters
(+414: the layout and date line, the documented-facts and escalation wording, the seat exception to the renewal line).
The tool definitions stay at 5,496 characters of JSON. Three kinds of tool result grew: an eligible charge's detail
by 201 characters (the partial-amount routing note), the escalation details by 111 to 187 (the internal-routing note;
the EUR detail also lost its "The policy allows a full refund" clause) and a seat decline past 30 days by 47 (the
Billing FAQ fact). The review of these changes replaced the prompt's example date with one of the same length.

**Estimate for the third run** (arithmetic on the second run's recorded usage in `runs/second-run/results.jsonl`, at
the list prices in `rta/config.py`):

- The second run's cached prefix (its 5,742 characters of system prompt and 5,496 of tools, 11,238 in all, plus the
  API's tool-use preamble) was 3,857 tokens: the 83 first turns that found only the prefix in the cache read exactly
  that (the others also read the email of an earlier run of the same scenario). That is 2.91 characters a token, so
  +414 characters is about +142 tokens, read from the cache on every model turn: 353, 343 and 347 turns on Haiku,
  Sonnet and Opus.
- The longer tool results, at the same rate, are written to the cache once and read by every later turn of their
  ticket: per model about 2,400 to 2,550 more cache-write tokens and 1,800 to 1,860 more cache-read tokens, counted
  ticket by ticket from the stored traces. The changed prefix (about 4,000 tokens) is also written once more per
  model.
- Output is assumed 5% longer for the line breaks (an assumption).

| Model | Second run (measured) | Added (estimate) | Third run (estimate) |
|---|---|---|---|
| Haiku 5.5 | $0.052 | $0.003 | $0.054 |
| Sonnet 5.5 | $0.791 | $0.044 | $0.835 |
| Opus 5.5 | $1.676 | $0.091 | $1.767 |
| **Full run** | **$2.52** | **$0.14** | **about $2.66** |

**Measured:** the third run cost $2.56: Haiku $0.054, Sonnet $0.829, Opus $1.676 (`runs/third-run/summary.json`).

**High case** (an assumption, not a measurement): every ticket's first turn misses the cache and writes the whole
prefix, and the output doubles: about $6.90.

**Budget.** `--budget 7.5` covers twice the estimate ($5.32) plus what the ledger reserves for calls still in flight:
up to about $0.44 per Opus call while a server-side fallback can run, 4 at a time, about $1.76 ($7.08 in all). It does
not cover the high case plus those reservations ($8.66): then the ledger stops the run before it passes $7.50 and the
report marks it incomplete.

Prices are the list prices in `rta/config.py`, checked on Anthropic's pricing page on 6 Nov 2026. Haiku 5.5 is billed
at a higher rate for prompts over 100,000 tokens; these prompts stay far below that.

**Planning other settings.** Before the first run, the estimate was built from prompt sizes and assumed token counts:
a 3,800-token cached prefix, 5 turns and 1,250 output tokens a ticket gave about $5.60 for a full run (high case
$11.50). The second run came in at under half of that: its tickets averaged 3.8 to 3.9 model turns instead of 5, and
510 to 590 output tokens instead of 1,250 (`runs/second-run/results.jsonl`). For another effort level or model mix,
start from the measured per-ticket costs above and scale the output.

## 6. Changing the rules or the tickets

- A rule change goes in `rta/policy.py` with a boundary test in `tests/test_policy.py` and a line in DECISIONS.md.
- A new ticket goes in `bench/scenarios.yaml` with its expected outcome, a `basis`, reply checks and an example
  reply. `python -m pytest tests/test_scenarios.py` checks it against the rules and the grader.
- Never change an expected outcome to make a model pass. Fix the agent, or fix the ticket and its expectation
  together and record why.
