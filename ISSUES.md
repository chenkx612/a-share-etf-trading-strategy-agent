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
| P2 | Walk-forward 缺少候选与 Champion 的行为差异摘要 |
| P2 | 仍依赖 Prompt 阻止加载无关 Skill |
| P2 | 中断或基础设施失败仍会消耗研发轮次 |
| P2 | Harness 风险登记文件名在仓库说明中不一致 |
| P2 | Evaluator 指纹计算依赖可写 Git 对象库 |
| P2 | Stale Champion 重评缺少阶段事件 |
| P2 | 报告解析因标题前说明文字误判有效 Markdown |

## 一、待解决问题

### P2：有时间时优化

#### Walk-forward 缺少候选与 Champion 的行为差异摘要

- **问题**：Walk-forward 已由 Harness 固定滚动窗口并记录逐折 OOS 指标，配合独立 Gate、硬约束和
  相对改善门槛，已覆盖原“单一 Development 汇总指标”问题的主要稳健性风险。当前 Decision 仍只
  展示汇总指标差异，不能辅助判断候选与 Champion 是否改变了信号日、订单、持仓或总暴露；相同汇总
  指标本身也不足以证明两者行为等价。这影响研究解释和排障效率，但不影响现有晋级规则的正确性。
- **方案**：有实际诊断需求时，在不改变晋级语义的前提下增加简洁的行为差异摘要；暂不把最低触发
  次数或跨折一致性设为硬门槛。若后续运行出现汇总指标掩盖明显逐折失真的具体证据，再单独设计并验证
  稳健性门槛。
- **验证**：构造订单完全相同、仅部分信号日不同以及汇总指标相同但持仓不同的候选，确认摘要能正确
  分类且不会影响原有接受或拒绝结果。
- **风险**：低。优化前可直接查看已保存的逐折指标、订单和持仓产物进行人工诊断。

#### 仍依赖 Prompt 阻止加载无关 Skill

- **问题**：策略使用 Skill 目录中的固定输入，可能触发完整的 ETF 发现、选池、刷新或推荐工作流；
  修改范围检查虽能阻止业务越界，额外上下文仍会分散推理并增加 token 消耗。
- **方案**：当前由 Prompt 声明输入固定并禁止相关工作流；若 OpenCode 支持能力级配置，改用 Skill
  allowlist 或完全关闭 Skill。
- **验证**：事件日志中不再出现无关 Skill 加载，固定输入仍能正常读取。

#### 中断或基础设施失败仍会消耗研发轮次

- **问题**：未提交候选的中断或基础设施失败会被记录为 failed 并计入 `max_rounds`，减少真实候选
  预算；系统又无法自动证明中断候选完整或安全恢复模型会话。
- **方案**：增加显式 `research loop reset-current` 或“诊断轮不计预算”模式。命令必须验证候选
  未提交、Champion 未变化，并保留独立审计记录。
- **验证**：只有满足安全前置条件的诊断失败可以释放研究预算；所有历史事件和失败原因仍可追溯。
- **风险**：修复前必须保留失败记录并继续使用同一任务根；直接删除 Round 目录或编辑状态文件会破坏
  恢复语义。

#### Harness 风险登记文件名在仓库说明中不一致

- **问题**：实际登记表和 README 使用 `ISSUES.md`，但 `AGENTS.md` 的项目结构及启动规则仍写作
  `HARNESS_ISSUES.md`。按说明执行启动前 P0 检查时会先遇到文件不存在，增加漏检或误建第二份登记表
  的风险。
- **方案**：确定 `ISSUES.md` 为唯一 canonical 名称后，统一仓库内全部引用，并增加轻量文档链接
  检查避免再次漂移；不要创建内容重复的第二份风险文档。
- **验证**：仓库搜索不再出现旧文件名，README 链接和 Agent 启动前检查均指向同一现有文件。
- **风险**：低；修复前操作人员应以现有 `ISSUES.md` 为准。

#### Evaluator 指纹计算依赖可写 Git 对象库

