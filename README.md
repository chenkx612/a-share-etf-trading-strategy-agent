# Quant Agent

Quant Agent 是一个以 **loop/harness engineering 驱动自动化量化交易策略研究**
的工程平台。它将策略研发从一次性的人工调参或 Agent 对话，转化为可持续运行、
可客观评测、可恢复、可审计的自动化研究闭环：

```text
定义研究目标、数据区间、约束和预算
→ Agent 提出假设并实现候选策略
→ Harness 运行测试与确定性回测
→ 在隔离的 Gate 数据上评测候选
→ 接受或拒绝候选并更新 Champion
→ 保存代码差异、指标、决策和研究记忆
→ 达到目标或预算耗尽前持续迭代
→ 生成终局研究复盘
```

其中，Agent 或代码模型负责单轮的策略假设探索和候选实现；确定性的 Python
Harness 负责外层循环、数据边界、候选隔离、测试、回测、晋级规则、预算控制、
失败恢复和停止条件。候选策略不能修改评测器，也不能决定自己是否晋级。

项目既支持从 0 到 1 研发新策略，也支持从 1 到 N 持续优化已有策略。自动化的
目标不是无约束地寻找更漂亮的回测曲线，而是在固定契约和样本外 Gate 下，
持续产生可复现、可比较、有完整证据链的研究结果。

## Harness architecture

```text
Research task (task.toml)
        │
        ▼
Loop controller ── budgets / stop conditions / resume
        │
        ▼
Candidate runner ── isolated worktree / development data / Agent session
        │
        ▼
Fixed evaluator ── tests / backtest / hard constraints / Gate metrics
        │
        ▼
Champion manager ── accept or reject / version refs / experiment evidence
        │
        └────────────── next research round
```

Harness 遵循以下核心原则：

- **确定性评测**：策略选择、回测指标、硬约束和晋级规则由固定 Python 代码执行。
- **数据隔离**：研发阶段只使用 Development 数据；精确 Gate 指标不会反馈给后续研发轮次。
- **候选隔离**：每轮候选在独立 worktree 中开发，失败或拒绝不会污染下一轮和当前工作分支。
- **可恢复运行**：循环状态、累计时间、连续失败、当前实验和停止原因都会持久化。
- **证据可审计**：每轮保存输入、代码差异、日志、指标、决策及其父 Champion。
- **受控自动化**：循环在达到目标、轮数、时长或连续失败预算时停止，并生成终局复盘。

## Quick start

安装项目：

```bash
python3 -m pip install -e ".[dev]"
pytest
```

运行自动化策略研究循环：

```bash
python3 -m quant_core.cli research loop \
  --task path/to/task.toml \
  --research-root .research
```

研究任务在 `task.toml` 中声明目标、数据、Development/Gate 区间、允许修改的范围、
固定评测命令、硬约束、Champion 改善要求、模型配置和运行预算。Harness 会在
`.research/<task-id>/` 下保存状态、实验记录、候选补丁、决策和最终报告。

调试时可以只运行一轮：

```bash
# 运行一次候选研发，不管理 Champion
python3 -m quant_core.cli research run-once \
  --task path/to/task.toml \
  --experiment-id experiment-001 \
  --output path/to/experiment

# 运行一次完整的隔离、评测和 Champion 决策
python3 -m quant_core.cli research run-managed \
  --task path/to/task.toml \
  --experiment-id experiment-001 \
  --research-root .research
```

循环结束后会自动生成复盘，也可以单独补生成或重试：

```bash
python3 -m quant_core.cli research report \
  --task path/to/task.toml \
  --research-root .research
```

## Quant framework

`src/quant_core/` 同时提供 Harness 所依赖的数据、因子、策略、回测、优化、推荐和
报告等确定性量化框架能力。它们可以独立使用：

```bash
python3 -m quant_core.cli data update --universe path/to/universe.csv --universe-name default --start 2024-01-01 --end 2024-12-31
python3 -m quant_core.cli factor compute --start 2024-01-01 --end 2024-12-31
python3 -m quant_core.cli backtest run --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31
python3 -m quant_core.cli optimize grid --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31 --top-n 3,5,10
python3 -m quant_core.cli recommend today --universe path/to/universe.csv --date 2024-12-31 --top-n 10
```

缺少 `pyarrow` 时，本地表会自动降级为 CSV；安装项目依赖后默认使用 Parquet。

## Project layout

```text
src/quant_core/research/  # Loop/harness contracts, runner, evaluator, state, report
src/quant_core/data/      # Market data download, cache, and table IO
src/quant_core/factors/   # Deterministic factor calculations
src/quant_core/strategy/  # Candidate and built-in strategy implementations
src/quant_core/backtest/  # Fixed simulation engine and metrics
tests/                    # Framework and harness tests
docs/                     # Framework engineering documentation
.agents/skills/           # Task knowledge, prompts, scripts, assets, and outputs
.research/<task-id>/      # Managed loop state and auditable experiment evidence
```

运行命令默认把本地日线缓存写到当前工作目录下的 `data/etf_daily.*`，把因子、回测和推荐等中间结果写到 `outputs/`。股票池不再保存在 `data/` 下；调用框架 CLI 时通过 `--universe path/to/universe.csv` 显式传入。

技能应显式把 `--root` 指向自己的 `outputs/` 子目录，避免在项目根目录产生中间结果：

```bash
python3 -m quant_core.cli --root .agents/skills/etf-sharpe-topk/outputs/sector_rotation factor compute --start 2026-05-01 --end 2026-05-31
```

## Docs

- [框架开发文档](docs/README.md)
- [框架架构](docs/architecture.md)
- [Loop Harness 设计与实施状态](docs/loop-harness.md)
- [Loop Harness 任务与结果契约](docs/loop-harness-contracts.md)
- [Skill 契约](docs/skill_contract.md)
- [Research Harness 问题与技术方案](docs/research-harness-lessons.md)

## Scope

当前 Harness 面向策略研究和回测验证，不自动合并 Champion 到用户当前分支，
也不负责自动部署、实盘下单或资金管理。这些能力需要独立的安全设计和明确的
人工审批边界。
