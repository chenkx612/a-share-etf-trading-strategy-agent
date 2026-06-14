---
name: etf-pool-automation
description: Automatically select three diversified large A-share ETF candidates, compare each candidate with the current sector-rotation pool, optimize the sector-factor-threshold strategy, select/apply the best pool, and output same-day recommendations from the best pool and parameters.
argument-hint: "[--date YYYY-MM-DD] [--apply] [optional candidate1[:name],candidate2[:name],candidate3[:name] override]"
---

# ETF Pool Automation

Run from repo root.

```bash
cd /Users/chenkx/quant-agent
TRADE_DATE="${TRADE_DATE:-$(date +%F)}"
RUN_ID="current"
RUN_DIR="workspaces/sector_rotation/outputs/automations/etf_pool/${RUN_ID}"
```

## SOP

1. Default `TRADE_DATE` to today when `--date` is absent.
2. Use one reusable run directory, `${RUN_DIR}`. The runner clears it before each run because historical intermediate outputs are not part of the durable result.
3. Select candidates automatically unless the user explicitly supplies exactly three candidate ETFs:
   - Pull AKShare ETF realtime spot data from `fund_etf_spot_em`.
   - Filter ETF fund size >= 10,000,000,000 CNY using `总市值` when available, otherwise `流通市值`.
   - Rank by AKShare spot field `涨跌幅`; do not fetch daily bars just to calculate the same-day return.
   - Exclude ETFs already present in the original `sector-rotation` pool. User-supplied candidates must also pass this check.
   - Use the skill script's diversified suggestion as the default candidate list.
   - Review `${RUN_DIR}/candidate_shortlist.csv` only for semantic duplication that the script cannot know, such as several semiconductor ETFs appearing under slightly different names. Keep at most one ETF per clearly duplicated theme, then take the next ranked ETF.
4. Use a three-year backtest window by default: `--start` defaults to three years before `TRADE_DATE`. Override only if the user explicitly asks.
5. Dry-run by default; add `--apply` only when requested.
6. Read outputs:
   - `${RUN_DIR}/candidate_selected.csv`: selected candidates.
   - `${RUN_DIR}/candidate_shortlist.csv`: ranked large-ETF candidates for audit and LLM semantic de-duplication.
   - `${RUN_DIR}/expanded_refresh_universe.csv`: current `sector-rotation` universe plus selected candidates.
   - `${RUN_DIR}/all_results.csv`: attempted grid values by pool.
   - `${RUN_DIR}/evaluations.csv`: best row per pool.
   - `${RUN_DIR}/best.json`: selected pool and best row.
   - `${RUN_DIR}/automation_summary.json`: candidates, evaluations, best row, requested `date`, actual `recommendation_date`, recommendation rows, and key output paths.

## Commands

Fast path, no manual candidate override:

```bash
python3 .claude/skills/etf-pool-automation/scripts/run_etf_pool_automation.py \
  --date "$TRADE_DATE"
```

Append `--apply` only when requested:

```bash
python3 .claude/skills/etf-pool-automation/scripts/run_etf_pool_automation.py \
  --date "$TRADE_DATE" \
  --apply
```

If semantic candidate review is needed, first stop after candidate discovery:

```bash
python3 .claude/skills/etf-pool-automation/scripts/run_etf_pool_automation.py \
  --date "$TRADE_DATE" \
  --candidate-only
```

Read `${RUN_DIR}/candidate_shortlist.csv`, choose exactly three comma-separated symbols if semantic de-duplication changes the default, then run:

```bash
python3 .claude/skills/etf-pool-automation/scripts/run_etf_pool_automation.py \
  --date "$TRADE_DATE" \
  --candidates "$CANDIDATES" \
  <optional --apply>
```

If any candidate is still missing after refresh, stop and report no provider data.

## Script Responsibilities

- `select_etf_candidates.py`: fetch AKShare ETF spot rows, filter large ETFs not already in the original pool, rank by `涨跌幅`, create candidate shortlist/selection, and write `expanded_refresh_universe.csv`.
- `run_etf_pool_automation.py`: clear the reusable run directory, run the full fixed pipeline, strictly backfill recent data for the expanded and selected universes, copy a dry-run recommendation workspace when not applying, extract best parameters from `best.json`, generate recommendations for the latest complete selected-universe trading date on or before the requested date, and write `automation_summary.json`.

Avoid manually reconstructing command lines from `best.json` unless the runner fails and the failure has been diagnosed. Empty recommendation output is a runner failure, not a successful no-pick result.

## Final Report

Reply in Chinese. Include only:

- 候选 ETF：`symbol`, display name, same-day return, theme, and whether the script candidate was manually adjusted for semantic de-duplication.
- 网格参数明细：from `all_results.csv`, unique tried values by pool for `top_n`, `sharpe_window`, `factor_lower_bound`, `corr_window`, `corr_threshold`, `stop_loss_pct`; include `objective` and `constraint`.
- 各池最优参数：from `evaluations.csv`, only `top_n`, `sharpe_window`, `factor_lower_bound`, `corr_window`, `corr_threshold`, `stop_loss_pct`, plus `sortino`, `annual_return`, `max_drawdown`, `valid`.
- 最佳股票池：from `best.json`, `pool_label`, `added_symbol`, display name, best strategy/filter parameters, key metrics, and apply status.
- 当日建议：from `automation_summary.json` `recommendation_date` and recommendation CSV, `symbol`, `name`, `score`, `target_weight`.

Do not include backtest config such as `fee_rate` in "最优参数"; mention it only for audit/debugging. If recommendations are empty, say no ETF passed the filters.