- **问题**：`evaluator_contract_sha256()` 虽用临时 `GIT_INDEX_FILE` 隔离 index，仍执行
  `git add -A` 和 `git write-tree`，需要向仓库 Git 对象库写入 blob/tree。在仅允许读取
  `.git`、但允许写工作区的受管执行环境中，Loop 会在分配 Run 前以
  `unable to create temporary file: Operation not permitted` 失败；本次在扫描
  `.agents/skills/etf-sharpe-topk/SKILL.md` 时复现。
- **方案**：将临时 object database 一并放入可写临时目录，并通过
  `GIT_OBJECT_DIRECTORY`/alternate object directories 只读复用原仓库对象；或改为不写 Git 对象库
  的确定性工作树内容哈希。两种方案都必须保持未提交 evaluator 改动可进入契约、排除规则不变，
  且不能污染主仓库 index/object database。
- **验证**：在主 `.git` 只读、工作区可写的测试夹具中计算契约哈希；覆盖 tracked 修改、未跟踪文件、
  删除、排除目录及相同内容的稳定哈希，并确认主仓库 index 和对象库均无变化。
- **风险**：低。当前可在明确授权后于具有 Git 对象库写权限的宿主环境启动，不影响评测语义；
  但未授权提升权限时无法运行 Harness。

#### Stale Champion 重评缺少阶段事件

- **问题**：Run 004 Round 001 的候选 Development 和 Gate 均已完成并发出事件后，Harness 因
  `evaluator_contract_sha256_changed` 在隔离 evaluator worktree 中重新评测现有 Champion。
  Champion Development/Gate 重评期间 stdout 与
  `.research/<task-id>/.tmp/runs/<run>/events.jsonl` 均没有开始、完成或进度事件，外部监督者只能通过
  临时 evaluator 输出目录反推进度；本次形成数分钟无可观测状态，容易被误判为 Gate 后卡死。
- **方案**：在 `_evaluate_existing()` 调用边界为 Champion 重评增加
  `champion_reevaluation_started`、逐阶段 started/completed、failed 和最终 completed 事件，并包含
  stale 原因但不输出精确 Gate 指标。若固定 evaluator 能提供安全的进度文件，可只转发折号、总折数和
  elapsed 等非 Gate 内容；事件仍写入现有活动 Run 临时流，不新增永久成功轨迹。
- **验证**：构造 evaluator 契约、数据指纹或评测环境变化使 Champion 指标 stale，确认候选 Gate
  完成后到 Decision 落盘前持续存在可审计阶段事件；覆盖重评成功、命令失败和中断恢复，且事件不得向
  后续候选泄露 Champion 或候选的精确 Gate 指标。
- **风险**：低。缺口不改变固定评测、约束或晋级结果，但降低长时间运行的外部可观测性和故障定位效率。

#### 报告解析因标题前说明文字误判有效 Markdown

- **问题**：Run 004 的报告会话以退出码 0 完成，并在最终 text event 中生成了完整的
  `# Research Loop 总结` 报告，但在标题前附加一句“已掌握足够信息，生成完整复盘报告。”。
  `generate_loop_report()` 使用
  `report.lstrip().startswith("# Research Loop 总结")` 做严格前缀校验，因而把有效报告误判为
  `OpenCode report session produced no valid Markdown report`，将 Run 的 `report_status` 记为
  failed 并保留约 270 KiB 的冻结输入与完整事件。
- **方案**：保持一级标题契约，但从最后 text event 中定位首个独占行
  `# Research Loop 总结`，只在该行之前全部属于非 Markdown 前言时截取并保存其后的报告；仍拒绝缺少
  标题、标题嵌在代码块/JSON 字符串、多个冲突标题或标题后为空的输出。Prompt 同时明确禁止标题前
  前言，但不能只依赖 Prompt 保证解析可靠性。
- **验证**：覆盖纯报告、前置空白、单句前言、代码块内伪标题、缺失标题、多个标题及标题后空内容；
  确认成功时删除临时事件，失败时保留冻结输入和事件供相同输入重试，且报告重试不改变 Loop Decision
  或 Champion。
