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
- **候选隔离**：每轮候选在独立 worktree 和一次性容器中开发，失败或拒绝不会污染下一轮和当前工作分支。
- **可恢复运行**：Run 状态、累计时间、连续失败、当前 Round 和停止原因都会持久化。
- **证据可审计**：每轮保存输入、代码差异、日志、指标、决策及其父 Champion。
- **受控自动化**：循环在达到目标、轮数、时长或连续失败预算时停止，并生成终局复盘。

## Quick start

安装项目：

```bash
python3 -m pip install -e ".[dev]"
pytest
```

构建候选 Agent 镜像：

```bash
docker build \
  --file docker/research-agent.Dockerfile \
  --tag quant-agent-research:latest \
  .
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
- `evaluation.mode = "fixed"` 保持原有的不重叠 Development/Gate 评测；
  `"walk_forward"` 用最近训练窗口逐折选参、在后续不重叠验证折评测。首版要求
  `validation_months = step_months`，参数网格由策略模块的 `parameter_grid()` 声明，
  固定 evaluator 通过 `select_with_params(...)` 评分并选参。
- 硬约束运算符为 `>=`、`<=` 和 `abs<=`；可选 Test 区间必须位于 Gate 之后，使用
  `research test --task task.toml` 对当前 Champion 单独评测，结果不参与晋级。
- `evaluation.acceptance.minimum_improvement` 控制合格 Champion 之上的最小改善；
  `evaluation.target.objective_at_least` 可在目标达成后提前停止。
- `budget.round_minutes` 是每轮候选研发的硬时限；未配置时兼容使用
  `opencode.timeout_minutes`。Harness 会向 Agent 提供绝对截止时间，并在候选工作区刷新
  `.quant-research-round.json`，其中包含剩余秒数和 `research`、`converge`、`finalize`、
  `submit_now` 阶段。
- 固定 evaluator 要求策略实现 `select(daily, universe, start, end)`，返回包含 `date`、
  `symbol`、`target_weight` 的 pandas DataFrame。权重必须有限且非负，每日总和不超过 1。

运行前还应确认 OpenCode 模型与认证可用，并且任务声明的测试和回测命令可以在仓库根目录运行。
Provider 模型名、推理档位、认证和价格属于外部状态，不在仓库中维护静态清单。

### Candidate Agent container

候选研发阶段必须通过 Docker 执行，不会在 Docker 不可用或镜像缺失时回退到宿主机
OpenCode。容器只挂载当前候选 worktree；worktree 的父级 Research Root、Gate runtime 和
主工作区不会进入容器。候选中的 `data/`、`outputs/factors/` 以及不承载回测生成输出的
`scope.forbidden` 路径会以只读方式重新挂载。候选代码和回测生成目录保持可写。

后续 Round 所需的脱敏研究历史继续由 Harness 注入 Prompt，不会通过挂载原始
`.research` 提供。若 `research run-once` 的 workspace 自身包含 `.research`，该目录会在
容器中以空目录覆盖。Agent 容器退出后，测试、Development 复核和 Gate 评估仍由 Harness
执行。

默认镜像为 `quant-agent-research:latest`，可通过
`QUANT_RESEARCH_AGENT_IMAGE` 覆盖。Harness 默认读取以下宿主机文件：

```text
~/.local/share/opencode/auth.json
~/.config/opencode/opencode.jsonc
~/.cache/opencode/models.json
```

可分别使用 `QUANT_OPENCODE_AUTH_FILE`、`QUANT_OPENCODE_CONFIG_FILE` 和
`QUANT_OPENCODE_MODELS_FILE` 覆盖路径。Harness 将存在的文件复制到一次性 runtime home，
再只挂载该 home；容器结束后删除临时副本，避免 Docker 不兼容的嵌套 bind mount。
这些输入只用于 OpenCode 认证、配置和模型目录，不应指向包含其他用户数据的文件。

新 Loop 分配 Run 编号前会用真实研究镜像执行容器预检，验证候选目录可写、固定输入只读、
Research Root 被遮蔽、OpenCode runtime 文件可见且任务配置的模型存在。预检失败不会创建
Run 或消耗 Round；运行中识别出的容器基础设施故障会立即停止 Run，不会连续重试至耗尽预算。

镜像只包含 OpenCode、基础工具和 `pyproject.toml` 声明的 Python 依赖，不包含项目源码。
候选代码始终来自当前 worktree 挂载；因此普通源码修改和每轮 Loop 都不需要重新构建镜像，
只有 Dockerfile、Python 依赖或 OpenCode 版本变化时才需要重新构建。

构建镜像并启动 Docker 后，可运行真实文件边界验收：

```bash
QUANT_TEST_AGENT_CONTAINER=1 pytest -q \
  tests/test_research_runner.py::test_agent_container_blocks_host_and_read_only_access
