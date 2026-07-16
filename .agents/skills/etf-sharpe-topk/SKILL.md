---
name: etf-sharpe-topk
description: Select large A-share ETF candidates with keyword review flags, require AI semantic de-duplication review before choosing exactly three candidates, compare each candidate with the current sector-rotation pool, optimize the sharpe-corr-threshold strategy, prune the worst contribution ETF when that improves Sortino, select/apply the final best pool, and output next-trading-day Top-K ETF recommendations from the final best pool and parameters.
---

# ETF Sharpe Top-K

Run from the repository root.

```bash
TRADE_DATE="${TRADE_DATE:-$(date +%F)}"
RUN_DIR=".agents/skills/etf-sharpe-topk/outputs"
```

## Directory Roles

- `references/`: input prompts, knowledge, and static reference data used by the skill. The fallback base `sector_rotation_universe.csv` belongs here.
- `scripts/`: independently runnable scripts with their own CLI entry points for AI-callable stages. Shared helper code may live in `scripts/utils.py` or a clearly named `utils/` module; do not call helper modules directly as workflow stages.
- `outputs/`: generated run artifacts only. Treat it as disposable unless `--apply` backs up the previous base pool there.

## SOP

1. Default `TRADE_DATE` to today when `--date` is absent.
2. Use one reusable output directory, `${RUN_DIR}`. The runner clears it before each run because historical intermediate outputs are not part of the durable result. Only pass a non-default `--run-id` when explicitly comparing separate runs; that creates `${RUN_DIR}/<run-id>`.
3. Discover candidates automatically unless the user explicitly supplies exactly three candidate ETFs:
   - Pull ETF realtime spot data from AKShare `fund_etf_spot_em`; if that endpoint fails, fall back to AKShare ETF symbols plus Tencent quote data.
   - Filter ETF fund size >= 10,000,000,000 CNY using `总市值` when available, otherwise `流通市值`.
   - Rank by realtime spot field `涨跌幅`; do not fetch daily bars just to calculate the same-day return.
   - Exclude ETFs already present in the original `sector-rotation` pool. User-supplied candidates must also pass this check.
   - Treat exact normalized ETF exposure Chinese-name matches with the original `sector-rotation` pool as script-level duplicates. Normalize by removing whitespace and comparing the text before `ETF`; for example `通信ETF华夏` duplicates base `通信ETF`, and `纳指ETF广发` duplicates base `纳指ETF`. Base-pool Chinese names must be stored directly in `references/sector_rotation_universe.csv`; do not import names from another local project.
   - After removing base-pool symbol/name duplicates, keep only the highest same-day-return ETF for each normalized ETF exposure Chinese name.
   - Keep keyword/theme matches as AI review flags only. The script marks shortlist rows with `theme`, `base_theme_overlap`, and `base_theme_matches`, but these fields must not hard-reject candidates.
4. Always run candidate discovery with `select_etf_candidates.py` first and stop for AI semantic de-duplication review before the full automation run, unless the user supplied exactly three reviewed candidates.
   - Read `${RUN_DIR}/candidate_selected.csv` and `${RUN_DIR}/candidate_shortlist.csv`.
   - Start from the script-selected candidates, then use ETF names/themes and `base_theme_matches` to judge whether any candidate duplicates either another candidate or an ETF already in the base pool, such as several semiconductor, AI/computer, broad-index, or new-energy variants.
   - Keep at most one ETF per clearly duplicated exposure.
   - Use `base_theme_overlap=true` as a warning flag, not a hard rejection. Keep or remove those candidates based on AI semantic review.
   - When a duplicate is removed, scan `${RUN_DIR}/candidate_shortlist.csv` in descending `return_pct` order and take the next non-base-pool ETF that is not semantically duplicate with the base pool or the kept candidates.
   - End with exactly three reviewed candidate symbols and pass them through `prepare_etf_pool_run.py --candidates`, even if the reviewed list is unchanged from the script-selected list.
5. Check each ETF for the latest trading date before calling the daily-data API. Skip ETFs already covered through that date. For each stale ETF, fetch qfq daily data starting five years before `TRADE_DATE` and replace that ETF's entire cached history only after a complete fetch succeeds; do not merge refreshed qfq rows with its old cache. Use a three-year backtest and parameter-search window by default: `--start` defaults to three years before `TRADE_DATE`. Override the backtest window only if the user explicitly asks; the market-data refresh remains capped at five years.
6. After the best pool is selected, always run the pruning challenge:
   - Re-run the best pool's selected strategy over the same three-year window and calculate each ETF's gross return contribution from held weight times next-open return.
   - Identify the ETF with the lowest return contribution.
   - Remove only that ETF, re-run the same parameter optimization grid on the pruned pool, and compare the pruned pool's optimized `sortino` with the original best pool's `sortino`.
   - If and only if the pruned pool's `sortino` is strictly higher, accept the pruned pool as the final best pool and exclude that ETF from `selected_universe.csv`, recommendations, and `--apply` updates.
   - If the pruned pool does not improve `sortino`, keep the original best pool unchanged.
7. Dry-run by default; add `--apply` only when requested.
   - `--apply` accepts the final best pool as the future base pool by updating `references/sector_rotation_universe.csv`; the previous base pool is backed up in `${RUN_DIR}/universe_before.*`.
