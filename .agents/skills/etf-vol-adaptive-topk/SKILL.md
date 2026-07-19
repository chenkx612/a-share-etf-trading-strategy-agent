---
name: etf-vol-adaptive-topk
description: Maintain the sector-rotation ETF universe and parameters for vol-adaptive-residual-sharpe, then generate auditable next-trading-day ETF and cash holdings. Use for daily recommendations, weekly post-close three-candidate add/prune/grid research, or an early parameter-only search explicitly triggered by abnormal markets.
---

# ETF Vol-Adaptive Top-K

Run from the repository root.

```bash
TRADE_DATE="${TRADE_DATE:-$(date +%F)}"
RUN_DIR=".agents/skills/etf-vol-adaptive-topk/outputs/research"
```

## Cadence

- Every trading day: refresh qfq data and generate next-trading-day holdings.
- After the last trading day of each week closes: review exactly three ETF
  candidates, run add challenges, optimize parameters, and run one prune
  challenge on the latest lookback window. Apply only changes that improve the
  objective by the fixed threshold.
- Abnormal market: allow an explicit early parameter-only research cycle. Do
  not discover candidates, add ETFs, or prune the pool in this mode.

An abnormal trigger is a supervisor/user decision based on observed risk state,
such as a risk-off transition or an unusually large volatility-ratio expansion.
Do not silently turn every risk-off day into a research run.

## Daily Recommendation

```bash
python3 .agents/skills/etf-vol-adaptive-topk/scripts/recommend_next_holdings.py \
  --date "$TRADE_DATE"
```

The command uses the canonical pool at
`.agents/skills/etf-sharpe-topk/references/sector_rotation_universe.csv` and the
last accepted parameters at `references/accepted_params.json`; strategy defaults
apply until the first accepted research cycle.

## Weekly Research

1. Discover candidates with the reusable `$etf-sharpe-topk` stage:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/select_etf_candidates.py \
  --date "$TRADE_DATE" \
  --output-dir "$RUN_DIR"
```

2. Read `candidate_selected.csv` and `candidate_shortlist.csv`. Perform the
   mandatory AI semantic de-duplication review from `$etf-sharpe-topk`, finish
   with exactly three symbols, and set `CANDIDATES`. Exclude money-market or
   cash-management ETFs because this strategy already models cash explicitly.
3. Prepare reviewed candidates and refresh their data:

```bash
python3 .agents/skills/etf-sharpe-topk/scripts/prepare_etf_pool_run.py \
  --date "$TRADE_DATE" \
  --root "$RUN_DIR" \
  --candidates "$CANDIDATES"
```

4. Refresh the qfq CSI 300 ETF benchmark:

```bash
python3 .agents/skills/etf-vol-adaptive-topk/scripts/refresh_csi300_benchmark.py \
  --date "$TRADE_DATE"
```

5. Run the deterministic weekly challenge and persist accepted state:

```bash
python3 .agents/skills/etf-vol-adaptive-topk/scripts/run_research_cycle.py \
  --date "$TRADE_DATE" \
  --root "$RUN_DIR" \
  --apply
```

6. Generate the final next-trading-day holdings:

```bash
python3 .agents/skills/etf-vol-adaptive-topk/scripts/recommend_next_holdings.py \
  --date "$TRADE_DATE"
```

The research command:

- uses the latest 12 months by default; override with `--lookback-months`;
- uses all data in that window for parameter search and stock-pool challenges;
- searches risk parameters in a second bounded grid;
- compares the base pool with each one-candidate addition;
- challenges the selected pool by removing only its lowest-contribution ETF;
- requires parameter, addition, and prune proposals to improve Sortino by the
  configured minimum on the same recent window;
- accepts changes only when validity constraints and the threshold pass;
- plots the selected strategy and `510300` CSI 300 ETF baseline over the same
  lookback window, with annualized return and maximum drawdown on the chart;
- backs up the previous universe/parameters before promotion;
- stores the accepted evidence snapshot with the parameters and verifies that
  the promoted parameters still match the canonical universe before each daily
  recommendation.

## Abnormal-Market Parameter Search

```bash
python3 .agents/skills/etf-vol-adaptive-topk/scripts/refresh_csi300_benchmark.py \
  --date "$TRADE_DATE"

python3 .agents/skills/etf-vol-adaptive-topk/scripts/run_research_cycle.py \
  --date "$TRADE_DATE" \
  --mode abnormal \
  --root "$RUN_DIR" \
  --apply

python3 .agents/skills/etf-vol-adaptive-topk/scripts/recommend_next_holdings.py \
  --date "$TRADE_DATE"
```

This mode may update parameters but must not change the ETF universe.

## Outputs

- Daily `outputs/recommendation_<signal-date>_sector-rotation.csv`: executable
  ETF and cash holdings only.
- Daily `outputs/recommendation_summary.json`: signal, parameters, risk state,
  holdings, and filter audit.
- Research `outputs/research/research_summary.json`: reviewed additions, each
  pool's best grid result, prune attempt, annualized return, maximum drawdown,
  selected pool/parameters, and promotion decision.
- Research `outputs/research/grid_results.csv`: complete deterministic grid
  rows for detailed audit.
- `outputs/equity_curve_<research-date>.png`: selected
  strategy versus the qfq `510300` CSI 300 ETF baseline for the same N-month
  research window, annotated with annualized return and maximum drawdown.
- Research backups `outputs/research/universe_before.csv` and
  `params_before.json`: present only when the corresponding accepted state was
  replaced.
- `references/accepted_params.json`: self-contained last promoted parameter
  contract, universe fingerprint, and acceptance evidence.

Candidate-selection and refresh files, plus duplicate best/evaluation/selected
files, are removed after a successful research summary is written.

ETF and cash weights must sum to 1.0. Recommendation dates are signal dates;
holdings apply to the next trading day.

## Final Report

Reply in Chinese. For daily runs, report the signal date, ETF/cash weights,
risk state, and actual filters. For research runs, report the three reviewed
candidates, lookback window, selected pool/parameters, recent-window metrics,
prune decision, and the explicit promotion status (`proposed`,
`applied`, or `unchanged`).

Do not claim a rejected proposal changed the accepted pool or parameters.
