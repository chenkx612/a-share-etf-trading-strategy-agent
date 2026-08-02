# Quant Agent

Quant Agent 是一个以 **loop/harness engineering 驱动自动化量化交易策略研究**
的工程平台。它将策略研发从一次性的人工调参或 Agent 对话，转化为可持续运行、
可客观评测、可恢复、可审计的自动化研究闭环：

```text
定义研究目标、数据区间、约束和预算
→ Agent 提出假设并实现候选策略
→ Harness 运行测试与确定性回测
→ 在隔离的 Gate 数据上评测候选
→ 对可晋级候选执行 Guard
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
conda run --no-capture-output -n quant python -m pip install -e ".[dev]"
conda run --no-capture-output -n quant pytest
```

仓库开发和测试统一使用已有的 `quant` Conda 环境。不要在仓库中创建
`.venv/`、`venv/`、`env/` 等项目级或一次性 Python 虚拟环境；若环境或依赖
缺失，应先报告缺失项，不得为绕过环境约束临时生成虚拟环境。

构建候选 Agent 镜像：

```bash
docker build \
  --file docker/research-agent.Dockerfile \
  --tag quant-agent-research:latest \
  .
```

运行自动化策略研究循环：

```bash
conda activate quant
quant-agent loop active-etf-rerank-topk

# 为事后 Codex 复盘保留可清理的诊断证据包
quant-agent loop active-etf-rerank-topk -d
```

`loop` 的任务参数可使用 `tasks/` 下的文件名、任务 ID 或 TOML 路径。原有
`quant-agent research loop --task path/to/task.toml --retain-diagnostics`
接口继续兼容已有脚本。

启动新的 Loop Run 前，先检查
[Research Harness Issues](ISSUES.md)；存在开放 P0 时不应启动新 Run。

### Task and candidate contract

研究任务在 `task.toml` 中声明目标、数据、Development/Gate 区间、允许修改的范围、
专项测试、硬约束、Champion 改善要求、模型配置和运行预算。每次调用自动分配新的
编号 Run；重复研发时继续使用默认 `.research`，无需显式传入 `--research-root`。`tasks/`
目录中的 TOML 文件是可直接运行的任务配置示例，字段由
`src/quant_core/research/contracts.py` 验证。

- `scope.editable` 当前只能声明一个仓库相对策略文件。
- 策略模块由唯一的 `scope.editable` Python 文件确定性推导，无需重复声明。
- Walk-Forward 的 evaluator contract 由 Harness 固定代码路径、整个 `tests/` 和
  `data.universe` 自动组成；Fixed 模式对 editable 策略之外的仓库内容整体取指纹。
- `baseline.mode = "workspace"` 从工作区策略初始化 Champion；`"none"` 用于 0→1 研发，
  可配合 `baseline.exclude` 从候选基座排除旧策略实现。
- `commands.tests` 只声明 pytest 文件或节点；Harness 自动添加 Python、pytest 和静默参数。
- 回测命令支持 `{python}`、`{universe}`、`{start}`、`{end}`、`{run_id}`、
  `{strategy_name}` 和 `{strategy_module}` 占位符。Walk-Forward 评测命令及
  `outputs/backtests/{run_id}/metrics.json` 指标路径由 Harness 固定生成。
- `evaluation.mode = "fixed"` 保持原有的不重叠 Development/Gate 评测；
  `"walk_forward"` 用最近训练窗口逐折选参、在后续不重叠验证折评测。调参边界由
  顶层 `parameter_selection.schedule` 显式声明，而不再锚定 Development/Gate 的
  `start`；同频任务因此共享相同交易日边界。当前月频契约使用自然月首个交易日收盘
  选参、下一交易日开盘生效。参数网格由策略模块的 `parameter_grid()` 声明，固定
  evaluator 通过 `select_with_params(...)` 评分并选参。评分区间若从月中开始，
  evaluator 会从此前最近的调参边界预热持仓和路径状态，但只统计配置区间内的指标。
