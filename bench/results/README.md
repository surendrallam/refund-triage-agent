# Benchmark results

`python -m bench.report runs/<name>` writes `<name>.md` and `<name>.json` here; with `--name <label>` it writes
`<label>.md` and `<label>.json` instead. Every stored trace is graded again with the current grader, and each verdict
that differs from the one stored at run time is listed with the grader's note. Reconciliation (flags, final queue,
enforced outcome) stays as recorded at run time.

`third-run.md` and `third-run.json` are the published results: the third 6 Nov 2026 run (`runs/third-run`), on the
agent after two audits, graded with the current grader after the third audit (DECISIONS.md, "Audit of the third run").
`first-run.md` and `second-run.md` (with their `.json`) are the two earlier runs (`runs/main`, `runs/second-run`),
re-graded with the same grader, so all three use the same checks. Every pass rate is over 30 scenarios x 3 runs per
model.
