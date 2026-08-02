# Research Harness Issue Registry

本文档沉淀 Research Loop Harness 在真实运行中暴露的工程问题、根因、修正和遗留风险。
它只记录框架研发经验，不记录具体策略知识、研究结论或原始运行日志。

## 优先级定义

- **P0**：执行新的 Loop 研发前必须解决。问题会破坏 Gate 隔离、确定性预算、审计证据或评测正确性，
  继续运行可能使研究结论不可信。
- **P1**：推荐解决，但可以在明确风险并加强人工观测的情况下继续 Loop。问题主要影响研究效率、
  泛化能力、诊断质量或部分审计完整性。
- **P2**：有时间时优化。问题不会直接使评测或晋级结论失真，主要影响易用性、资源消耗或边缘流程。

## 记录原则与格式

新增问题先放入“待解决问题”的对应优先级，解决后按价值决定是否移入“已解决问题”。已解决区只保留
对后续设计、运行或排障有复用价值的难点；简单修复由代码、测试和版本历史记录。

字段按需使用，不要求为了填满格式而重复信息：

```markdown
### 简短标题

- **问题**：
- **方案**：待解决问题写拟议方案，已解决问题改为“解决”并记录关键决策。
- **验证**：
- **风险**：
```

## 当前结论

当前没有开放 P0；以下问题按优先级继续处理：

| 优先级 | 问题 |
| --- | --- |
| P1 | 终局报告将绝对 objective 改善门槛误写为百分比 |
| P2 | Agent 执行阶段缺少心跳事件，长时间停滞无法及时诊断 |

## 一、待解决问题

### P1：推荐解决

#### 终局报告将绝对 objective 改善门槛误写为百分比

- **问题**：`active-etf-sharpe` Run 005 的固定 Decision 正确按
  `candidate_sortino >= champion_sortino + 0.03` 执行绝对 objective 点数改善，但终局
  `report.md` 将 `minimum_improvement=0.03` 写成“相对改善阈值 3%”。Round 001 的 Sortino
  从 `7.757116` 升至 `7.958143`，绝对改善 `0.201027` 因而正确晋级，百分比改善却只约
  `2.59%`；报告因此出现“+ 约 2.6%”与“已过 3% 门槛”的内部矛盾。Promotion、Champion
  和冻结指标未受影响，但报告误述了晋级契约，会误导人工复盘和下游摘要。
- **方案**：报告输入与模板显式将 `minimum_improvement` 标注为“objective 绝对点数”，并为每轮
  预计算 `required_candidate_objective = champion + minimum_improvement` 与绝对差值；禁止报告模型
  仅根据小数形式自行推断百分比语义。如未来需要真正的比例改善，应在版本化契约中增加独立 mode，
  不得复用现有字段并改变历史语义。
- **验证**：覆盖高基数 objective（如 `7.757 -> 7.958`）、低基数、负值和无 Champion 的报告事实；
  生成报告必须与 `runner.py` 的绝对加法判定一致，且不出现未由契约明确定义的 `%` 表述。
- **风险**：修复终局文案不应回写或重判历史 Decision；历史 Run 005 报告应保持不可变，需在
  新报告或外部复盘中明确更正。

### P2：有时间时优化

#### Agent 执行阶段缺少心跳事件，长时间停滞无法及时诊断

- **问题**：`active-etf-sharpe` Run 004 的诊断摘要在 Round 003 和 004 分别记录了约 `901s` 的
  `long_event_gap`，均发生在 `agent_started` 与首个 `round_time_warning` 之间；Round 001、002
  也分别出现约 `457s`、`362s` 的同类空窗。Harness 只看到 Agent 进程仍存活，无法让外部监督器及时
  区分正常推理、工具调用卡死、Provider 停滞或事件转发故障。现有 15/5 分钟剩余时间警告只能在固定
  时点打破静默，诊断价值过晚；该问题不影响本 Run 的固定评测、Promotion 或最终 Champion。