- Walk-Forward 区间既可继续使用绝对 `development`/`gate`/`guard`，也可通过
  `[evaluation.walk_forward.relative]` 声明
  `anchor = "latest_complete_universe_date"` 以及可配置的
  `development_months`、`gate_months`、可选 `guard_months`。相对区间在新 Run
  启动时按本地行情最新日期解析；该日期必须覆盖当前 Universe 的全部 symbol。
  Harness 会为 Run 冻结内容寻址的完整评测输入、解析后的绝对区间和 Development
  视图，恢复及后续 Round 不受工作区数据更新影响。历史不足以覆盖首个 Development
  折训练窗、最新日数据不完整或任一区间没有交易日时会在研究开始前明确失败，不会
  自动回退日期或缩短区间。
- Walk-Forward 任务通过顶层 `[parameter_selection]` 唯一声明 `train_months`、
  `objective`、`constraints`、`max_parameter_sets` 和 `schedule`。Research、逐日推荐
  与生产因果曲线共用该政策；`[production]` 只声明曲线窗口、基准和可选数据要求。
  策略调用内的迟滞等路径状态在调参边界重置，并在同一周期内连续回放。
- 硬约束运算符为 `>=`、`<=` 和 `abs<=`。可选 `[evaluation.guard]` 与 Guard 区间必须成对配置，
  且严格位于 Gate 之后；每轮候选先通过 Primary Gate，才会执行 Guard。Harness 以冻结 Universe
  的无费、日度再平衡 open-to-open 等权基准计算 Gate 与 Guard 年化超额收益；当其下滑超过
  `max_excess_annual_return_degradation`（由任务配置，现有任务为 5 或 10 个百分点）即拒绝，Guard
  通过后才晋级 Champion。
  Guard 完整证据保存在该轮 `guard.json`，后续候选 Prompt 与研究记忆只会收到统一的拒绝原因，不会收到
  Guard 精确指标。
- `evaluation.acceptance.minimum_improvement` 控制合格 Champion 之上的最小改善；
  `evaluation.target.objective_at_least` 可在目标达成后提前停止。
- 配置 `[production]` 的任务在 Run 报告完成后，会将该 Run 冻结的最终 Champion
  原子同步到 `scope.editable` 声明的生产策略文件。同步只在 Run 内产生新 Champion 时发生，
  不执行 `git add`、commit 或 push。若策略文件在 Run 启动时已与 Champion 不一致，Harness 会在
  分配 Run 后立即停止、不会启动 Round；若在 Run 期间被人工修改，同样不会覆盖。两种情况都会在
  `artifacts/production-sync.json` 留下哈希证据并以非零状态退出。若冲突发生在已有新 Champion 的
  终局同步阶段，处理后重新运行同一 Loop 命令会先恢复该同步，不会分配新 Run。
- `budget.round_minutes` 是每轮候选研发的硬时限；
  `execution.command_timeout_minutes` 是固定测试、Development、Gate 和 Champion 重评等
  单条 Harness 命令的超时，两者相互独立。旧任务契约仍可从
  `opencode.timeout_minutes` 兼容读取这两个值，但新任务必须显式配置前两项。Harness 会向
  Agent 提供绝对截止时间，并在候选工作区刷新
  `.quant-research-round.json`，其中包含剩余秒数和 `research`、`converge`、`finalize`、
  `submit_now` 阶段。候选容器的 OpenCode Bash 默认超时由 Harness 显式设为同一 Round
  时限，实际仍受实时剩余 Round 时间约束，不会再隐含使用较短的工具默认值。每次
  Development 前，Agent 必须先提交与当前策略哈希一致的 checkpoint，再通过
  `python3 -m quant_core.research.attempt evaluate` 请求 Harness 执行固定评测；walk-forward
  evaluator 通过 Harness 状态验证冻结副本，并用首尾折的基准耗时估算完整网格。候选容器中的
  evaluator 模块由只读门面覆盖：低层 `evaluate_candidate()` 与 selection 校验仍可用于诊断，
  顶层 walk-forward 函数和模块 CLI 只会抛出 `HarnessExecutionRequired`。权威实现不进入候选
  解释器，因此修改 marker、模块全局变量或伪造 `WalkForwardExecutionControl` 都不能恢复它。
  评测预算会
  扣除 Round 时长的四分之一、最多 300 秒作为 finalization 预留；若预计超预算则提前拒绝，并把
  进度和估算写入本次 backtest 输出目录的 `progress.json`。Agent 可用
  `python3 -m quant_core.research.checkpoint submit <metadata.json>` 冻结当前策略；超时时
  Harness 只恢复截止前最近的有效 checkpoint，随后照常执行固定测试、Development、Gate 和（仅当 Primary Gate 通过时的）Guard。
  每次成功的顶层 Development 请求按候选哈希形成一个不可重复的最小事实记录；Agent 可立即用
  `python3 -m quant_core.research.attempt learn <attempt-id> <learning.json>` 补充经验解释。
  `result.json.development_attempts` 保留所有同轮有效尝试并标识最终提交项；learning 缺失不影响
  事实留存、固定评测或晋级。
  `research run-once --output` 必须与候选 workspace 不同，以保持 checkpoint 对 Agent 不可写。
