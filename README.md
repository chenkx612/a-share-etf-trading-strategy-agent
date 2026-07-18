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
Champion manager ── accept or reject / atomic strategy file / experiment evidence
        │
        └────────────── next research round
```

Harness 遵循以下核心原则：

- **确定性评测**：策略选择、回测指标、硬约束和晋级规则由固定 Python 代码执行。
- **数据隔离**：研发阶段只使用 Development 数据；精确 Gate 指标不会反馈给后续研发轮次。
- **候选隔离**：每轮候选在独立 worktree 中开发，失败或拒绝不会污染下一轮和当前工作分支。
- **可恢复运行**：Run 状态、累计时间、连续失败、当前 Round 和停止原因都会持久化。
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
  --task path/to/task.toml
```

启动新的 Loop Run 前，先检查
[Research Harness Issues](HARNESS_ISSUES.md)；存在开放 P0 时不应启动新 Run。

### Task and candidate contract

研究任务在 `task.toml` 中声明目标、数据、Development/Gate 区间、允许修改的范围、
固定评测命令、硬约束、Champion 改善要求、模型配置和运行预算。每次调用自动分配新的
编号 Run；重复研发时继续使用默认 `.research`，无需显式传入 `--research-root`。当前
`task.toml` 同时是可直接运行的任务配置示例，字段由
`src/quant_core/research/contracts.py` 验证。

- `scope.editable` 当前只能声明一个仓库相对策略文件。
- `baseline.mode = "workspace"` 从工作区策略初始化 Champion；`"none"` 用于 0→1 研发，
  可配合 `baseline.exclude` 从候选基座排除旧策略实现。
- 回测命令支持 `{python}`、`{universe}`、`{start}`、`{end}`、`{run_id}`、
  `{strategy_name}` 和 `{strategy_module}` 占位符；指标路径必须包含 `{run_id}`。
- 当前只支持不重叠的固定 Development/Gate 区间。硬约束运算符为 `>=`、`<=` 和
  `abs<=`；可选 Test 区间必须位于 Gate 之后，Loop 不会读取或评测它。
- `evaluation.acceptance.minimum_improvement` 控制合格 Champion 之上的最小改善；
  `evaluation.target.objective_at_least` 可在目标达成后提前停止。
- 固定 evaluator 要求策略实现 `select(daily, universe, start, end)`，返回包含 `date`、
  `symbol`、`target_weight` 的 pandas DataFrame。权重必须有限且非负，每日总和不超过 1。

运行前还应确认 OpenCode 模型与认证可用，并且任务声明的测试和回测命令可以在仓库根目录运行。
Provider 模型名、推理档位、认证和价格属于外部状态，不在仓库中维护静态清单。

### Run operations

常用研究命令：

```bash
# 运行一次候选研发，不管理 Champion
python3 -m quant_core.cli research run-once \
  --task task.toml \
  --experiment-id experiment-001 \
  --output path/to/experiment

# 重新生成最近或指定 Run 的终局报告
python3 -m quant_core.cli research report --task task.toml
python3 -m quant_core.cli research report --task task.toml --run 2

# 清理临时 worktree、派生缓存和冗余成功日志
python3 -m quant_core.cli research clean --task task.toml
python3 -m quant_core.cli research clean --task-id <task-id>
```

任务级 Champion 保存在 `.research/<task-id>/champion.py` 和 `champion.json`；每轮的
`result.json`、`decision.json` 和候选 patch 保存在 `runs/<run>/rounds/<round>/`，终局复盘
保存在 `runs/<run>/report.md`。活动 Loop 的阶段事件会同时输出到终端和
`.research/<task-id>/.tmp/runs/<run>/events.jsonl`，正常结束后清理临时事件与 worktree。

Loop 在达到轮数、总时长、连续技术失败或可选目标值时停止；`rejected` 是正常研究结果，
不增加连续失败计数。总时长预算只阻止启动下一 Round，不会中断已开始且仍在单轮超时内的
Agent。中断后重新执行同一命令会恢复活动 Run，没有完整决策的当前 Round 会作为失败证据
落盘；已经正常停止的 Run 不会恢复，而是分配下一个编号。

`research clean` 不会删除结构化 Round 结果、Decision、候选 patch、Champion、冻结的
Evaluation 数据或终局报告，也不会清理仍在运行的 Loop。

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
HARNESS_ISSUES.md         # Prioritized Harness issue and resolution history
.agents/skills/           # Task knowledge, prompts, scripts, assets, and outputs
.research/<task-id>/      # Champion, numbered Run history, cache, and temp observation
```

运行命令默认把本地日线缓存写到当前工作目录下的 `data/etf_daily.*`，把因子、回测和推荐等中间结果写到 `outputs/`。股票池不再保存在 `data/` 下；调用框架 CLI 时通过 `--universe path/to/universe.csv` 显式传入。

技能应显式把 `--root` 指向自己的 `outputs/` 子目录，避免在项目根目录产生中间结果：

```bash
python3 -m quant_core.cli --root .agents/skills/etf-sharpe-topk/outputs/sector_rotation factor compute --start 2026-05-01 --end 2026-05-31
```

## Scope

当前 Harness 面向策略研究和回测验证，不自动合并 Champion 到用户当前分支，
也不负责自动部署、实盘下单或资金管理。这些能力需要独立的安全设计和明确的
人工审批边界。