- **风险**：低。问题不影响研究评测和晋级，但会让成功 Run 缺少终局报告，并产生不必要的人工重试。

## 二、已解决问题

本节只保留会影响信任边界、评测语义、恢复一致性或长时间运行可靠性的经验。一次性的字段遗漏、
Prompt 表述和其他低风险修补由代码、测试及版本历史承载，不再逐条记录。

### P1：曾影响终局复盘完整性

#### 终局报告大型输入不得通过 argv 传递

- **问题**：逐折 Development/Gate 记录令 Run 003 的冻结 `report-input.json` 达到 258,525
  字节；旧实现把完整 Prompt 作为 `opencode run` 的单个位置参数嵌入 `docker run`，容器 init
  因参数过长退出 255，且 Loop state 没有记录结构化失败类型。
- **解决**：完整冻结输入改由 OpenCode `--file /workspace/report-input.json` 附加，并在报告
  容器中对该文件使用只读覆盖挂载；argv 只保留有 64 KiB 启动前预算的固定报告指令和小型 Champion
  指标适用性覆盖。`E2BIG` 与 `argument list too long` 统一分类为
  `infrastructure/invocation_argument_too_long`，报告重试仍逐字节复用原冻结输入。
- **验证**：回归测试使用超过宿主 `ARG_MAX`、包含空格和 Unicode 的输入，确认完整字节只进入附件、
  不进入任何 argv 元素；同时覆盖只读挂载、冻结输入重试和 Loop state 失败分类。
- **风险**：附件大小不再受操作系统 argv 限制，但仍可能在未来触及模型上下文上限；若出现实际证据，
  应另行设计确定性摘要工件，不能静默截断审计输入。

### P0：曾阻断可信研发

#### 首个新增策略必须生成可重放的 Candidate Patch

- **问题**：`baseline.mode = "none"` 的首轮策略是 untracked 文件，旧
  `git diff --binary HEAD -- <editable>` 不会记录它。候选可直接复制为 Champion，但耐久
  `candidate.patch` 为空，未来覆盖 Champion 后无法从轮次证据重建首次获胜源码。
- **解决**：Harness 使用隔离的临时 index 对新增策略执行 intent-to-add，并以 Git 原始字节生成
  binary patch，不修改候选真实 index。写 Decision 和晋级前，在另一个临时 index 上将 patch 应用到
  冻结 Parent，逐字节核对重建策略、候选策略及 `submission.strategy_sha256`；失败统一记录为
  `infrastructure/candidate_patch_integrity_failed` 并熔断。首次 0→1 晋级还原子保存
  `candidate.py` 作为冗余内容寻址证据。
- **验证**：回归测试覆盖新增文件、空文件、已有文件修改、文件删除、二进制新增/修改、真实空改动、
  submission 哈希不符及错误空 patch；每个有效 patch 均在干净 Parent 上重放，并确认候选 index
  未变化。集成测试覆盖首次晋级源码保全、后续普通修改以及完整性故障不改变 Champion；Conda
  `quant` 全量测试 `240 passed, 3 skipped`。
- **风险**：历史 Run 003 Round 001 的空 patch 无法追溯修补；当前任务级 Champion 仍存在时应另行
  冻结核对。新 Round 已具备可重放 patch 和首次晋级冗余源码。

#### 宿主固定评测环境必须进入 Champion 指标适用性契约

- **问题**：Harness 控制器、固定测试以及 Development/Gate 评测使用启动 Loop 的宿主解释器，
  旧 applicability 却不包含 Python 和依赖环境。不同宿主环境评出的候选可能直接与缓存的旧 Champion
  指标比较，破坏跨 Run 的确定性和晋级正确性。
- **解决**：Research 入口强制使用 Conda `quant`，在任何 Run 状态变更、Docker 或 Provider 调用前
  校验环境。Harness 对 Python/ABI/平台、Conda package build 和 Python distribution 生成 canonical
  manifest，按 SHA-256 内容寻址保存到任务级 `environments/`。Champion schema v6 的 applicability、
  Loop state v3、Round Result、独立 Test 和报告输入均记录该哈希；环境变化保留旧指标但标记 stale，
  晋级前在当前环境重评。活动 Run 缺少或改变环境证据时以
  `infrastructure/evaluation_environment_changed` 审计化停止，不跨环境恢复。
