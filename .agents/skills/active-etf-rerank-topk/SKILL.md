---
name: active-etf-rerank-topk
description: Generate deterministic, auditable next-trading-day target holdings for the production active-etf-rerank-topk ETF strategy. Use when Codex needs the daily post-close Active ETF Rerank Top-K recommendation, a holdings-and-cash allocation, or an offline verification of that recommendation against the task-configured universe and synchronized Research Harness Champion.
---

# Active ETF Rerank Top-K

Generate only target holdings; do not infer account-level orders or connect to a
broker.

## Run the recommendation

1. Work from the repository root.
2. Run after the requested signal trading day has closed:

```bash
conda run --no-capture-output -n quant \
  quant-agent recommend active_etf_rerank_topk --date YYYY-MM-DD
```

3. Use `--skip-refresh` only for an offline run whose repository data cache
   already contains the intended signal-date qfq data.
4. Do not add or pass strategy parameter overrides or a universe override. On
   the first successful run of every calendar month, the script deterministically
   selects and freezes one parameter set from the production module's fixed grid,
   using only the preceding 18 months through that signal date and the production
   hard constraints. Later runs in that month must reuse the frozen artifact.
5. Stop if Champion synchronization validation fails. Synchronize the Harness
   Champion to production before retrying; never execute `champion.py`.

The framework refreshes the task universe and benchmark, owns parameter
scheduling, and fails closed when a due search or causal replay cannot complete.
Do not call or copy a strategy-specific recommendation script.

## Report the result

Read `outputs/active-etf-rerank-topk/summary.json` and
the referenced recommendation CSV, then report in Chinese:

- the requested date, resolved signal date, and next-trading-day execution
  semantics;
- every ETF target weight and the cash weight;
- the strategy, Champion, task, universe, policy, grid, and backtest-contract hashes;
- the effective monthly parameter set and whether it was searched or reused;
- the strict-causal one-year cumulative-return curve and `510300` comparison,
  including annualized return and maximum drawdown;
- the current-universe survivorship-bias disclosure.

Treat the referenced `recommendation.csv` as the execution-facing target
portfolio. Confirm ETF plus cash weights total exactly 1.0.