- 固定 evaluator 要求策略实现 `select(daily, universe, start, end)`，返回包含 `date`、
  `symbol`、`target_weight` 的 pandas DataFrame。权重必须有限且非负，每日总和不超过 1。
- Harness 控制器、固定测试和 Development/Gate/Guard 评测必须从 Conda `quant` 启动。Harness 会
  对实际 Python、ABI、平台、Conda build 和 Python distribution 生成稳定环境指纹；环境变化会使
  历史 Champion 指标变为 stale，并在下一次晋级比较前重评。完整清单保存在
  `.research/<task-id>/environments/<sha256>.json`。

运行前还应确认 OpenCode 模型与认证可用，并且任务声明的测试及 Fixed 模式回测命令可以在仓库根目录运行。
Provider 模型名、推理档位、认证和价格属于外部状态，不在仓库中维护静态清单。

### Candidate Agent container

候选研发阶段必须通过 Docker 执行，不会在 Docker 不可用或镜像缺失时回退到宿主机
OpenCode。容器只挂载当前候选 worktree；worktree 的父级 Research Root、Gate runtime 和
主工作区不会进入容器。候选中的框架、测试、universe、`data/`、`outputs/factors/`、
`.agents/` 和 `.research/` 等 Harness 固定保护路径会以只读方式重新挂载。
editable 策略和回测生成目录保持可写。
Harness 还会把宿主临时身份文件只读挂载到
`/run/quant-research/candidate-container`，并继续使用 `cap-drop=ALL` 和
`no-new-privileges`。真正的执行边界由另一个 OS 级只读 bind mount 提供：候选工作区中的
`src/quant_core/research/evaluator.py` 被替换为受限门面，对应 `__pycache__` 也以空只读目录遮蔽，
避免从候选可写 bytecode 恢复被隐藏实现。门面和挂载源均位于候选 worktree 之外，Agent 无法通过
同一 workspace 的别名路径改写。
官方 Attempt receiver 始终在无此标记的宿主执行，因此 checkpoint、去重、预算投影、
finalization 预留和 Attempt 耐久记录保持由 Harness 控制。此边界不禁止 pandas、自写轻量回测器
或 `evaluate_candidate()` 等低层诊断。

后续 Round 所需的脱敏研究历史继续由 Harness 注入 Prompt，不会通过挂载原始
`.research` 提供。若 `research run-once` 的 workspace 自身包含 `.research`，该目录会在
容器中以空目录覆盖。Agent 容器退出后，测试、Development 复核、Gate 与 Guard 评估仍由 Harness
执行。

默认镜像为 `quant-agent-research:latest`，可通过
`QUANT_RESEARCH_AGENT_IMAGE` 覆盖。Harness 默认读取以下宿主机文件：

```text
~/.local/share/opencode/auth.json
~/.config/opencode/opencode.jsonc
~/.cache/opencode/models.json
```

