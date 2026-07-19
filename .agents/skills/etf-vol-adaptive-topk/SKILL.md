---
name: etf-vol-adaptive-topk
description: Generate an auditable next-trading-day ETF and cash target portfolio with src/quant_core/strategy/vol_adaptive_residual_sharpe.py. Use when Codex needs current holdings advice for the sector-rotation ETF universe, including risk-off state and stop-loss or correlation filters; accept explicit strategy parameters and otherwise use the strategy defaults.
---

# ETF Vol-Adaptive Top-K

Run from the repository root.

## Workflow

1. Default the requested date to today and the universe to
   `.agents/skills/etf-sharpe-topk/references/sector_rotation_universe.csv`.
   This is the single durable sector-rotation pool; do not copy or mutate it.
2. Run the recommendation script. It refreshes stale qfq daily data for the
   selected universe, uses the latest complete signal date on or before the
   request, and writes ETF plus cash target weights.
3. Use strategy defaults unless the user supplies parameters or provides a
   previously accepted parameter set. Do not optimize parameters inside this
   skill. Parameter promotion belongs to the deterministic Research Harness.
4. Treat the output as the target portfolio for the next trading day after the
   signal date. Never describe the signal date as the execution date.
5. Use `--skip-refresh` only for an explicit offline/local-data run.

## Command

```bash
python3 .agents/skills/etf-vol-adaptive-topk/scripts/recommend_next_holdings.py \
  --date "${TRADE_DATE:-$(date +%F)}"
```

Pass `--universe`, `--data-root`, `--output-dir`, or individual strategy
parameters only when needed.

## Outputs

- `outputs/recommendation_<signal-date>_sector-rotation.csv`: executable ETF
  and cash target holdings only.
- `outputs/recommendation_summary.json`: requested date, signal date,
  parameters, risk regime, complete holdings, stop-loss/correlation filter
  audit, and output path.

ETF and cash target weights must sum to 1.0. If no ETF passes, output 100% cash.

## Final Report

Reply in Chinese. Include:

- 信号日期及“适用于下一交易日”的说明。
- ETF 和现金的 symbol、名称、score、target weight。
- risk-off、波动率比值与阈值、风险档目标仓位、实际总仓位和现金。
- 仅在存在时列出 stop-loss 和 correlation 过滤事件。

Do not report backtest metrics or claim the parameters are optimal unless they
came from a separately completed and accepted Research Harness result.
