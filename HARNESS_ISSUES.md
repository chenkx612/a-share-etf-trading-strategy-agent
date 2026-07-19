# Research Harness Issue Registry

本文档沉淀 Research Loop Harness 在真实运行中暴露的工程问题、根因、修正和遗留风险。
它只记录框架研发经验，不记录具体策略知识、研究结论或原始运行日志。

## 优先级定义

- **P0**：执行新的 Loop 研发前必须解决。问题会破坏 Gate 隔离、确定性预算、审计证据或评测正确性，
  继续运行可能使研究结论不可信。
- **P1**：推荐解决，但可以在明确风险并加强人工观测的情况下继续 Loop。问题主要影响研究效率、
  泛化能力、诊断质量或部分审计完整性。
- **P2**：有时间时优化。问题不会直接使评测或晋级结论失真，主要影响易用性、资源消耗或边缘流程。

## 当前结论

当前没有待解决的 P0，允许启动新的 Loop Run；仍需明确评估以下 P1/P2 遗留问题：

| 优先级 | 编号 | 问题 |
| --- | --- | --- |
| P1 | 13 | 单一 Development 汇总指标缺少稳健性和行为诊断 |
| P1 | 16 | 临时 Development runtime 缺失会清空耐久 Champion 指标 |
| P2 | 4 | 仍依赖 Prompt 阻止加载无关 Skill |
| P2 | 6 | 中断或基础设施失败仍会消耗研发轮次 |

## 一、待解决问题

### P0：新 Loop 前必须解决

当前无开放 P0。

### P1：推荐解决，可带风险继续

#### 13：单一 Development 汇总指标缺少稳健性和行为诊断

- **状态**：open
- **发现日期**：2026-07-18
- **问题与影响**：多轮候选在完整 Development 区间显著改善目标或回撤，但在 Gate 上没有相对改善；
  另有候选在 Gate 上与 Champion 指标完全相同，说明新增逻辑没有改变该区间的实际交易。当前决策不能
  区分跨阶段失效、触发样本不足和行为等价。
- **根因**：提交前只使用单一 Development 全期汇总；Decision 契约没有候选与 Champion 的订单、
  持仓、暴露和机制触发差异。
- **推荐方案**：
  - 由 Harness 预先固定 Development 子区间或滚动折叠，报告各段目标、约束和贡献方向。
  - 要求最低触发样本和跨段一致性；切片定义不能由 Agent 选择。
  - Gate 评估记录与 Champion 不同的信号日、订单、持仓、总暴露和机制触发次数。
  - 行为完全一致时输出显式 `behaviorally_equivalent`，而不只报告目标相同。
- **验收标准**：构造只在单个 Development 子段有效、Gate 完全不触发及订单完全相同的候选，
  分别验证稳健性门槛和行为差异分类。
- **带问题继续的条件**：限制 Run 轮数、保持人工实时观测，并把新 Champion 视为仍需独立最终验证的
  研究结果，不能直接进入生产或实盘。
- **遗留风险**：增加可见 Development 切片也会扩大反馈面，必须预先固定并限制指标，避免形成新的
  参数挖掘目标。
- **关联模块**：`src/quant_core/research/runner.py`、`src/quant_core/research/evaluator.py`

#### 16：临时 Development runtime 缺失会清空耐久 Champion 指标

- **状态**：open
- **发现日期**：2026-07-19
- **问题与影响**：一次故障 Run（当时编号 `003`，后按用户要求从研究历史移除）启动时，因
  disposable Development runtime 目录不存在，
  `champion.json` 中原本完整的 Development/Gate 指标和 `champion_metrics_key` 被写成 `null`。
  随后三轮 Agent 均在容器初始化阶段失败，没有评测可重新填充指标；终局报告因此声称本 Run 无法报告
  当前 Champion 指标。历史 Run `001` 的接受证据仍在，既有晋级决定未改变，但任务级耐久摘要被临时
  缓存状态降级。
- **根因**：`ResearchWorkspace._prepare_runtime` 把“需要重建 Development 派生缓存”和“已记录
  Champion 指标失效”绑定处理。仅仅缺少可重建的 Development runtime，也会无条件清空
  `champion_metrics`；而 Agent 失败路径不会重跑 Champion evaluator。
- **推荐方案**：
  - 把指标的适用性元数据与指标值分开保存。临时 runtime 缺失只触发缓存重建，不应删除已经冻结的
    Development/Gate 证据。
  - 只有数据内容指纹、固定区间、evaluator 契约或策略哈希变化时才标记指标需要重算；保留旧值并明确
    标注 `stale`，直到新评测原子替换，而不是先写 `null`。
  - 终局报告在本 Run 无候选评测时仍应读取最近有效 Champion 指标，并注明其来源 Run 和适用指纹。