可分别使用 `QUANT_OPENCODE_AUTH_FILE`、`QUANT_OPENCODE_CONFIG_FILE` 和
`QUANT_OPENCODE_MODELS_FILE` 覆盖路径。Harness 将存在的文件复制到权限受限的一次性 runtime home，
再只挂载该 home；容器结束后删除临时副本，避免 Docker 不兼容的嵌套 bind mount。
这些输入只用于 OpenCode 认证、配置和模型目录，不应指向包含其他用户数据的文件。
对轮换型 OAuth Provider 凭据，Harness 会按认证文件加跨进程锁。固定提示、`--pure` 且全部工具
禁用的宿主 OpenCode 认证预检负责在分配 Run 或 Round 前验证认证。候选和报告使用包含完整 OAuth
状态的一次性副本；会话结束后，Harness 只接受结构合法的目标 Provider 变化，再用可信宿主探针验证，
最后将该 Provider 原子合并回宿主认证文件。其他 Provider、临时配置修改和未经验证的状态不会回写。
即使候选超时、失败或被中断，Harness 仍会在释放认证锁前尝试回收已经轮换的凭据。

新 Loop 分配 Run 编号前会用真实研究镜像执行容器预检，验证候选目录可写、固定输入只读、
Research Root 被遮蔽、OpenCode runtime 文件可见且任务配置的模型存在。预检失败不会创建
Run 或消耗 Round。Harness 还会在 Run 和后续 Round 分配前发起无工具的轻量 Provider 认证请求；
明确的认证或 refresh token 故障会标记为 `infrastructure` 并立即停止，不会连续重试至耗尽预算。
每个真实候选 worktree 在分配 Round 和启动 Agent 前还会执行只读 bind 可见性探针；Docker Desktop
瞬态不可见会在固定 5 秒内有界退避，持续故障保留 Run 级诊断并停止，但不消耗候选研究轮次。

镜像只包含 OpenCode、基础工具和 `pyproject.toml` 声明的 Python 依赖，不包含项目源码。
候选代码始终来自当前 worktree 挂载；因此普通源码修改和每轮 Loop 都不需要重新构建镜像，
只有 Dockerfile、Python 依赖或 OpenCode 版本变化时才需要重新构建。

构建镜像并启动 Docker 后，可运行真实文件边界验收：

```bash
QUANT_TEST_AGENT_CONTAINER=1 pytest -q \
  tests/test_research_runner.py::test_agent_container_blocks_host_and_read_only_access \
  tests/test_research_runner.py::test_real_agent_preflight_validates_the_frozen_development_view \
  tests/test_research_attempt.py::test_candidate_container_official_attempt_uses_host_receiver
```

### Run operations

常用研究命令：

```bash
# 运行一次候选研发，不管理 Champion
conda run --no-capture-output -n quant python -m quant_core.cli research run-once \
  --task tasks/sharpe-corr-threshold-optimization.toml \
  --experiment-id experiment-001 \
  --output path/to/experiment

# 重新生成最近或指定 Run 的终局报告
conda run --no-capture-output -n quant python -m quant_core.cli research report \
  --task tasks/sharpe-corr-threshold-optimization.toml
conda run --no-capture-output -n quant python -m quant_core.cli research report \
  --task tasks/sharpe-corr-threshold-optimization.toml --run 2

# 清理临时 worktree、派生缓存和冗余成功日志
conda run --no-capture-output -n quant python -m quant_core.cli research clean \
  --task tasks/sharpe-corr-threshold-optimization.toml
conda run --no-capture-output -n quant python -m quant_core.cli research clean \
  --task-id <task-id>
```

