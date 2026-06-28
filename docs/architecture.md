# Framework Architecture

`src/quant_core/` is the framework package. It provides reusable infrastructure for data access, factor calculation, strategy selection, backtesting, and the CLI.

The framework must not store skill state, prompt content, generated artifacts, task-specific SOPs, default trading pools, prompt references, or quantitative trading business knowledge.

## Modules

- `data/`: explicit universe loading, root-relative data/output paths, table IO, ETF daily data download, standardization, validation, and local cache refresh helpers.
- `factors/`: reusable factor formulas and factor table construction.
- `strategy/`: selection, scoring, filtering, and target-weight logic.
- `backtest/`: simulation, return series, turnover, costs, and performance metrics.
- `config.py`: shared defaults and typed configuration.
- `cli.py`: command-line orchestration over framework modules, including lightweight report file output.

## Dependency Direction

Skills may import and call `quant_core`. `quant_core` must not import from `.claude/skills/`.

CLI commands should accept an explicit `--root` when they create `data/` or `outputs/` artifacts. Skills are responsible for passing a root under their own `outputs/` directory.

## Boundary

Framework code may provide atomic capabilities such as table storage, market data normalization/cache refresh, factor calculation, optimization grids, and backtest execution.

Skill scripts compose those atomic capabilities into low-discretion SOP steps. Skill scripts own business defaults such as candidate counts, ETF pool definitions, semantic theme rules, parameter grids, and final report schemas.