- **验收标准**：删除 Development runtime 后启动一个在 Agent 阶段失败的 Run，确认缓存可重建、
  `champion.json` 的最近有效指标不丢失，报告能正确引用来源 Run；再改变固定评测输入，确认旧指标只被
  标记失效且不能用于晋级比较。
- **遗留风险**：在修复前可从历史接受 Round 的 `result.json`/`decision.json` 或 Run 报告恢复解释，
  但不得手工回填 `champion.json`，以免绕过原子状态语义。
- **关联代码**：`src/quant_core/research/workspace.py::_prepare_runtime`、
  `src/quant_core/research/report.py::generate_loop_report`

### P2：有时间时优化

#### 4：仍依赖 Prompt 阻止加载无关 Skill

- **状态**：mitigated
- **发现日期**：2026-07-18
- **问题与影响**：策略研发轮次可能自动加载 ETF 发现、选池、数据刷新或推荐 Skill。修改范围检查能
  阻止业务越界，但额外上下文会分散推理并增加 token 消耗。
- **根因**：策略使用 Skill 目录中的固定输入，路径和语义容易触发完整业务工作流。
- **当前缓解**：Prompt 明确声明股票池和缓存数据固定，禁止运行发现、选池、刷新和推荐工作流；
  Scope 继续禁止修改相关路径。
- **推荐方案**：若 OpenCode 支持能力级配置，为研究任务增加 Skill allowlist 或完全关闭 Skill。
- **验收标准**：事件日志中不再出现无关 Skill 加载，固定输入仍能正常读取。
- **关联代码**：`src/quant_core/research/runner.py::_prompt`

#### 6：中断或基础设施失败仍会消耗研发轮次

- **状态**：open
- **发现日期**：2026-07-18
- **问题与影响**：未提交候选的中断或基础设施失败会被记录为 failed 并计入 `max_rounds`。行为保守且
  可审计，但会减少真实候选预算。
- **根因**：系统无法自动证明中断候选是否完整，也不能安全恢复模型会话。
- **当前做法**：保留失败记录，不手工修改 Loop state，不创建临时 Research Root；后续调用继续使用
  同一任务根，由 Harness 管理 Run 编号。
- **推荐方案**：增加显式 `research loop reset-current` 或“诊断轮不计预算”模式。命令必须验证候选
  未提交、Champion 未变化，并保留独立审计记录。
- **验收标准**：只有满足安全前置条件的诊断失败可以释放研究预算；所有历史事件和失败原因仍可追溯。
- **遗留风险**：直接删除 Round 目录或编辑状态文件会破坏恢复语义，不能作为操作手册。

## 二、已解决问题

### P0：曾阻断可信研发

#### 15：Docker 嵌套 bind mount 在当前 runtime 失败且连续耗尽 Run

- **状态**：resolved
- **发现日期**：2026-07-19
- **解决日期**：2026-07-19
- **问题与影响**：真实 Run `003` 的三个候选均在 OpenCode 启动前失败，未产生假设、代码或评测；
  Harness 在不足一秒内把三次相同基础设施故障分别计为失败轮次，并因
  `max_consecutive_failures` 停止整个 Run。
- **根因**：
  - Runner 先把临时 `runtime_home` bind mount 到 `/home/agent`，再从其他宿主目录把 OpenCode
    认证和配置文件挂到其子路径；Docker Desktop 29.6.1/containerd runtime 对该拓扑稳定报
    `outside of rootfs`。
  - 容器临时 home 只获得认证和配置，遗漏 `~/.cache/opencode/models.json`，导致容器模型目录与
    宿主机不一致并把有效的 `xai/grok-4.5` 误判为不存在。
  - 当前 OpenCode 1.17.13 要求 `opencode run` 的 message 为位置参数，原 Runner 只通过 stdin
    传递 Prompt。
  - Loop 启动前没有真实容器预检；容器初始化错误被压缩为通用 `OpenCode session failed`，同源
    故障会持续消耗 Round。
