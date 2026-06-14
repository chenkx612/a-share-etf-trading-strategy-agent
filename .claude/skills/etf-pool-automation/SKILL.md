---
name: etf-pool-automation
description: Select large A-share ETF candidates with keyword pre-filtering, require AI semantic de-duplication review before choosing exactly three candidates, compare each candidate with the current sector-rotation pool, optimize the ranked-threshold-corr strategy, select/apply the best pool, and output same-day recommendations from the best pool and parameters.
argument-hint: "[--date YYYY-MM-DD] [--apply] [optional candidate1[:name],candidate2[:name],candidate3[:name] override]"
---

# ETF Pool Automation

Run from repo root.

```bash
cd /Users/chenkx/quant-agent
TRADE_DATE="${TRADE_DATE:-$(date +%F)}"
RUN_ID="current"
RUN_DIR=".claude/skills/etf-pool-automation/outputs"
DATA_ROOT="."
```

## Directory Roles

- `references/`: input prompts, knowledge, and static reference data used by the skill. The fallback base `sector_rotation_universe.csv` belongs here.
- `scripts/`: independently runnable scripts with their own CLI entry points. Do not add helper-only modules here unless they are also valid standalone commands.
- `assets/`: output templates or reusable files copied into generated outputs. Do not store input knowledge or base universes here.

## SOP

1. Default `TRADE_DATE` to today when `--date` is absent.
2. Use one reusable output directory, `${RUN_DIR}`. The runner clears it before each run because historical intermediate outputs are not part of the durable result. Only pass a non-default `--run-id` when explicitly comparing separate runs; that creates `${RUN_DIR}/<run-id>`.
3. Discover candidates automatically unless the user explicitly supplies exactly three candidate ETFs:
   - Pull AKShare ETF realtime spot data from `fund_etf_spot_em`.
   - Filter ETF fund size >= 10,000,000,000 CNY using `总市值` when available, otherwise `流通市值`.
   - Rank by AKShare spot field `涨跌幅`; do not fetch daily bars just to calculate the same-day return.
   - Exclude ETFs already present in the original `sector-rotation` pool. User-supplied candidates must also pass this check.
   - The script's `theme` field is only a keyword pre-filter, not final de-duplication.
4. Always run candidate discovery with `select_etf_candidates.py` first and stop for AI semantic de-duplication review before the full automation run, unless the user supplied exactly three reviewed candidates.
   - Read `${RUN_DIR}/candidate_selected.csv` and `${RUN_DIR}/candidate_shortlist.csv`.
   - Start from the script-selected candidates, then use ETF names/themes to judge whether any are semantically duplicate exposures, such as several semiconductor, AI/computer, broad-index, or new-energy variants.
   - Keep at most one ETF per clearly duplicated exposure.
   - When a duplicate is removed, scan `${RUN_DIR}/candidate_shortlist.csv` in descending `return_pct` order and take the next non-base-pool ETF that is not semantically duplicate with the kept candidates.
   - End with exactly three reviewed candidate symbols and pass them through `run_etf_pool_automation.py --candidates`, even if the reviewed list is unchanged from the script-selected list.
5. Use a three-year backtest window by default: `--start` defaults to three years before `TRADE_DATE`. Override only if the user explicitly asks.
6. Dry-run by default; add `--apply` only when requested.
   - `--apply` accepts the best pool as the future base pool by updating `references/sector_rotation_universe.csv`; the previous base pool is backed up in `${RUN_DIR}/universe_before.*`.
7. Read durable outputs:
   - `${RUN_DIR}/automation_summary.json`: candidates, compact grid values by pool, evaluations, best row, requested `date`, actual `recommendation_date`, recommendation rows, and key output paths.
   - `${RUN_DIR}/recommendation_<recommendation-date>_sector-rotation.csv`: generated recommendation rows.
   - `${RUN_DIR}/universe_before.csv` or `.parquet`: only present for `--apply`, as the previous universe backup.