- **补充证据**：Run 005 在保留的失败会话中再次出现同类空窗：Round 003、006、010 的
  `opencode-events*.jsonl` 最大相邻事件间隔分别约 `545s`、`600s`、`457s`，三轮最终均因
  `Candidate research deadline exceeded` 失败。Round 006 的 `600s` 空窗对应 Agent 内部长时间
  Development proxy 命令；单靠当前事件流，外部监督器在命令返回前无法区分“计算中”与“已卡死”。
- **方案**：Agent 运行期间由 Harness 发出不含模型内容、工具参数、Development 指标或 Gate 信息的
  周期性心跳，至少包含 Run/Round、单调递增时间、已用时和进程存活状态；若 Agent runtime 能提供安全
  的活动序号，可附加最后活动距今时长，但不得把候选会话内容写入后续 Prompt 或耐久成功工件。心跳与
  现有超时和终止逻辑解耦，只增强观测，不自行判定候选失败。
- **验证**：用静默 fake agent、持续输出 agent、工具调用挂起和正常超时分别覆盖：长会话在配置心跳
  间隔内持续产生事件；心跳不延长 deadline、不改变预算、Decision 或恢复语义；诊断摘要可区分
  “进程存活但无可见活动”和“心跳自身中断”；关闭诊断保留后仍只产生既有临时运行事件，不新增永久
  成功会话轨迹。
- **风险**：仅有进程心跳不能证明模型或 Provider 正在取得进展，过密事件还会制造噪声；因此应采用
  低频、可配置的保守信号，并继续由硬 deadline 负责停止条件。

## 二、已解决问题

本节只保留会影响信任边界、评测语义、恢复一致性或长时间运行可靠性的经验。一次性的字段遗漏、
Prompt 表述和其他低风险修补由代码、测试及版本历史承载，不再逐条记录。

### P1：固定评测基线可靠性

#### Run 和 Round 分配前验证 Parent 固定测试

- **问题**：Harness 曾在分配耐久 Run 和研究轮次前跳过 Parent/Champion 的 `commands.test`，使固定
  基线故障被误记为候选失败并消耗连续失败预算。
- **解决**：使用不可变 Champion worktree 和真实 Evaluation runtime 执行 Parent 固定测试。成功缓存
  键包含 Champion SHA、稳定测试命令契约 SHA、Evaluator 契约 SHA、Evaluation runtime 输入 SHA 和
  Evaluation environment SHA，任一变化都会重测，失败永不缓存。新 Run 前失败写入任务级
  `preflight-failures/` 并在 Run 分配前熔断；活动 Run 在下一 Round 分配前失败则以
  `infrastructure_failure` 停止且不改已有计数。Candidate 测试失败时固定执行一次同环境 Parent A/B
  对照：Parent 通过仍归为候选回归，Parent 同样失败、超时或不可执行则改判基础设施故障并保留两份日志。
- **风险**：首次预检和适用性变化会增加一次固定测试成本；失败路径的 A/B 对照也会额外执行一次测试，
  这是避免静态依赖闭包误判的确定性成本。

### P1：曾影响终局复盘完整性

#### 失败 Round 必须保留 Agent 事件

- **问题**：Agent 正常退出但返回 `status=blocked` 时，Runner 会在识别失败前删除
  `opencode-events.jsonl`；终局压缩还会因旧 `agent-output.json` 存在而再次删除失败事件，使失败轮缺少
  工具调用、预算和 checkpoint 处理证据。
- **解决**：Agent 事件现在只在 Round 最终写入 `status=completed` 的 `result.json` 后清理。blocked、
  无效输出、进程失败、deadline 无可恢复候选、越界或无代码变化、测试失败以及 Development/Gate
  失败均保留现有机制脱敏后的主事件和重试事件；checkpoint 超时恢复后最终成功仍清理冗余事件。
  `compact_artifacts()` 继续删除旧 `agent-output.json` 并向失败结果补充可用摘要，但只按最终
  `result.status == completed` 删除 Agent 事件。