- **修正**：
  - 将 `auth.json`、`opencode.jsonc` 和 `models.json` 复制到一次性 runtime home，只把该 home
    挂载一次；临时副本权限设为 `0600`，容器结束后随临时目录删除。
  - Prompt 改为 OpenCode 位置参数传入，容器仍在结束或超时后强制删除。
  - 新 Loop 分配 Run 编号前执行真实镜像预检，验证候选写入、Development 只读、Research Root
    遮蔽、runtime 文件可见，并确认 `task.toml` 配置的模型出现在容器模型目录中。预检失败不创建 Run。
  - OCI/container init、bind source、镜像和容器清理错误写入 `failure_kind=infrastructure` 并保留
    原始诊断；若运行中仍发生此类错误，Loop 在首个失败 Round 后以
    `infrastructure_failure` 停止，不再连续重试。
- **验证**：
  - 在当前 Docker Desktop 29.6.1/containerd runtime 上，真实预检通过，并确认容器内
    `opencode auth list` 显示 `xAI oauth`。
  - `xai/grok-4.5` 真实容器会话退出码为 0，返回结果可被 Harness 解析；没有修改模型配置。
  - 真实容器边界测试通过：候选可写自身文件，但不能修改只读数据，也不能通过绝对路径或符号链接访问
    Gate/Research Root。
  - 单元测试覆盖单一 home 挂载、runtime 文件复制和清理、模型目录预检、基础设施错误分类、预检不
    分配 Run 及运行中故障单轮熔断。
- **历史处置**：确认故障根因、完成修复和验收后，按用户要求删除 Run `003` 的系统失败工件，避免其
  进入后续策略研究历史；同时从冻结的 `001/004` 结果恢复 Champion 指标，并把
  `last_round_id` 恢复为 `002/005`。本条只保留框架工程证据，不把该故障 Run 视为研究结论。
- **关联问题/代码**：问题 6；`src/quant_core/research/runner.py::_stage_opencode_runtime`、
  `preflight_agent_container`、`_run_opencode_container`，`src/quant_core/research/loop.py`

#### 10：候选 Agent 曾可通过 Bash 访问 Research Root

- **状态**：resolved
- **发现日期**：2026-07-18
- **解决日期**：2026-07-19
- **问题与影响**：候选 Agent 位于隔离 worktree，但通过 Bash 使用主仓库绝对路径，仍能枚举
  `.research/<task-id>/` 下的 Champion、Run 和 Round 文件。工具级
  `external_directory=deny` 无法约束 Bash、Python、符号链接或其他子进程，可能造成 Gate 泄漏。
- **修正**：
  - 候选 OpenCode 强制通过一次性 Docker 容器运行，只精确挂载当前候选 worktree。
  - Development 数据、因子缓存和不承载生成输出的固定路径以只读方式覆盖。
  - Research Root、Gate runtime 和主工作区不挂载；脱敏研究历史继续由 Harness 注入 Prompt。
  - Docker 不可用或镜像缺失时直接失败，不回退到宿主机执行。
  - 每个容器使用唯一名称；退出或超时后强制删除，无法确认删除时禁止进入后续 Gate。
- **验证**：在 macOS Docker Desktop 的 Linux ARM64 runtime 中构建
  `quant-agent-research:latest`，真实容器测试确认候选可以写自身文件并读取 Development 数据，
  但不能修改只读数据或固定脚本，也不能通过 Bash、Python、绝对路径或符号链接访问 Gate 文件和
  原始 `.research`。
- **信任边界**：不防御 Docker daemon、宿主内核或容器运行时本身被攻破；OpenCode 认证文件仍作为
  必要运行输入只读挂载，不应指向包含其他用户数据的目录。
- **关联代码/测试**：`src/quant_core/research/runner.py::_run_opencode_container`、
  `tests/test_research_runner.py::test_agent_container_blocks_host_and_read_only_access`

#### 2：Development 搜索预算曾依赖 Prompt 次数限制

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：Agent 曾超过 Prompt 声明的实现和参数组数，并把部分回测称为诊断或复核；当时
  Harness 无法强制或核验这些次数。
- **设计决策**：Harness 采用协作型 Agent 信任模型：信任 Agent 不会主动攻击或逃逸系统边界，但
  防范超时、崩溃、错误输出和普通残留子进程。研究自由度不再以实现次数、参数组数、信号族冻结或逐次
  evaluation manifest 约束；单轮研发时间是唯一的搜索硬限制。
- **修正**：
  - 新增 `budget.round_minutes`；Harness 使用单调时钟执行硬截止，Prompt 提供绝对截止时间，动态
    Round 时钟提供剩余秒数以及 `research`、`converge`、`finalize`、`submit_now` 阶段。
  - 15、5、1 分钟节点写入活动事件；超时会终止 Agent 进程组并永久记录
    `result.json.round_timing`。
  - Agent 即使在截止后返回成功，Harness 仍将 Round 标为失败，且不再执行测试、Development、Gate
    或晋级。