- **验证**：测试覆盖错误 Conda 环境在分配 Run 前失败、manifest 稳定排序与脱敏、环境 registry
  完整性、旧 schema 迁移、环境变化 stale、同环境缓存有效、跨环境恢复熔断、成功及失败 Result 和
  Test/Report 环境引用；Conda `quant` 全量测试 `224 passed, 2 skipped`。
- **风险**：`quant` 内包升级会主动使指标 stale 并触发重评；当前未增加跨平台 Conda lockfile，
  但每次实际评测环境均有不可歧义的耐久清单，不能静默复用异环境指标。

#### Provider 刷新令牌失效必须持久化并按基础设施错误熔断

- **问题**：隔离会话只复制宿主认证快照，轮换后的 refresh token 随一次性 runtime home 删除；后续
  Round 和报告继续使用已撤销 token。认证错误又未被基础设施分类器识别，导致无效重试耗尽预算。
- **解决**：按认证文件串行化 OpenCode 会话；固定提示、`--pure` 且全部工具禁用的宿主 OpenCode
  预检负责在分配 Run 或 Round 前验证认证。候选和报告使用完整 OAuth 状态的一次性副本；会话退出后
  只提取结构合法的目标 Provider 变化，以可信宿主探针复核后原子合并回宿主文件。恢复覆盖成功、失败、
  超时和中断路径；`invalid_grant`、凭据损坏及并发冲突统一写入结构化 `infrastructure` 故障并立即熔断。
  报告输入单独冻结，认证恢复后可重试。
- **验证**：假 Provider 覆盖完整凭据暂存、二次验证轮换、Provider 范围原子回写、超时和中断恢复、
  失败探针的可信轮换保全、容器未确认停止时拒绝恢复、非法状态拒绝、并发冲突、权限、错误脱敏、
  预检不分配 Run/下一 Round 及报告冻结输入重试；真实验收连续运行三次容器会话并在每次之后复查
  宿主认证。
- **风险**：同一认证文件上的 Harness 会话会串行，锁等待计入会话硬时限；外部 OpenCode 进程不遵守
  Harness 锁时无法完全消除提交竞态，Harness 会检测已知同 Provider 冲突并拒绝覆盖。完整 refresh
  token 会进入一次性容器；这符合当前协作型 Agent 威胁模型，但不防御主动读取或替换凭据的恶意候选。

#### 容器运行时故障必须在 Run 前预检并按基础设施错误熔断

- **问题**：嵌套 bind mount、缺失模型缓存和 CLI 调用契约变化曾使 Agent 在启动前连续失败，
  却耗尽多个研究轮次。
- **解决**：将认证、配置和模型目录复制到权限为 `0600` 的一次性 runtime home，并只挂载一次；
  分配 Run 前用真实镜像验证写入边界、只读数据、Research Root 遮蔽、runtime 文件和模型可用性。
  OCI、挂载、镜像及清理错误统一标记为 `infrastructure`，运行中首次出现即熔断。
- **验证**：真实容器会话及隔离边界测试通过。不能把容器初始化失败当普通候选失败重试；预检
  应发生在创建耐久 Run 状态之前。

#### 候选 Agent 曾可通过 Bash 访问 Research Root

- **问题**：候选 Agent 位于隔离 worktree，但通过 Bash 使用主仓库绝对路径仍可枚举 Research
  Root；工具级目录限制不能约束 Bash、Python、符号链接等子进程，存在 Gate 泄漏。
- **解决**：Agent 只在一次性容器中运行，仅挂载候选 worktree 和只读 Development 输入；
  Research Root、Gate runtime 和主工作区不挂载。Docker 不可用时直接失败，不回退宿主机。
- **验证**：真实容器测试覆盖 Bash、Python、绝对路径和符号链接，确认候选只能写自身目录。
- **风险**：不防御 Docker daemon、宿主内核或容器运行时本身被攻破；OpenCode 认证文件仍作为
  必要输入进入一次性 runtime home。