## Commands

Stage 1, candidate discovery:

```bash
python3 .claude/skills/etf-pool-automation/scripts/select_etf_candidates.py \
  --date "$TRADE_DATE" \
  --output-dir "$RUN_DIR"
```

Stage 2, mandatory AI semantic de-duplication review:

Read `${RUN_DIR}/candidate_selected.csv` and `${RUN_DIR}/candidate_shortlist.csv`, choose exactly three reviewed comma-separated symbols as described in the SOP.

Stage 3, full automation:

```bash
python3 .claude/skills/etf-pool-automation/scripts/run_etf_pool_automation.py \
  --date "$TRADE_DATE" \
  --candidates "$CANDIDATES"
```

Append `--apply` to the full automation command only when requested:

```bash
python3 .claude/skills/etf-pool-automation/scripts/run_etf_pool_automation.py \
  --date "$TRADE_DATE" \
  --candidates "$CANDIDATES" \
  --apply
```

If any candidate is still missing after refresh, stop and report no provider data.

Data refresh only checks the existing local daily table under `DATA_ROOT`; there is no separate skill cache directory. `DATA_ROOT` defaults to the repository root, so daily bars live in the framework-level `data/etf_daily.*` file rather than under `.claude/skills/...`. The sector-rotation pool itself belongs to `references/sector_rotation_universe.csv`, not `data/`. Before requesting provider daily bars, the runner resolves the latest trading date on or before `TRADE_DATE`; symbols already covered for that trading date are skipped. Symbols missing that trading date are refreshed over the full adjusted daily window capped to the most recent five calendar years, because qfq data can be restated. If a requested stale symbol is still missing from the provider result, stop and report no provider data for that symbol.

## Script Responsibilities

- `select_etf_candidates.py`: independently runnable candidate discovery command; fetch AKShare ETF spot rows, filter large ETFs not already in the original pool, rank by `涨跌幅`, apply keyword-based theme pre-filtering, create candidate shortlist/selection, and write temporary files needed by the runner.
- `run_etf_pool_automation.py`: full automation command only; read the Stage 1 shortlist, accept the AI-reviewed candidate list through `--candidates`, rebuild selected candidate artifacts, clear the reusable output directory for the full run, strictly backfill recent data for the expanded and selected universes, use a temporary recommendation workspace, generate recommendations for the latest complete selected-universe trading date on or before the requested date, write `automation_summary.json`, update `references/sector_rotation_universe.csv` only when `--apply` is passed, and remove runner-only intermediate files after a full run.

Avoid manually reconstructing command lines from `best.json` unless the runner fails and the failure has been diagnosed. Empty recommendation output is a runner failure, not a successful no-pick result.

## Final Report

Reply in Chinese. Include only:

- 候选 ETF：`symbol`, display name, same-day return, theme, and whether the script candidate was manually adjusted for semantic de-duplication.
- 网格参数明细：from `automation_summary.json` `grid_values_by_pool`, unique tried values by pool for `top_n`, `sharpe_window`, `factor_lower_bound`, `corr_window`, `corr_threshold`, `stop_loss_pct`; include `objective` and `constraint`.
- 各池最优参数：from `automation_summary.json` `evaluations`, only `top_n`, `sharpe_window`, `factor_lower_bound`, `corr_window`, `corr_threshold`, `stop_loss_pct`, plus `sortino`, `annual_return`, `max_drawdown`, `valid`.
- 最佳股票池：from `automation_summary.json` `best`, `pool_label`, `added_symbol`, display name, best strategy/filter parameters, key metrics, and apply status.
- 当日建议：from `automation_summary.json` `recommendation_date` and recommendation CSV, `symbol`, `name`, `score`, `target_weight`.

Do not include backtest config such as `fee_rate` in "最优参数"; mention it only for audit/debugging. If recommendations are empty, say no ETF passed the filters.