- **验证**：测试覆盖动态时钟阶段、普通子进程随超时终止、超时返回和截止后成功返回；后两者均不会
  进入固定评测。
- **信任边界**：不防御 Agent 主动通过 `setsid`、容器逃逸或其他系统权限规避进程组。若未来改为
  对抗型 Agent，容器或操作系统沙箱应作为独立安全项目实施，而不是恢复评估次数限制。
- **关联代码/测试**：`src/quant_core/research/runner.py::_RoundClock`、
  `src/quant_core/research/runner.py::run_once`、`tests/test_research_runner.py`

#### 14：成功工件压缩曾被要求保留全部搜索轨迹

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：旧设计把每次 Development 探针、参数集合和假设切换都视为晋级所需的永久证据，
  因而认为成功日志压缩会破坏审计。
- **设计决策**：随问题 2 采用协作型 Agent 和单轮时间硬预算后，中间搜索路径属于研究员工作过程，
  不再是评测或晋级契约。耐久证据聚焦冻结输入、最终代码差异、固定 Development/Gate 指标、Decision、
  Parent Champion 和 Round 时间。
- **修正**：`result.json.round_timing` 永久保留时间边界；成功轮可以继续删除重复事件和日志，失败及
  超时原因仍写入结构化结果。无需 evaluation manifest，也不再比较 Agent 总结与全部中间尝试。
- **验证**：Round 时间契约有类型校验和回归测试；成功工件压缩保留结构化 `result.json`。
- **遗留风险**：压缩后不能复盘全部探索细节，这是明确接受的可观测性取舍，不影响固定 Gate 和
  Harness 晋级结论。
- **关联代码/测试**：`src/quant_core/research/runner.py::run_once`、
  `src/quant_core/research/workspace.py::compact_artifacts`、`tests/test_research_workspace.py`

#### 1：候选 Worktree 导入主工作区代码

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：候选策略已经修改，但开发集指标与基准完全一致；模块路径显示 `quant_core` 来自主
  工作区。正式评测若沿用该环境，会把候选当成基准策略评估，使晋级判定失真。
- **根因**：项目以 editable mode 安装，仅设置子进程 `cwd` 不能覆盖主工作区的导入映射。
- **修正**：所有 OpenCode、测试和回测子进程都把候选 worktree 的 `src/` 放在 `PYTHONPATH`
  首位，同时保留已有 `PYTHONPATH`。
- **验证**：真实子进程断言策略模块 `__file__` 位于候选路径；单元测试覆盖搜索路径顺序。
- **遗留风险**：非 Python 命令或自行重置环境变量的子进程仍可能绕过。长期可考虑独立虚拟环境。
- **关联代码**：`src/quant_core/research/runner.py::_workspace_env`

#### 5：不可行 Champion 的接受语义错误

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：原始 Champion 不满足 Gate 硬约束时，如果仍要求候选相对其提升目标函数，会错误
  拒绝已经满足全部约束的首个可行候选。
- **根因**：接受规则没有区分可行解和不可行解。
- **修正**：
  - 候选始终必须通过全部 Gate 硬约束。
  - Champion 不可行时，首个目标值有限且满足约束的候选直接替换 Champion。
  - Champion 可行后，候选才需要满足相对目标改善。
- **验证**：Decision 记录 `champion_constraints_passed` 和 `relative_improvement_required`；
  测试与真实 Run 均验证首个可行候选按该语义晋级。
- **遗留风险**：未来若有分层约束，需要升级为分层可行性比较。

#### 7：Development 改善曾被误当成可替代 Gate 的证据

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：候选可能在 Development 同时改善目标和回撤，但在近期 Gate 仍违反回撤或换手约束。
- **根因**：Development 与近期市场的波动结构、拥挤程度和触发频率不同。
- **修正**：继续由 Harness 独立运行隐藏 Gate；候选只有通过全部 Gate 约束后才可能晋级。反馈只暴露
  决策类别，不向后续研发 Agent 提供精确 Gate 指标。
- **验证**：真实 Run 中只有满足全部 Gate 约束的候选被提升，拒绝候选未污染 Champion。
- **遗留风险**：反复接收 Gate 通过/失败仍会形成间接反馈；上线前需要新的最终验证区间或前向观察。

#### 9：终局报告混入其他 Run 的实验

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：复用同一 Research Root 时，如果报告扫描任务下全部实验，会混合不同 Loop Run，
  使轮次统计、假设演进和 Champion 归因失真。