任务级 Champion 保存在 `.research/<task-id>/champion.py` 和 `champion.json`；每轮的
`result.json`、`decision.json` 和可从冻结 Parent 重放的 `candidate.patch` 保存在
`runs/<run>/artifacts/rounds/<round>/`。`baseline.mode = "none"` 的首次 0→1 晋级还会保留完整
`candidate.py`，其内容哈希与 Round submission 一致。终局复盘
保存在 `runs/<run>/report.md`；冻结的叙述与指标输入保存在 `artifacts/report/input.json`，由最终 Champion
反向重放 Accepted Patch、再正向核验所有 Candidate 得到的版本事实链保存在
`artifacts/report/facts.json`。新 Run 的恢复状态与冻结输入契约位于 `artifacts/state.json` 和
`artifacts/contracts/`；已存在的旧 Run 保持原布局且可继续读取。Guard 启用时，仅通过 Primary Gate 的
候选会在 `runs/<run>/artifacts/rounds/<round>/guard.json` 写入区间、策略/基准年化收益、超额收益、
衰减、阈值及输入/策略指纹；`decision.json` 仅引用其路径和结果。Run state 记录
`guard_query_count`。终局不再执行 Champion Test，也不再向报告追加 Test 附录；
报告 Agent 失败时仍会生成最小 `report.md` 人类入口。报告只能基于事实链解释机制演进，
组合变更不得自动归因为单一机制。报告认证失败
不会改变 Loop 结果；重新认证后可用 `research report --run ...` 基于相同输入单独重试。活动 Loop
的阶段事件会同时输出到终端和
`.research/<task-id>/.tmp/runs/<run>/events.jsonl`，正常结束后清理临时事件与 worktree。
使用 `--retain-diagnostics` 时，Harness 还会将不含精确 Gate 指标的事件时间线、阶段日志尾部和
确定性 `diagnostic-summary.json` 保存到新 Run 的 `artifacts/diagnostics/`（旧 Run 仍沿用历史缓存路径）。该开关对
同一活动 Run 的恢复保持有效，适合在 Loop 停止后让 Codex 只检查摘要和异常 Round；这些缓存可由
`research clean` 删除，不参与晋级或后续候选 Prompt。

Loop 在达到轮数、总时长、连续技术失败或可选目标值时停止；`rejected` 是正常研究结果，
不增加连续失败计数。总时长预算只阻止启动下一 Round；`budget.round_minutes` 会硬终止超时的
候选研发，并在 `result.json.round_timing` 中保留时间证据。若恢复了 checkpoint，`result.json`、
`decision.json` 和事件日志会记录 checkpoint ID、策略哈希及 `submitted_by_timeout`。中断后重新执行同一命令会恢复活动
Run，没有完整决策的当前 Round 会作为失败证据
落盘；已经正常停止的 Run 不会恢复，而是分配下一个编号。

`research clean` 会删除可选诊断缓存。schema 6 的成功 Round 会删除可由结构化结果与认证 Patch
替代的 checkpoint、成功 bind-probe、评测日志和 Agent 事件；失败或中断 Round 保留诊断证据。
清理不会改写或删除已完成历史 Run，也不会删除结构化 Round 结果、Decision、patch、首次 0→1
候选源码、Champion、冻结的 Evaluation 数据或终局报告，也不会清理仍在运行的 Loop。

## Quant framework

`src/quant_core/` 同时提供 Harness 所依赖的数据、因子、策略、回测、优化、推荐和
报告等确定性量化框架能力。它们可以独立使用：

```bash
python3 -m quant_core.cli data update --universe path/to/universe.csv --universe-name default --start 2024-01-01 --end 2024-12-31
python3 -m quant_core.cli factor compute --start 2024-01-01 --end 2024-12-31
python3 -m quant_core.cli backtest run --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31
python3 -m quant_core.cli optimize grid --universe path/to/universe.csv --strategy sharpe-corr-threshold --start 2024-03-01 --end 2024-12-31 --top-n 3,5,10
quant-agent recommend active-etf-rerank-topk
quant-agent recommend active-etf-rerank-topk --date 2026-07-24

# Offline verification against the existing local market-data cache
quant-agent recommend active-etf-rerank-topk --date 2026-07-24 --skip-refresh
```

`recommend` 会直接在终端打印次日 ETF/现金目标持仓、最近一次与下一次调参日，
并自动生成近期严格因果收益曲线。曲线逐个调参周期重放：每个交易日只沿用当时
最近一个调参日搜索得到的参数，不会用后续调参结果回填历史。
在线刷新会保留共享缓存近五年的可用历史，并额外预留 14 个日历日处理非交易日边界；
上市不足五年的 ETF 从实际可用首日开始，不会因历史不足五年而导致刷新失败。

缺少 `pyarrow` 时，本地表会自动降级为 CSV；安装项目依赖后默认使用 Parquet。

### 生成低成本 ETF 轮动池

仓库级建池命令与各技能解耦，默认只生成预览：

