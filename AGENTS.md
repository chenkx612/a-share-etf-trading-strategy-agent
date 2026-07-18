# Repository Guidelines

## Project Mission

This project is a **loop/harness engineering platform for automated quantitative
trading strategy R&D**, not just a collection of indicators, backtests, or one-off
research scripts. Its primary goal is to turn strategy development into a
repeatable closed loop:

```text
define task and constraints
→ generate or modify a candidate strategy
→ run deterministic tests and backtests
→ evaluate development and gate metrics
→ accept or reject the candidate
→ persist evidence and research memory
→ continue until the target or budget is reached
```

The agent or coding model explores hypotheses and implements one candidate at a
time. The deterministic Python harness owns orchestration, data boundaries,
evaluation, version isolation, budgets, recovery, and stopping conditions.
Preserve this separation: model output may propose strategy logic, but it must
not be the authority that scores or promotes its own candidate.

Favor work that strengthens the automated research loop: explicit contracts,
reproducible experiments, objective gates, isolated candidates, resumable state,
auditable artifacts, and safe long-running execution. A strategy improvement is
not complete merely because a backtest looks better; it must be reproducible and
pass the configured out-of-sample gates and hard constraints without weakening
the evaluator.

## Project Structure & Module Organization

`src/quant_core/` contains the framework package and CLI entry point. Core modules
are split by responsibility: `data/` for market data download/cache and table IO
helpers, `factors/` for factor calculation, `strategy/` for selection logic,
`backtest/` for deterministic simulation, and `research/` for the loop/harness
contracts, candidate runner, evaluator, workspace isolation, state machine, and
final reporting. Each strategy module owns its trading logic and tunable
parameter defaults. Fixed simulation settings live in `config.py`, and
lightweight report output lives in the CLI.

`tests/` holds pytest coverage for framework and harness behavior. `docs/`
contains framework development documentation only; do not put quantitative
trading business knowledge there. Skill-specific prompts, knowledge, scripts,
templates, stock-pool definitions, and intermediate outputs live under
`.agents/skills/<skill-name>/`. Managed research keeps task-level Champion state
at `.research/<task-id>/champion.json` and immutable Loop history under
`.research/<task-id>/runs/<run>/rounds/<round>/`. It must remain isolated from
normal framework outputs.

## Build, Test, and Development Commands

- `python3 -m pip install -e ".[dev]"`: install the package in editable mode with pytest.
- `pytest`: run the full test suite.
- `python3 -m quant_core.cli data update --universe path/to/universe.csv --start 2024-01-01 --end 2024-12-31`: fetch/update ETF daily data.
- `python3 -m quant_core.cli factor compute --start 2024-01-01 --end 2024-12-31`: compute factor tables.
- `python3 -m quant_core.cli backtest run --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31`: run the Sharpe/correlation/threshold strategy backtest.
- `python3 -m quant_core.cli --root .agents/skills/etf-sharpe-topk/outputs/sector_rotation recommend today --universe .agents/skills/etf-sharpe-topk/outputs/sector_rotation/selected_universe.csv --date 2024-12-31 --top-n 10`: generate skill-scoped recommendations.
- `python3 -m quant_core.cli research run-once --task path/to/task.toml --experiment-id experiment-001 --output path/to/experiment`: run one candidate-development experiment without champion management.
- `python3 -m quant_core.cli research loop --task path/to/task.toml --research-root .research`: run the resumable automated strategy-research loop until its configured target or budget stops it.
- `python3 -m quant_core.cli research clean --task path/to/task.toml --research-root .research`: remove disposable worktrees, derived development data, and redundant successful-run diagnostics without deleting research decisions or the champion.

## Loop & Harness Engineering Rules

- Keep candidate strategy code separate from fixed evaluators, backtest metrics,
  data splits, promotion rules, and stopping logic.
- Treat `task.toml`, experiment `result.json`, decisions, event logs, patches,
  metrics, and loop state as explicit versioned contracts. Validate inputs early
  and write state atomically where interruption is possible.
- Prevent gate leakage. Candidate development may use only its configured
  development inputs; exact gate-period metrics must not feed later research
  rounds. Final reports may inspect gate results only after the loop stops.
- Run candidates in isolated worktrees or equivalent disposable workspaces.
  Rejected, failed, or interrupted rounds must not contaminate the next
  round or the user's current branch and index.
- Promotion is harness-owned. A candidate becomes champion only after fixed
  tests, hard constraints, objective comparison, and configured improvement
  thresholds pass.
- Design long-running flows for timeout, interruption, retry, and resume. Budget
  exhaustion and rejected hypotheses are normal terminal or research outcomes,
  not reasons to silently bypass gates.
- Preserve auditability: an experiment should be explainable from its frozen
  inputs, code diff, logs, metrics, decision, and parent champion.
- Keep durable evidence separate from disposable runtime state. Store caches
  under `.cache/`, worktrees under `.tmp/`, retain detailed logs for failures,
  and avoid persisting duplicate or empty success artifacts.
- Never create ad-hoc research roots such as `.research/clean-run` to separate
  Loop invocations. Reuse the configured task root; the Harness allocates
  `runs/001`, `runs/002`, and subsequent Run directories automatically.
- Emit concise stage events to stdout and `.tmp/runs/<run>/events.jsonl` while a
  Loop is active so an external Codex supervisor can observe progress without
  requiring permanent successful-session traces.
- Do not turn the harness into an automatic deployment or live-trading system
  without an explicit, separately reviewed scope change.

## Coding Style & Naming Conventions

Use Python 3.11+ syntax and keep modules typed where practical. Follow the existing style: 4-space indentation, `snake_case` functions and variables, `PascalCase` classes, and explicit imports. Prefer small, pure functions that can be tested with in-memory pandas data. Keep comments brief and limited to non-obvious financial or data-handling logic.

## Testing Guidelines

Tests use `pytest` and pandas fixtures built directly in test files. Name test
files `test_*.py` and test functions `test_*`. Add tests when changing factor
formulas, strategy scoring, backtest metrics, storage behavior, research
contracts, candidate isolation, promotion decisions, loop recovery, stopping
conditions, or CLI-visible workflows. Prefer deterministic sample data and fake
agent runners over live market calls or real model sessions. For harness changes,
test both the successful path and at least the relevant reject, failure,
timeout, or interruption path.

## Commit & Pull Request Guidelines

This checkout does not include Git history, so no project-specific commit convention is available. Use concise, imperative commit subjects such as `Add Sharpe strategy report` or `Fix backtest weight normalization`. Pull requests should describe the behavior change, list commands run, note affected skill outputs, and link relevant issues or docs. Include report excerpts when changing generated reporting output.

## Security & Configuration Tips

Do not hard-code credentials, local absolute paths, or private data sources. Use `--root` to isolate experiments from the repository root, and keep generated `data/` and `outputs/` artifacts scoped to the intended skill output directory.