```

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
不增加连续失败计数。总时长预算只阻止启动下一 Round；`budget.round_minutes` 会硬终止超时的
候选研发，并在 `result.json.round_timing` 中保留时间证据。中断后重新执行同一命令会恢复活动
Run，没有完整决策的当前 Round 会作为失败证据
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

### 生成低成本 ETF 轮动池

仓库级建池命令与各技能解耦，默认只生成预览：

```bash
python3 scripts/build_liquid_etf_universe.py --date YYYY-MM-DD
```

预览产物写入 `outputs/liquid_etf_universe/`，包括 `shortlist.csv`、
`selected_universe.csv`、`correlation.csv` 和带完整剔除原因的 `summary.json`。
显式增加 `--apply` 时，命令会备份旧池并原子更新
`universes/liquid_etf_rotation.csv`；它不会修改 `universes/sector_rotation.csv`。
任一入围 ETF 行情刷新失败或最终池为空时，命令保留预览和审计产物但拒绝更新正式池。

默认参数为 `--min-fund-size 10000000000`、`--shortlist-size 100`、
`--lookback-days 252`、`--min-observations 120` 和
`--corr-threshold 0.90`。命令只扫描当前 ETF 产品，不主动纳入 LOF，并允许当前
产品列表带来的幸存者偏差。筛选和排序只使用当前规模及名称分组，不使用历史或当日
收益排名；共享前复权历史仅用于剔除历史不足和普通 Pearson 相关系数大于阈值的产品，
负相关不取绝对值。`--shortlist-size` 是为避免单次拉取过多行情设置的上限；
相关性去重后不强制补足至该上限。

## Project layout

```text
src/quant_core/research/  # Loop/harness contracts, runner, evaluator, state, report
src/quant_core/data/      # Market data download, cache, and table IO
src/quant_core/factors/   # Deterministic factor calculations
src/quant_core/strategy/  # Candidate and built-in strategy implementations
src/quant_core/backtest/  # Fixed simulation engine and metrics
scripts/                  # Repository-level operational and universe-building commands
tests/                    # Framework and harness tests
universes/                # Repository-level stock pools shared across skills
HARNESS_ISSUES.md         # Prioritized Harness issue and resolution history
.agents/skills/           # Skill-specific knowledge, prompts, scripts, parameters, and outputs
.research/<task-id>/      # Champion, numbered Run history, cache, and temp observation
```

运行命令默认把本地日线缓存写到当前工作目录下的 `data/etf_daily.*`，把因子、回测和推荐等中间结果写到 `outputs/`。跨技能共享的股票池放在 `universes/`，当前 sector-rotation 的 canonical 股票池为 `universes/sector_rotation.csv`；技能目录只保存技能专属输入和产物。股票池不保存在 `data/` 下；调用框架 CLI 时通过 `--universe path/to/universe.csv` 显式传入。

技能应显式把 `--root` 指向自己的 `outputs/` 子目录，避免在项目根目录产生中间结果：

```bash
python3 -m quant_core.cli --root .agents/skills/etf-sharpe-topk/outputs/sector_rotation factor compute --start 2026-05-01 --end 2026-05-31
```

## Scope

当前 Harness 面向策略研究和回测验证，不自动合并 Champion 到用户当前分支，
也不负责自动部署、实盘下单或资金管理。这些能力需要独立的安全设计和明确的
人工审批边界。