#### 搜索预算和耐久证据采用不同契约

- **问题**：依赖 Prompt 限制实现或参数尝试次数无法强制核验；反过来永久保留全部中间搜索
  轨迹又会显著增加工件体积，却不增强最终晋级证据。
- **解决**：Harness 采用协作型 Agent 信任模型：信任 Agent 不会主动攻击或逃逸系统边界，但
  防范超时、崩溃、错误输出和普通残留子进程。单轮时间是搜索硬限制；耐久证据只保留冻结输入、最终
  diff、固定 Development/Gate 指标、Decision、Parent Champion 和 Round 时间。
  `budget.round_minutes` 由单调时钟强制执行；超时终止进程组并记录
  `result.json.round_timing`，截止后返回的成功也不得进入评测。成功轮可压缩重复日志，失败原因仍
  结构化保留。
- **风险**：不防御 Agent 主动通过 `setsid`、容器逃逸或其他系统权限规避进程组。若改为对抗型
  Agent，应升级系统沙箱，而不是恢复次数限制或宣称可核验全部内部探索。

#### 候选 Worktree 导入主工作区代码

- **问题**：editable install 曾使正式评测导入主工作区代码，把已经修改的候选当作基准策略
  评估，直接破坏晋级正确性。
- **解决**：所有 Agent、测试和回测子进程都将候选 worktree 的 `src/` 放在 `PYTHONPATH`
  首位；仅切换 `cwd` 不能覆盖 editable install 的导入映射。
- **验证**：真实子进程断言策略模块 `__file__` 位于候选路径；单元测试覆盖搜索路径顺序。

#### Gate 可行性优先于目标改善和 Development 表现

- **问题**：若要求不可行 Champion 的替代者先改善目标，会拒绝首个可行解；若把 Development
  改善视为 Gate 的替代证据，又会接受在隐藏区间违反硬约束的候选。
- **解决**：候选必须先通过全部 Gate 硬约束。Champion 不可行时，首个目标有限且满足约束的
  候选直接替换；Champion 可行后才要求相对目标改善。精确 Gate 指标不得反馈给后续研发轮次。
- **验证**：真实 Run 中只有满足全部 Gate 约束的候选被提升，拒绝候选未污染 Champion。
- **风险**：Gate 通过/失败仍形成弱反馈；最终结果需要独立验证区间或前向观察。

### P1：重要可靠性问题

#### Evaluator 契约必须显式声明固定评测输入

- **问题**：旧 `evaluator_contract_sha256()` 对除少数排除路径外的整个工作树执行 `git add -A`，
  使 `ISSUES.md` 等无关文档变化也会将 Champion 指标标记 stale，并触发昂贵的 Development/Gate
  重评。
- **解决**：任务必须通过 `evaluation.contract.paths` 显式声明固定测试、回测、配置及其导入或读取
  的仓库输入。Harness 只用临时 Git index 哈希这些字面路径，覆盖 tracked 修改、删除和非 ignored
  的未跟踪依赖，不修改用户 index；工作区与候选提交使用同一 canonical manifest。路径校验拒绝
  editable 策略、运行时目录、重复、重叠、越界、缺失和空输入；两个现有任务均声明完整清单，并用
  静态 Python 导入审计防止漏掉仓库内间接依赖。
- **验证**：测试覆盖 README、`ISSUES.md` 和无关 Skill 变化哈希稳定且不触发 Champion 重评，
  evaluator tracked 修改和未跟踪依赖变化哈希改变并重评，工作区/提交哈希一致、用户 index 不变及
  非法清单提前失败；Conda `quant` 全量测试 `266 passed, 3 skipped`。
- **风险**：任务使用非 Python 动态读取的新资源时，维护者仍须显式扩充清单；缺漏不会由静态导入
  审计发现，因此任务评测命令的资源变更必须同步评审 `evaluation.contract.paths`。

#### Agent 单次 Shell 时限与 Round 评测预算不一致