8. Read durable outputs:
   - `${RUN_DIR}/automation_summary.json`: candidates, compact grid values by pool, evaluations, best row, requested `date`, actual `recommendation_date`, recommendation rows, and key output paths.
   - `${RUN_DIR}/recommendation_<recommendation-date>_sector-rotation.csv`: generated recommendation rows.
   - `${RUN_DIR}/universe_before.csv` or `.parquet`: only present for `--apply`, as the previous universe backup.

## Commands

Stage 1, candidate discovery:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/select_etf_candidates.py \
  --date "$TRADE_DATE" \
  --output-dir "$RUN_DIR"
```

Stage 2, mandatory AI semantic de-duplication review:

Read `${RUN_DIR}/candidate_selected.csv` and `${RUN_DIR}/candidate_shortlist.csv`, choose exactly three reviewed comma-separated symbols as described in the SOP.

Stage 3, prepare reviewed pool and data:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/prepare_etf_pool_run.py \
  --date "$TRADE_DATE" \
  --candidates "$CANDIDATES"
```

Stage 4, optimize pools and pruning challenge:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/optimize_etf_pool.py \
  --date "$TRADE_DATE"
```

Append `--apply` to the optimize command only when requested:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/optimize_etf_pool.py \
  --date "$TRADE_DATE" \
  --apply
```

Stage 5, generate recommendation and summary:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/recommend_etf_pool.py \
  --date "$TRADE_DATE"
```

If any candidate is still missing after refresh, stop and report no market data.

Data refresh only checks the existing local daily table under `DATA_ROOT`; there is no separate skill cache directory. `DATA_ROOT` defaults to the repository root, so daily bars live in the framework-level `data/etf_daily.*` file rather than under `.agents/skills/...`. The sector-rotation pool itself belongs to `references/sector_rotation_universe.csv`, not `data/`. Daily bars must use qfq adjusted prices. The prepare stage skips current ETFs and refreshes only ETFs missing the latest trading date. A successful refresh replaces all cached rows for each refreshed ETF with the new five-year qfq history, because qfq data can be restated after splits or distributions. The recommendation stage must not fetch market data; it only verifies the local table can provide a complete selected-universe recommendation date on or before `TRADE_DATE`. If a requested symbol is still missing from the local market data result, stop and report no market data for that symbol.

## Script Responsibilities

- `select_etf_candidates.py`: independently runnable candidate discovery command; fetch ETF spot rows with AKShare first and Tencent quote fallback, filter large ETFs not already in the original pool, rank by `涨跌幅`, skip script-level normalized ETF exposure Chinese-name duplicates, add keyword/theme review flags, create candidate shortlist/selection, and write temporary files needed by later stages.
- `prepare_etf_pool_run.py`: independently runnable prepare stage; read the Stage 1 shortlist, accept the AI-reviewed candidate list through `--candidates`, rebuild selected candidate artifacts, clear the reusable output directory for the full run, skip ETFs already current, and replace stale ETFs' cached histories with freshly downloaded five-year qfq data. This data window is independent of the default three-year optimization window.
- `optimize_etf_pool.py`: independently runnable optimization stage; read prepared candidates, optimize candidate pools, challenge the chosen best pool by removing the worst contribution ETF, accept the pruned pool only when optimized Sortino improves, write `selected_universe.csv`, `best.json`, `evaluations.csv`, `all_results.csv`, and update `references/sector_rotation_universe.csv` only when `--apply` is passed.
- `recommend_etf_pool.py`: independently runnable recommendation stage; verify local selected-universe data, generate recommendations for the latest complete selected-universe trading date on or before the requested date, write `automation_summary.json`, and remove runner-only intermediate files after a full run.
- `utils.py`: helper module for shared stage implementation. Do not call it directly as a workflow stage.

Avoid manually reconstructing command lines from `best.json` unless the runner fails and the failure has been diagnosed. Empty recommendation output is a runner failure, not a successful no-pick result.

## Final Report

Reply in Chinese. Include only:

- 候选 ETF：`symbol`, display name, same-day return, theme, and whether the script candidate was manually adjusted for semantic de-duplication.
- 网格参数明细：from `automation_summary.json` `grid_values_by_pool`, unique tried values by pool for `top_n`, `sharpe_window`, `factor_lower_bound`, `corr_window`, `corr_threshold`, `stop_loss_pct`; include `objective` and `constraint`.
- 各池最优参数：from `automation_summary.json` `evaluations`, only `top_n`, `sharpe_window`, `factor_lower_bound`, `corr_window`, `corr_threshold`, `stop_loss_pct`, plus `sortino`, `annual_return`, `max_drawdown`, `valid`.
- 最佳股票池：from `automation_summary.json` `best`, `pool_label`, `added_symbol`, display name, best strategy/filter parameters, key metrics, and apply status.
- 剔除挑战：from `automation_summary.json` `pruning_challenge`, include the lowest-contribution ETF, contribution, original best pool Sortino, pruned pool Sortino, and whether the ETF was deleted from the final pool.
- 当日建议：from `automation_summary.json` `recommendation_date` and recommendation CSV rows with `record_type=recommendation`, `symbol`, `name`, `score`, `target_weight`.
- 过滤说明：only when `automation_summary.json` `recommendation_filters` is non-empty. Say exactly which ETF was filtered and why. For `stop_loss`, include `symbol`, `name`, `daily_return`, and `stop_loss_pct`. For `correlation`, include filtered `symbol`, `name`, compared selected ETF, `correlation`, and `corr_threshold`.

Do not include backtest config such as `fee_rate` in "最优参数"; mention it only for audit/debugging. If recommendations are empty, say no ETF passed the filters.
