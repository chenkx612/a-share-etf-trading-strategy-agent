# Workspaces

工作区目录用于存放真实研究数据、策略运行产物和可复现实验，不放源码。

- `sharpe_single/`：Sharpe 单因子策略工作区，包含当前使用的数据快照和运行产物。可以配合 `--root workspaces/sharpe_single` 运行 CLI。

常用命令：

```bash
python3 -m quant_agent.cli --root workspaces/sharpe_single factor compute --start 2026-05-01 --end 2026-05-31
python3 -m quant_agent.cli --root workspaces/sharpe_single backtest run --strategy sharpe-single --start 2026-05-01 --end 2026-05-31 --run-id sharpe_single_baseline
```