```bash
quant-agent universe build-liquid --date YYYY-MM-DD
```

预览产物写入 `outputs/liquid_etf_universe/`，包括 `shortlist.csv`、
`selected_universe.csv`、`correlation.csv` 和带完整剔除原因的 `summary.json`。
显式增加 `--apply` 时，命令会备份旧池并原子更新所选 `--root` 下的
`universes/liquid_etf_rotation.csv`；它不会修改 `universes/sector_rotation.csv`。
任一入围 ETF 行情刷新失败或最终池为空时，命令保留预览和审计产物但拒绝更新正式池。

默认参数为 `--min-fund-size 10000000000`、`--shortlist-size 100`、
`--lookback-days 252`、`--min-observations 120` 和
`--corr-threshold 0.90`。命令只扫描当前 ETF 产品，不主动纳入 LOF，并允许当前
产品列表带来的幸存者偏差。筛选和排序只使用当前规模及名称分组，不使用历史或当日
收益排名；共享前复权历史仅用于剔除历史不足和普通 Pearson 相关系数大于阈值的产品，
负相关不取绝对值。`--shortlist-size` 是为避免单次拉取过多行情设置的上限；
相关性去重后不强制补足至该上限。

### 生成高活跃度 ETF 轮动池

独立建池命令先要求场内规模代理值不低于 10 亿元，再按规模预选 Top 100，以近 60 日
成交额过滤产品，并按流动性顺序进行贪心收益相关性去重：

```bash
quant-agent universe build-active --date YYYY-MM-DD
```

预览产物写入 `outputs/active_etf_universe/`，包括规模和流动性审计、相关性矩阵、
共同观察数矩阵、逐产品选择决策及最终池。显式增加 `--apply` 时，命令会备份并原子
更新所选 `--root` 下的 `universes/active_etf_rotation.csv`，不会修改其他正式股票池。
默认要求近 60 日至少 50 个有效成交额观察值、成交额中位数不低于 5000 万元，并使用近 252 日收益和
至少 120 个共同观察值。候选按成交额中位数、规模和代码确定顺序；只有与所有已选
ETF 的普通 Pearson 相关性均不高于 `0.90` 时才入池。共同历史不足的候选拒绝入池，
负相关不取绝对值，最终池不强制补足数量。

当前正式高活跃度池以 `universes/active_etf_rotation.csv` 为唯一成分来源。2026-07-25
应用的快照使用截至 2026-07-24 的行情：1652 只场内 ETF 中有 432 只达到 10 亿元
规模下限，规模 Top 100 中有 97 只通过流动性和历史过滤，相关性去重后保留 38 只。
最终池实际最小规模约 73.9 亿元，近 60 日成交额中位数最低约 9268 万元；任意两只
ETF 均有 252 个共同收益观察，最大两两相关性为 `0.8931`。池中包含境内宽基、风格、
行业主题、港股、美股和黄金等可交易风险暴露；具体成分随正式 CSV 更新，不在 README
重复维护。Top 100 是控制单次行情刷新成本的明确边界，因此该池表示大规模 ETF 中的
高活跃、低同质化轮动候选，不表示对全部规模达标 ETF 的穷举筛选。

## Project layout

```text
src/quant_core/backtest/        # Fixed simulation engine and metrics
src/quant_core/commands/        # CLI command handlers
src/quant_core/data/            # Market data download, cache, and table IO
src/quant_core/factors/         # Deterministic factor calculations
src/quant_core/recommendation/  # Parameter search, causal replay, and next-day holdings
src/quant_core/research/        # Loop/harness contracts, runner, evaluator, state, report
src/quant_core/strategy/        # Candidate and built-in strategy implementations
src/quant_core/universe/        # Deterministic repository-level universe builders
tasks/                          # Canonical task-id TOML contracts
tests/                          # Framework and harness tests
universes/                      # Repository-level stock pools shared across skills
data/ and outputs/              # Ignored local market data and general CLI artifacts
.agents/skills/                 # Skill-specific knowledge, scripts, and outputs
.research/<task-id>/            # Champion, manifests, immutable Run history, cache, and temp state
ISSUES.md                       # Prioritized Harness issue and resolution history
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