- **根因**：旧布局只有任务级平铺实验目录，没有明确 Run 边界。
- **修正**：任务级 Champion 与 Run 历史分离。每次 Loop 自动分配 `runs/<run>/`，Round 只写入该
  Run，状态和报告也保存在 Run 内；旧布局在首次访问时迁移。
- **验证**：连续启动两个 Run，确认分别生成独立目录和报告，且报告只读取所属 Run。
- **关联代码/测试**：`src/quant_core/research/report.py::_loop_round_ids`、
  `tests/test_research_report.py`

### P1：重要可靠性问题

#### 8：终局报告缺少完整 Decision 语义

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：首版报告只能看到接受/拒绝标签和简化原因，无法准确解释 Champion 是否可行及是否
  要求相对目标改善。
- **根因**：报告契约没有传入 Decision 的目标比较、Champion 可行性和逐项约束。
- **修正**：报告输入包含完整 `decision_objective`、`decision_constraints` 和
  `decision_reasons`；Prompt 要求以这些字段解释决策。
- **验证**：报告能准确说明 `champion_constraints_passed` 和
  `relative_improvement_required` 对晋级语义的影响。
- **遗留风险**：扩展多目标或分层约束时必须同步升级报告契约。
- **关联代码**：`src/quant_core/research/report.py::_experiment_records`

#### 11：Runner 早期失败产生幽灵 Round 并复用编号

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：真实 Run 在 `current_round` 写入后、候选目录创建前遭遇 Git 权限错误。恢复逻辑
  把该 Round 计为失败但没有落盘工件，随后复用相同 ID。最终完成计数大于 Round 工件数，少执行一个
  真实候选，并使报告出现不存在的缺失轮次。
- **根因**：
  - 恢复逻辑只在 Round 目录已存在时补写失败工件。
  - 编号器只扫描物理目录，不参考状态中已记录的 ID。
  - 决策记录对重复 ID 去重列表，却仍递增计数。
- **修正**：
  - 恢复未完成 Round 时创建目录并补写失败 `result.json` 和 `decision.json`。
  - 新编号同时参考物理目录和状态保留 ID。
  - 决策写入前校验完成数、决策计数和 ID 数量，拒绝重复 ID。
- **验证**：回归测试覆盖缺少 Round 目录的恢复、编号不复用及计数一致性。
- **遗留风险**：失败是否占用预算仍由问题 6 跟踪。
- **关联代码/测试**：`src/quant_core/research/loop.py::_next_round_id`、
  `src/quant_core/research/loop.py::_record_decision`、`tests/test_research_loop.py`

#### 12：报告未校验 Loop 状态与 Round 工件一致性

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：状态与工件数量不一致时，报告模型只能推断另有“记录不足”的轮次，不能明确这是
  Harness 状态损坏，并存在虚构缺失轮次内容的风险。
- **根因**：报告同时接收汇总计数和 Round 列表，却没有确定性完整性检查。
- **修正**：报告生成前检查完成数、Round ID 数量、决策计数、Round 目录和必需 JSON 工件，将异常
  写入 `loop.integrity_warnings`；Prompt 要求明确报告状态一致性问题且禁止虚构。
- **验证**：测试构造计数与 ID 不一致状态，确认警告进入报告输入；正常和 legacy 状态不误报。
- **遗留风险**：检查只能诚实报告已有损坏，不能自动修复历史状态。
- **关联代码/测试**：`src/quant_core/research/report.py::_loop_integrity_warnings`、
  `tests/test_research_report.py`

### P2：易用性和效率问题

#### 3：回测成功但无标准输出，Agent 误判失败

- **状态**：resolved
- **发现日期**：2026-07-18
- **问题与影响**：Evaluator 成功退出但 stdout 为空，Agent 误以为失败并反复搜索指标文件。
- **根因**：执行契约没有向 Agent 说明静默成功语义和指标产物路径。
- **修正**：Prompt 显式提供格式化后的 Development metrics 路径，并说明空 stdout 是正常成功。
- **验证**：Prompt 测试校验实验 ID 正确展开到指标路径。
- **遗留风险**：其他静默命令也应在契约中声明产物路径。

## 新问题记录模板

新增问题时先放入“待解决问题”的对应优先级；解决后移动到“已解决问题”，保留原优先级以及发现、
修正和验证记录。

```markdown
### N：简短标题

- **状态**：open、mitigated 或 resolved
- **发现日期**：YYYY-MM-DD
- **问题与影响**：
- **触发条件**：
- **根因**：
- **方案或修正**：
- **验收或验证**：
- **遗留风险**：
- **依赖关系**：
- **关联代码/测试/实验**：
```
