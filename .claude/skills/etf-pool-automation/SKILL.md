---
name: etf-pool-automation
description: Run the daily ETF pool automation for quant-agent. Use when the user provides three candidate ETF symbols and wants Claude Code to compare the current sector-rotation pool against each candidate-added pool, optimize the factor-threshold strategy by Sortino with the drawdown-lt-return constraint, and select or apply the best new ETF pool.
argument-hint: "<date> <candidate1,candidate2,candidate3> [--apply]"
---

# ETF Pool Automation

Run this skill from the `quant-agent` repository:

```bash
cd /Users/chenkx/quant-agent
```

## Daily Workflow

1. Parse the user-provided trade date and exactly three ETF symbols.
2. Confirm the three candidate symbols are present in the workspace daily data before optimizing.
3. Run the automation command in dry-run mode first unless the user explicitly asks to apply the new pool.
4. Review `evaluations.csv` and `best.json`.
5. If the user requested application, rerun with `--apply` or run the same command with `--apply` included.

## Command Template

Use this command for the sector rotation workspace:

```bash
python3 -m quant_agent.cli \
  --root workspaces/sector_rotation \
  automation etf-pool \
  --date <YYYY-MM-DD> \
  --candidates <symbol1,symbol2,symbol3> \
  --start 2023-04-03 \
  --end <YYYY-MM-DD> \
  --top-n 4,5,6 \
  --fee-rate 0.0003 \
  --sharpe-window 15,20,25,30,35 \
  --factor-lower-bound=-1.0,-0.5,0.0,0.5,1.0 \
  --corr-window 100 \
  --corr-threshold 0.9 \
  --stop-loss-pct 0.1 \
  --objective sortino \
  --constraint drawdown-lt-return \
  --run-id <YYYY-MM-DD>_etf_pool
```

Add `--apply` only when the user wants to update the workspace universe. With `--apply`, the command writes the selected pool to:

```text
workspaces/sector_rotation/data/universe/sector_rotation_universe.*
```

It also writes the previous universe backup and selected universe snapshot under:

```text
workspaces/sector_rotation/outputs/automations/etf_pool/<run-id>/
```

## Output Checks

After the command finishes, report:

- selected `pool_label` and `added_symbol`
- `sortino`, `annual_return`, `max_drawdown`, and `valid`
- output directory path
- whether the universe was applied or only evaluated

If the command says a candidate is missing from local daily data, stop and tell the user to update/fetch data for that ETF before running the automation.
