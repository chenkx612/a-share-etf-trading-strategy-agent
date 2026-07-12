# Quant Agent

Quant Agent 将量化框架基建和 Agent skill 工作流分开维护。`src/quant_core/` 提供数据、因子、策略、回测、报告和 CLI 等通用能力；具体任务、业务知识、脚本和运行产物放在 `.agents/skills/` 下的独立技能目录中。

## Quick start

```bash
python3 -m quant_core.cli data update --universe path/to/universe.csv --universe-name default --start 2024-01-01 --end 2024-12-31
python3 -m quant_core.cli factor compute --start 2024-01-01 --end 2024-12-31
python3 -m quant_core.cli backtest run --universe path/to/universe.csv --start 2024-03-01 --end 2024-12-31 --top-n 10
python3 -m quant_core.cli backtest run --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31
python3 -m quant_core.cli optimize grid --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31 --top-n 3,5,10
python3 -m quant_core.cli recommend today --universe path/to/universe.csv --date 2024-12-31 --top-n 10
```

缺少 `pyarrow` 时，本地表会自动降级为 CSV；安装项目依赖后默认使用 Parquet。

## Project layout

```text
src/quant_core/      # Framework package and CLI implementation
tests/               # Framework tests
docs/                # Framework development docs only
.agents/skills/      # Skill workflows, references, assets, scripts, and outputs
```

运行命令默认把本地日线缓存写到当前工作目录下的 `data/etf_daily.*`，把因子、回测和推荐等中间结果写到 `outputs/`。股票池不再保存在 `data/` 下；调用框架 CLI 时通过 `--universe path/to/universe.csv` 显式传入。

技能应显式把 `--root` 指向自己的 `outputs/` 子目录，避免在项目根目录产生中间结果：

```bash
python3 -m quant_core.cli --root .agents/skills/etf-sharpe-topk/outputs/sector_rotation factor compute --start 2026-05-01 --end 2026-05-31
```

## Docs

- [框架开发文档](docs/README.md)
- [框架架构](docs/architecture.md)
- [Skill 契约](docs/skill_contract.md)