- **风险**：失败会话可能较大并含 Provider 输出，因此继续沿用认证信息脱敏和失败工件保留策略。已经
  丢失的历史 Run 003 Round 002 事件无法可靠回填，本修复不伪造或重建该日志。

#### 终局报告必须从 Parent—Patch—Champion 事实链核验机制演进

- **问题**：仅依赖 Agent 叙述，容易把已拒绝机制误认为 Parent 既有机制，或把组合修改错误归因为
  单一机制。
- **解决**：Harness 生成写一次的 `report-facts.json`，从最终 Champion 和每轮 Patch 重建
  Parent/Candidate，核对源码与哈希，并用 AST 保守描述结构变化。事实链优先于叙述；组合或不透明修改
  不作单机制归因，代码变化也不作为指标改善的因果证明。
- **风险**：结构化差异刻意保守，可能把相关修改归为组合变化，但不会把未经证据支持的因果解释写成
  事实。该机制不改变评测、Gate、Promotion 或停止条件。

#### Round 内被放弃的 Development 尝试必须进入研究记忆

- **问题**：轮末自由文本通常只描述最终候选，已实际评估但放弃的方向会丢失，后续研究可能重复试错。
- **解决**：允许同轮调整研究方向。Agent 先冻结 checkpoint，再通过 Harness-owned Attempt
  接口运行 Development；Harness 按候选哈希去重并冻结假设、指标和时间，以最终提交哈希标记
  `submitted`/`abandoned`。`learning` 只承担解释，不替代 Harness-owned 事实。
- **风险**：Agent 仍可给出错误因果解释，因此 learning 仅作为研究叙述；Harness-owned 指标和
  Candidate 哈希才是审计事实。底层参数组合与 folds 不作为独立 Attempt，避免低质量信息膨胀。

### P0：曾阻断可信研发

#### Walk-forward 历史折边界不得依赖实时交易日历

- **问题**：固定 evaluator 曾在 `calendar_month` 和 `iso_week` 边界判断中实时调用 AkShare，并在
  Provider 异常时静默改用输入表相邻日期；相同源码、任务和冻结行情会随网络状态得到不同 folds、
  参数日期与预算投影。
- **解决**：排序、归一化并去重后的冻结候选池行情日期并集是 Research 历史边界的唯一权威输入。
  周期 `start` 取本地序列中每个周期的首日；周期 `end` 仅在已有后续周期日期可证明时取上一周期末日。
  生产推荐仍可用交易所日历构造未来候选日期，但运行时日历不再参与历史月/周边界判断。任务已显式将
  `src/quant_core/schedule.py` 纳入 evaluator 契约，因此本次源码变化会使旧 Champion 指标自动 stale；
  历史 Run 保持不可变。
- **风险**：所有 ETF 同时缺失的日期按回测实际可交易语义视为不可交易日。该定义可能不同于交易所
  官方开市日，但可由已冻结的评测输入完整重放，不允许再用实时 Provider 隐式补齐。

#### Development 数据视图必须 fail closed 并冻结到审计契约

- **问题**：旧 Development runtime 会跳过未知格式、解析失败或缺少 `date` 的文件，并保留符号链接；
  合成容器哨兵也不能证明真实行情视图、Run 和 Attempt 使用了同一数据边界。
- **解决**：完整 Evaluation runtime 保持宿主专用；候选输入由严格解析的 CSV/Parquet 生成到
  `.cache/runtime/development-views/<sha256>/`。规范 manifest 冻结终点、schema、行数、日期范围、
  文件大小与哈希，视图复用前重新验证，损坏缓存以临时目录原子重建。Champion applicability、Run 根
  `development-inputs.json`、Run state、Attempt 和 `result.json` 共同冻结视图哈希与终点；旧完成
  Run 保持可读，缺少该契约的旧活动 Run 以基础设施不兼容终止。
- **风险**：`data/` 与 `outputs/factors/` 现在是纯时间表契约；合法非时间辅助文件必须迁移到
  `universes/`、任务配置或其他显式目录。视图是可重建缓存，由既有清理流程回收。