- **问题**：候选 Round 有 30 分钟研发预算，但 OpenCode Bash 工具曾以未声明的 600 秒时限
  截断完整 Development walk-forward；`max_parameter_sets` 只限制结构大小，不能代表实际可完成的
  搜索预算。
- **解决**：研究镜像固定升级到 OpenCode 1.18.3；Harness 启动候选容器时显式设置
  `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` 为 Round 时限，并在 Prompt 中声明该契约。
  Agent 专用 Development 配置通过嵌套只读挂载读取 Harness-owned checkpoint 状态，要求当前策略
  已有真实冻结副本；随后按首尾折首组参数的耗时估算
  `folds × (parameters + 1) × 1.25`，从实时剩余 Round 时间扣除 Round 时长四分之一、最多
  300 秒的 finalization 预留后，预计超预算即提前拒绝而不裁剪网格。进度、实际耗时、剩余预算和
  终止原因原子写入对应 backtest 输出的 `progress.json`；Round 超时时 Harness 将最后进度附加到
  stage event。Harness-owned
  Development/Gate 复评仍使用不含这些研发控制项的固定 evaluator 配置。
- **验证**：测试覆盖 Docker 环境变量及 checkpoint 状态只读挂载、伪造 workspace ack 无效、Prompt
  时限声明、缺失或哈希不匹配 checkpoint、短 Round 预留、超预算提前拒绝、首尾折校准结果复用、
  完成进度和 Round 超时事件；Conda `quant` 回归测试通过。
- **风险**：耗时估算基于两个折的保守采样，极端非均匀策略仍可能在外层 Round 截止处被终止；
  `max_parameter_sets` 仍是结构上限，不能替代运行时估算。

#### Champion 指标值与适用性必须独立于 disposable runtime

- **问题**：正常 Run 清理会删除 Development runtime；旧实现把该派生缓存缺失等同于 Champion 指标
  失效，在下一次初始化及预检前把耐久指标写成 `null`。失败 Run 因而无法报告当前 Champion，配置了
  `evaluation.target` 时还会启动本不需要的 Round。
- **解决**：`champion.json` 升级为 schema v5，用 `champion_metrics_record` 同时保存指标值、评测来源、
  valid/stale 状态、Champion 哈希、任务评测键、Development/Gate 数据指纹和 evaluator 内容指纹。
  runtime 重建只重建缓存；内容不变时旧指标保持 valid，适用性变化时保留历史值并标记 stale。晋级比较
  和 `target_reached` 只读取匹配的 valid 指标；下一次成功评测再原子替换记录。旧 schema 指标迁移后
  保留为 stale，pending promotion 恢复同时切换策略和完整指标记录。报告明确区分策略来源与指标来源，
  stale 指标仅用于历史解释。
- **验证**：确定性测试覆盖正常清理重建、预检失败、分配 Round 前目标停止、固定数据与 evaluator 变化、
  schema v4 迁移、Champion 重评、接受/拒绝、晋级中断恢复以及 valid/stale 报告；完整测试通过。
- **风险**：旧 schema 缺少完整适用性证据，升级后的首次晋级比较必须重评 Champion；这是有意的保守
  行为，不能通过当前 runtime 反推旧指标有效。

#### Round 硬时限不能丢弃截止前已冻结的候选

- **问题**：`budget.round_minutes` 到期会终止 Agent；过去即使候选已可运行，只要尚未返回最终 JSON，
  Harness 也会直接判超时并删除临时 worktree。
- **解决**：增加 Harness-owned checkpoint 协议。Agent 用显式命令提交策略和完整候选说明，Harness 在
  截止前校验范围、哈希、编码和语法，并冻结到 Agent 不可写的 Round 目录。正常完成仍以最终 JSON 为准；
  超时则恢复最近的有效 checkpoint 后运行固定测试、Development 和 Gate。结果和 Decision 记录
  `submission`、checkpoint ID、策略哈希、`submitted_by_timeout` 及 candidate patch 哈希；基础设施失败
  不触发恢复，正式测试失败也不得回退到更早版本。
