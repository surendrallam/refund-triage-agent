# Notes for Claude Code

- This repo is public. Don't add personal contact details (email, phone, personal websites), employer or client
  names, or real people's names.
- Fictional data only: Fernway and its customers are invented, addresses use the reserved `.example` domain, and
  people have first names only. Keep it that way in `data/`, `kb/` and `bench/`.
- API key: read through `rta.config.api_key()` from `RTA_ANTHROPIC_API_KEY` in the project's `.env`. Never set or
  export `ANTHROPIC_API_KEY` in the shell, and never print, log or commit a key.
- Run `python -m pytest -q` before every commit. The tests need no key and make no network calls.
- Rules live in `rta/policy.py`. A rule change needs a boundary test and a line in DECISIONS.md.
- Never change an expected outcome in `bench/scenarios.yaml` to make a model pass. Fix the agent, or fix the
  ticket and its expectation together and record why in DECISIONS.md.
- Every API call spends money: state the expected cost before running the benchmark (docs/RUNBOOK.md, section 5),
  and pass `--budget`.
- Keep README claims to what the code and the committed results in `bench/results/` show.
