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
conda run --no-capture-output -n quant python \
  .agents/skills/active-etf-rerank-topk/scripts/recommend_next_holdings.py \
  --date YYYY-MM-DD
```

3. Use `--skip-refresh` only for an offline run whose `--data-root` already
   contains the intended signal-date qfq data. Use `--output-dir` only to isolate
   validation artifacts. Keep official exchange-calendar resolution enabled so
   cache-wide missing sessions cannot pass continuity checks.
4. Do not add or pass strategy parameter overrides, a universe override, or an
   auto-tuning step. The script must read the task-configured production module
   and universe and instantiate `EtfRerankTopKParams()` unchanged.
5. Stop if Champion synchronization validation fails. Synchronize the Harness
   Champion to production before retrying; never execute `champion.py`.

The script refreshes only symbols lacking a valid signal-date close. Each
successful refresh replaces that symbol's cached history with its returned
five-year qfq history. A failed symbol remains auditable and is unavailable on
the signal date without blocking valid peers. The script never falls back to an
older signal date merely to make the full universe complete.

## Report the result

Read `recommendation_summary.json` and report in Chinese:

- the requested date, resolved signal date, and next-trading-day execution
  semantics;
- every ETF target weight and the cash weight;
- every dynamic exclusion and refresh failure;
- the strategy, Champion, task, and universe hashes;
- whether Champion/production hash synchronization passed.

Treat the dated CSV as the execution-facing target portfolio. Confirm ETF plus
cash weights total exactly 1.0.
