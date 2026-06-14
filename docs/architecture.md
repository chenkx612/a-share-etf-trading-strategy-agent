# Framework Architecture

`src/quant_core/` is the framework package. It provides reusable infrastructure for data access, factor calculation, strategy selection, backtesting, reporting, storage, paths, and the CLI.

The framework must not store skill state, prompt content, generated artifacts, task-specific SOPs, default trading pools, prompt references, or quantitative trading business knowledge.

## Modules

- `data/`: providers, market data normalization, validation, and incremental merge helpers.
- `factors/`: reusable factor formulas and factor table construction.
- `strategy/`: selection, scoring, filtering, and target-weight logic.
- `backtest/`: simulation, return series, turnover, costs, and performance metrics.
- `reporting/`: framework-level report builders and formatting helpers.
- `config.py`: shared defaults and typed configuration.
- `paths.py`: root-relative framework path conventions.
- `storage.py`: table and JSON read/write helpers.
- `cli.py`: command-line orchestration over framework modules.

## Dependency Direction

Skills may import and call `quant_core`. `quant_core` must not import from `.claude/skills/`.

CLI commands should accept an explicit `--root` when they create `data/` or `outputs/` artifacts. Skills are responsible for passing a root under their own `outputs/` directory.

## Boundary

Framework code may provide atomic capabilities such as table storage, provider normalization, factor calculation, optimization grids, and backtest execution.

Skill scripts compose those atomic capabilities into low-discretion SOP steps. Skill scripts own business defaults such as candidate counts, ETF pool definitions, semantic theme rules, parameter grids, and final report schemas.
