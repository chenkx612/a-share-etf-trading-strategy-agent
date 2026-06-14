# Repository Guidelines

## Project Structure & Module Organization

`src/quant_core/` contains the framework package and CLI entry point. Core modules are split by responsibility: `data/` for providers, `factors/` for factor calculation, `strategy/` for selection logic, `backtest/` for simulation, and `reporting/` for generated reports. Shared configuration and path helpers live in `config.py`, `paths.py`, and `storage.py`.

`tests/` holds pytest coverage for framework behavior. `docs/` contains framework development documentation only; do not put quantitative trading business knowledge there. Skill-specific prompts, knowledge, scripts, templates, stock-pool definitions, and intermediate outputs live under `.claude/skills/<skill-name>/`.

## Build, Test, and Development Commands

- `python3 -m pip install -e ".[dev]"`: install the package in editable mode with pytest.
- `pytest`: run the full test suite.
- `python3 -m quant_core.cli data update --universe path/to/universe.csv --start 2024-01-01 --end 2024-12-31`: fetch/update ETF daily data.
- `python3 -m quant_core.cli factor compute --start 2024-01-01 --end 2024-12-31`: compute factor tables.
- `python3 -m quant_core.cli backtest run --universe path/to/universe.csv --strategy sharpe-single --start 2024-03-01 --end 2024-12-31`: run a named strategy backtest.
- `python3 -m quant_core.cli --root .claude/skills/etf-pool-automation/outputs/sector_rotation recommend today --universe .claude/skills/etf-pool-automation/outputs/sector_rotation/selected_universe.csv --date 2024-12-31 --top-n 10`: generate skill-scoped recommendations.

## Coding Style & Naming Conventions

Use Python 3.11+ syntax and keep modules typed where practical. Follow the existing style: 4-space indentation, `snake_case` functions and variables, `PascalCase` classes, and explicit imports. Prefer small, pure functions that can be tested with in-memory pandas data. Keep comments brief and limited to non-obvious financial or data-handling logic.

## Testing Guidelines

Tests use `pytest` and pandas fixtures built directly in test files. Name test files `test_*.py` and test functions `test_*`. Add tests when changing factor formulas, strategy scoring, backtest metrics, storage behavior, or CLI-visible workflows. Prefer deterministic sample data over live market calls.

## Commit & Pull Request Guidelines

This checkout does not include Git history, so no project-specific commit convention is available. Use concise, imperative commit subjects such as `Add Sharpe strategy report` or `Fix backtest weight normalization`. Pull requests should describe the behavior change, list commands run, note affected skill outputs, and link relevant issues or docs. Include report excerpts when changing generated reporting output.

## Security & Configuration Tips

Do not hard-code credentials, local absolute paths, or private data sources. Use `--root` to isolate experiments from the repository root, and keep generated `data/` and `outputs/` artifacts scoped to the intended skill output directory.