#### 首个新增策略必须生成可重放的 Candidate Patch

- **问题**：`baseline.mode = "none"` 的首轮策略是 untracked 文件，旧
  `git diff` 不会记录它，导致候选虽能晋级，耐久 Patch 却无法重放。
- **解决**：使用隔离临时 index 生成包含新增文件的 binary patch；Decision 和晋级前，在冻结 Parent
  上重放并逐字节核对候选及 submission 哈希。首次晋级另存内容寻址的 `candidate.py` 冗余证据。
- **风险**：历史 Run 003 Round 001 的空 patch 无法追溯修补；当前任务级 Champion 仍存在时应另行
  冻结核对。新 Round 已具备可重放 patch 和首次晋级冗余源码。

#### 宿主固定评测环境必须进入 Champion 指标适用性契约

- **问题**：若 applicability 不含 Python、ABI、平台和依赖，不同宿主环境产生的指标可能被直接比较。
- **解决**：入口在状态变更前校验固定环境，并生成内容寻址、稳定且脱敏的环境 manifest；所有评测
  与报告引用其哈希。环境变化使旧指标 stale 并触发重评，活动 Run 禁止跨环境恢复。
- **风险**：`quant` 内包升级会主动使指标 stale 并触发重评；当前未增加跨平台 Conda lockfile，
  但每次实际评测环境均有不可歧义的耐久清单，不能静默复用异环境指标。

#### 候选 Agent 曾可通过 Bash 访问 Research Root

- **问题**：worktree 和工具级目录限制无法阻止 Bash、Python 或符号链接访问宿主绝对路径，存在
  Gate 泄漏。
- **解决**：候选只在一次性容器中运行，仅挂载候选 worktree 和只读 Development 输入；Research
  Root、Gate runtime 和主工作区不挂载。Docker 不可用时失败，不回退宿主机。
- **风险**：不防御 Docker daemon、宿主内核或容器运行时本身被攻破；OpenCode 认证文件仍作为
  必要输入进入一次性 runtime home。

#### 搜索预算和耐久证据采用不同契约

- **问题**：Prompt 无法强制限制尝试次数；永久保留全部搜索轨迹又会放大工件，却不增强晋级证据。
- **解决**：以单调时钟强制单轮时间预算；截止后返回的成功不得进入评测。耐久证据只保留冻结输入、
  顶层 Attempt 最小事实、最终 diff、固定评测指标、Decision、Parent 和时长，内部参数与 fold 搜索
  轨迹保持 disposable。
- **风险**：不防御 Agent 主动通过 `setsid`、容器逃逸或其他系统权限规避进程组。若改为对抗型
  Agent，应升级系统沙箱，而不是恢复次数限制或宣称可核验全部内部探索。

#### Gate 可行性优先于目标改善和 Development 表现

- **问题**：要求不可行 Champion 的替代者先改善目标会拒绝首个可行解；用 Development 改善替代
  Gate 证据则会接受隐藏区间违反约束的候选。
- **解决**：候选必须先通过全部 Gate 硬约束。Champion 不可行时，首个目标有限且满足约束的
  候选直接替换；Champion 可行后才要求相对目标改善。精确 Gate 指标不得反馈给后续研发轮次。
- **风险**：Gate 通过/失败仍形成弱反馈；最终结果需要独立验证区间或前向观察。

### P1：重要可靠性问题

#### Agent 顶层 Development 评测必须经过 Harness-owned Attempt

- **问题**：候选曾可直接导入或通过模块 CLI 调用 `evaluate_walk_forward()`，以
  `execution=None` 或伪造 execution 绕过 checkpoint、Attempt 去重、预算投影、finalization
  预留和研究记忆；外层 Round 时限仍生效，但成功完成的顶层搜索不会形成 Attempt 审计事实。
