# Quant Agent

阶段一目标是先做一个人工可用的 A 股 ETF 多因子闭环：更新数据、计算因子、选股、回测、生成推荐和报告。

## Quick start

```bash
python3 -m quant_agent.cli data update --start 2024-01-01 --end 2024-12-31
python3 -m quant_agent.cli factor compute --start 2024-01-01 --end 2024-12-31
python3 -m quant_agent.cli backtest run --start 2024-03-01 --end 2024-12-31 --top-n 10
python3 -m quant_agent.cli backtest run --strategy sharpe-single --start 2024-03-01 --end 2024-12-31
python3 -m quant_agent.cli optimize grid --strategy sharpe-single --start 2024-03-01 --end 2024-12-31 --top-n 3,5,10
python3 -m quant_agent.cli recommend today --date 2024-12-31 --top-n 10
```

缺少 `pyarrow` 时，本地表会自动降级为 CSV；安装项目依赖后默认使用 Parquet。

## Project layout

```text
quant_agent/        # Python package and CLI implementation
tests/              # Automated tests
docs/               # Design notes and user manual
workspaces/         # Research data and strategy run artifacts
```

运行命令默认把研究数据写到当前工作目录下的 `data/` 和 `outputs/`。如果希望和源码分开，推荐显式指定工作区：

```bash
python3 -m quant_agent.cli --root workspaces/sharpe_single factor compute --start 2026-05-01 --end 2026-05-31
```

## Docs

- [阶段一实现说明](docs/stage1.md)
- [使用手册：从策略设计到上线](docs/user_manual.md)