- **验证**：确定性测试覆盖多次提交后超时、冻结副本损坏回退、无 checkpoint、最终提交优先、截止后
  拒绝、非法源码、恢复后测试失败不回退，以及恢复候选通过 Gate 后晋级；完整测试通过。
- **风险**：checkpoint 接收使用协作型 Agent 信任模型；冻结边界防止确认后的版本被回写，但不防御
  主动攻击 Harness 控制协议的恶意候选。正式评测仍不计入 Agent 研发时限，并受原有命令超时约束。

#### 晋级后的下一轮候选 worktree 对 Docker bind 不可见

- **问题**：晋级后清理并重建 worktree 父目录，Docker Desktop daemon 偶尔仍认为下一轮
  bind source 不存在，使多轮 Loop 退化为单轮并触发基础设施熔断。
- **解决**：活动 Run 只删除子 worktree，保持父目录 inode 稳定。仅当 Docker 明确报告 bind
  source 不存在、且所有 source 在宿主机均存在时，使用新容器名安全重试一次；真实目录缺失、超时和
  其他错误均不重试。
- **验证**：测试覆盖父目录稳定性、瞬态单次重试和真实缺失不重试；真实容器连续挂载五轮通过。
- **风险**：Docker daemon 持续不可用或 bind source 在宿主机真实消失仍按
  `infrastructure_failure` 熔断；重试条件必须保证 Agent 尚未启动，避免重复产生候选副作用。

#### 恢复 Run 后 Docker bind source 不可见可持续超过一次重试

- **问题**：中断清理会删除活动 Run 的 candidates 父目录；恢复时虽然宿主已重建真实候选 worktree，
  Docker Desktop daemon 仍可能持续报告 bind source 不存在。原有固定等待 0.25 秒的单次重试会覆盖
  首次日志，并在 Agent 启动事件和 Round 分配之后才失败。
- **解决**：候选生命周期拆成暂存、真实路径 bind 探针和正式激活。探针不启动 OpenCode，以只读方式
  挂载候选路径，在固定 5 秒内最多尝试五次并采用 0.25、0.5、1、2 秒退避；每次记录独立日志、容器名
  以及探针前后的宿主 device/inode。只有 Docker 明确报告 source 不存在且路径身份稳定时才重试。
  中断清理保留活动 Run 的父目录 inode；持续不可见、宿主路径缺失或身份变化会在分配 Round 前生成
  Run 级 `preflight_failure` 和耐久诊断并按基础设施故障停止。Agent 启动阶段的最后防线也改用独立
  attempt 日志，避免覆盖首次证据。
- **验证**：确定性测试覆盖首次失败后恢复、连续五次不可见、宿主路径消失、失败不分配 Round、独立
  证据和中断清理后的 inode 稳定。真实 Docker Desktop 测试通过新探针，并覆盖中断式清理后连续五轮
  候选挂载；Conda `quant` 全量测试 `229 passed, 3 skipped`。
- **风险**：持续超过 5 秒的 daemon 可见性故障仍会停止 Run，但不会消耗候选研究轮次；固定预算避免
  Docker Desktop 故障无限阻塞 Harness。

#### Run 边界及状态—工件一致性必须由 Harness 强制

- **问题**：旧布局会在报告中混入其他 Run；早期失败还曾产生无工件的“幽灵 Round”、复用
  编号并造成完成计数与实际工件不一致。
- **解决**：
  - Champion 保持任务级，Round、状态和报告按 `runs/<run>/` 隔离。
  - 恢复未完成 Round 时补写失败工件；新编号同时参考状态保留 ID 和物理目录。
  - 写入 Decision 前校验完成数、决策数和 ID 唯一性；报告前确定性检查 Round 目录及必需 JSON，
    异常写入 `loop.integrity_warnings`，禁止报告模型自行补全事实。
- **验证**：回归测试覆盖连续 Run 隔离、缺目录恢复、编号不复用、计数一致性和损坏状态告警。
- **风险**：完整性检查只能暴露既有损坏，不能自动推断或修复缺失历史；失败是否占预算仍由
  “中断或基础设施失败仍会消耗研发轮次”跟踪。