- **解决**：解释器内的 marker 判断不足以作为权限边界，因为候选可修改模块全局变量。Harness
  仍为 Agent 容器挂载只读身份文件，但真正的执行边界改为 OS 级模块覆盖：宿主临时目录中的受限
  evaluator 门面只读挂载到候选 `src/quant_core/research/evaluator.py`，并用空只读目录遮蔽其
  `__pycache__`。门面只转发 `evaluate_candidate()` 和 selection 校验，walk-forward 函数与 CLI
  固定抛出 `HarnessExecutionRequired`；权威实现不进入候选解释器。挂载源位于 worktree 外，
  容器继续丢弃全部 capabilities 并启用 `no-new-privileges`。官方 receiver 在宿主执行冻结评测。
- **风险**：边界只保护项目权威的顶层 walk-forward evaluator。候选仍可使用 pandas、自写轻量
  Development 诊断和 `evaluate_candidate()` 等低层纯函数；在冻结 Development 数据边界内，这是
  保留的协作研究能力，不构成 Harness-owned 顶层 Attempt。

#### Evaluator 契约必须显式声明固定评测输入

- **问题**：哈希整个工作树会让无关文档变化使 Champion 指标 stale；清单过窄又会漏掉真实评测依赖。
- **解决**：任务必须通过 `evaluation.contract.paths` 显式声明固定测试、回测、配置及其导入或读取
  的仓库输入。Harness 用临时 index 生成 canonical manifest；路径校验拒绝 editable 策略、runtime、
  重复、重叠、越界、缺失和空输入，并用静态导入审计补强 Python 间接依赖检查。
- **风险**：任务使用非 Python 动态读取的新资源时，维护者仍须显式扩充清单；缺漏不会由静态导入
  审计发现，因此任务评测命令的资源变更必须同步评审 `evaluation.contract.paths`。

#### Champion 指标值与适用性必须独立于 disposable runtime

- **问题**：若把 disposable runtime 缓存缺失等同于指标失效，正常清理会抹掉耐久 Champion 指标，
  还可能错误启动新 Round。
- **解决**：`champion.json` 升级为 schema v5，用 `champion_metrics_record` 同时保存指标值、评测来源、
  valid/stale 状态及策略、数据、环境和 evaluator 指纹。清理只影响缓存；适用性变化保留历史值但标记
  stale。晋级与目标停止只读取匹配的 valid 指标，pending promotion 原子切换策略和指标记录。
- **风险**：旧 schema 缺少完整适用性证据，升级后的首次晋级比较必须重评 Champion；这是有意的保守
  行为，不能通过当前 runtime 反推旧指标有效。

#### Round 硬时限不能丢弃截止前已冻结的候选

- **问题**：Agent 在截止前可能已有可运行候选，但尚未来得及返回最终 JSON；直接判超时会丢失有效工作。
- **解决**：增加 Harness-owned checkpoint 协议。Agent 用显式命令提交策略和完整候选说明，Harness 在
  截止前校验并冻结到 Agent 不可写目录。正常完成以最终提交为准；超时才恢复最近有效 checkpoint 并
  执行完整固定评测。基础设施失败不恢复，正式测试失败也不回退更早版本。
- **风险**：checkpoint 接收使用协作型 Agent 信任模型；冻结边界防止确认后的版本被回写，但不防御
  主动攻击 Harness 控制协议的恶意候选。正式评测仍不计入 Agent 研发时限，并受原有命令超时约束。

#### Run 边界及状态—工件一致性必须由 Harness 强制

- **问题**：Run 间工件混用、无工件的“幽灵 Round”或编号复用都会使状态、计数和报告互相矛盾。
- **解决**：Champion 保持任务级，Round、状态和报告按 Run 隔离；恢复未完成 Round 时补写失败工件，
  编号同时参考状态和物理目录。Decision 前校验计数与 ID，报告前确定性检查必需工件；异常只记录
  `integrity_warnings`，禁止模型补全事实。
- **风险**：完整性检查只能暴露既有损坏，不能自动推断或修复缺失历史；失败是否占预算仍由
  “中断或基础设施失败仍会消耗研发轮次”跟踪。
