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

当前存在开放 P0，修复前不得启动新的 Loop 研发；以下问题按优先级继续处理：

| 优先级 | 问题 |
| --- | --- |
| P0 | Development 数据视图缺少 fail-closed 验证与契约闭环 |
| P1 | Agent 可绕过 Attempt 与 Development 预算控制直接调用 evaluator |
| P2 | 结构归因警告出现过晚，无法在 Gate 前提示拆分候选 |
| P2 | 中断或基础设施失败仍会消耗研发轮次 |
| P2 | 每轮结构归因警告错误地把拒绝候选称为已接受候选 |

## 一、待解决问题

### P0：执行新的 Loop 研发前必须解决

#### Development 数据视图缺少 fail-closed 验证与契约闭环

- **问题**：`active-etf-rerank-topk` Run 003 的 Round 006/009 事件日志证明，候选 Agent 可直接读取
  `data/etf_daily.parquet` 并在进程内调用固定 evaluator；但两轮日志同时显示该文件的最大日期为
  Development 终点 `2025-07-21`，没有证据表明候选看到了 Gate 行情或 Gate 指标。当前标准路径已经
  从完整 Evaluation runtime 派生按 Development 终点截断的视图，再复制到候选 worktree 并将
  `data/` 只读挂载，因此“候选容器已挂载完整行情缓存”和“Prompt 是唯一 Gate 边界”不是现有证据
  支持的事实。真正的缺口是该边界仍为 best-effort：表过滤会跳过无法解析、缺少 `date` 列或不受支持
  格式的文件，复制会保留符号链接，容器预检只检查合成哨兵而不核验真实可见行情的日期范围；数据视图
  的范围和哈希也尚未进入 Run/Attempt 契约。数据格式、缓存拓扑或挂载逻辑一旦回归，Harness 不能在
  Run 分配前 fail closed，也无法仅凭持久化契约证明候选只接触过冻结的 Development 输入。
- **方案**：把候选可见输入构建为内容寻址、只读且无符号链接的 Development 数据视图；视图最多保留
  Development 终点及完成首个训练折、信号窗口所需的前置历史，所有行情文件必须由显式格式契约解析
  并验证日期上界，未知格式、解析失败、缺失日期语义或越界一律按基础设施故障拒绝。完整缓存和 Gate
  评测数据只允许宿主 Harness 进程访问，不能通过 worktree、只读 bind、已安装包资源或符号链接进入
  候选容器。把视图范围、逐文件 manifest 和整体哈希纳入 Run/Attempt 契约。
- **验证**：真实容器测试覆盖 Bash、Python、pandas、pyarrow、绝对路径、符号链接、未知或损坏文件
  和直接导入 evaluator；候选可完成合法 Development 与必要训练回看，但任何可见行情的最大日期不得
  晚于 Development 终点，完整缓存与 Gate runtime 均不可见。预检应在分配 Run 前核验真实数据视图、
  manifest、挂载拓扑和日期上界，任一不一致立即按基础设施故障熔断。使用同一冻结 Champion 对照宿主
  固定 Development，折定义、可行性、参数选择和汇总指标必须一致。
- **风险**：严格格式契约必须覆盖合法的非行情辅助文件，截断视图也必须保留足够训练和特征预热历史，
  否则会拒绝正常任务或静默改变 Development 结果；内容寻址缓存还需避免重复大文件带来的空间放大。

### P1：推荐解决

#### Agent 可绕过 Attempt 与 Development 预算控制直接调用 evaluator

- **问题**：Run 003 的 Round 006/009 未提交 checkpoint，也没有 Harness-owned
  `development_attempts`，但事件日志显示两轮均多次直接导入
  `quant_core.research.evaluator.evaluate_walk_forward` 批量评测候选；Round 009 截止前一次脚本还
  完成了七组网格输出。由于 `evaluate_walk_forward(..., execution=None)` 是合法调用，这些评测
  绕过 checkpoint 校验、Attempt 去重、耗时投影、finalization 预留、进度事件和研究记忆，最终
  两个 `result.json` 仍显示空尝试并以超时失败。外层 Round 硬时限仍生效，固定 Gate/Promotion
  也未被这些脚本直接改写，但“被放弃的 Development 尝试必须进入研究记忆”的既有解决并未形成
  可执行边界。
- **方案**：在候选容器上下文中禁止无 Harness execution capability 的顶层 evaluator 调用；官方
  Attempt 由宿主 receiver 对冻结 checkpoint 执行并返回规范化结果。保留纯函数级单元诊断能力，
  但批量 walk-forward 入口必须校验不可由候选伪造的 Harness capability。Prompt 继续指导正确流程，
  但不得作为唯一约束。
- **验证**：真实 Agent 容器内直接导入、`python -m`、包装函数和省略 `execution` 均应在开始评测前
  拒绝；官方 Attempt 仍可评测、去重、写入 learning，并遵守耗时投影与 finalization 预留。超时后
  所有已成功返回给 Agent 的顶层 Development 结果都必须存在对应 Attempt 最小事实。
- **风险**：Agent 仍能用 pandas 或自写回测器做轻量探索；在 Development 数据边界已物理隔离的
  前提下，这属于协作型研究能力。Harness 至少应阻止对固定 evaluator 的偶然绕行，并明确哪些
  探索必须登记为顶层 Attempt。

### P2：有时间时优化

#### 结构归因警告出现过晚，无法在 Gate 前提示拆分候选

- **问题**：`active-etf-rerank-topk` Run 002 的 Round 002 在同一候选中移除 Parent 已晋级机制并
  新增另一机制，仍通过固定评测并晋级；Harness 直到终局生成 `report-facts.json` 时才将其标记为
  `combined_change`。评测、Promotion 和 Gate 隔离本身仍有效，但研究者已失去在 Gate 前主动拆分
  候选或补充 Development 消融的机会。
- **方案**：在 Candidate 提交后、Gate 前复用保守的 Parent—Patch 结构归因，向 Agent 发出
  非阻断提示，并允许其在本轮剩余时间内拆分或重新提交。Promotion 仍只由固定测试、硬约束和目标改善
  决定；结构归因不定义“机制”、不作为拒绝条件，选择继续的候选在终局明确标记为不可单独归因。
- **验证**：覆盖疑似组合或不透明修改会在 Gate 前产生提示、Agent 可重新提交、选择继续时 Gate 和
  Promotion 语义不变、单纯接线修改即使被保守提示也不会自动拒绝；提示不得包含或间接泄露 Gate
  信息。
- **风险**：AST 归因只能描述结构变化，不能可靠划分单机制与多机制；提示可能误报或漏报，因此只能
  帮助研究纪律，不能成为候选有效性或因果归因的证明。

#### 中断或基础设施失败仍会消耗研发轮次

- **问题**：未提交候选的中断或基础设施失败会被记录为 failed 并计入 `max_rounds`，减少真实候选
  预算；系统又无法自动证明中断候选完整或安全恢复模型会话。
- **方案**：增加显式 `research loop reset-current` 或“诊断轮不计预算”模式。命令必须验证候选
  未提交、Champion 未变化，并保留独立审计记录。
- **验证**：只有满足安全前置条件的诊断失败可以释放研究预算；所有历史事件和失败原因仍可追溯。
- **风险**：修复前必须保留失败记录并继续使用同一任务根；直接删除 Round 目录或编辑状态文件会破坏
  恢复语义。

#### 每轮结构归因警告错误地把拒绝候选称为已接受候选

- **问题**：Run 003 的 `report-facts.json` 对 Round 001、003、004、005、007、008、010 等拒绝候选
  均写入 `Accepted candidate contains multiple or opaque structural changes`。只有 Round 002
  实际接受；任务级 `integrity_warnings` 正确地只汇总了接受轮，因此未改变 Promotion 或最终
  Champion，但逐轮事实的措辞与 Decision 冲突，容易在自动报告或人工复盘时把拒绝候选误读为晋级。
- **方案**：结构分类器使用中性的 `Candidate contains ...`，或在报告组装层按 Decision 生成措辞；
  接受轮是否进入任务级 integrity warning 仍由独立逻辑决定，不能靠文案字符串推断。
- **验证**：同一 `combined_change` 分类分别覆盖 accepted、rejected 和其他合法 Decision；逐轮警告
  与 Decision 一致，只有接受候选的归因风险进入任务级汇总，历史报告重建保持确定。
- **风险**：这是报告准确性问题，不影响已冻结指标、Gate、Promotion、patch 重放或 Champion。

## 二、已解决问题

本节只保留会影响信任边界、评测语义、恢复一致性或长时间运行可靠性的经验。一次性的字段遗漏、
Prompt 表述和其他低风险修补由代码、测试及版本历史承载，不再逐条记录。

### P2：诊断可观测性

#### Champion 初评与重评缺少阶段事件

- **问题**：Champion 初评或因 evaluator/data/environment 变化而重评时，外部观察者会在候选 Gate
  后看到数分钟无事件，难以区分正常重评与卡死。
- **解决**：`_evaluate_existing()` 现在发出 Champion 重评及 Development/Gate 子阶段的 started、completed、
  failed 事件，携带 stale 原因但不携带精确 Gate 指标。`research loop --retain-diagnostics` 可将该时间线和
  确定性摘要保留到可清理缓存，供终局离线复盘。
- **验证**：Loop 与 runner 测试覆盖诊断时间线和终局摘要；事件仍经原有候选隔离边界写入，不进入后续候选 Prompt。
- **风险**：事件仅描述阶段状态；精确 Gate 指标仍只存在于冻结 Round 评测产物中。

### P1：曾影响终局复盘完整性

#### 终局报告必须从 Parent—Patch—Champion 事实链核验机制演进

- **问题**：仅依赖 Agent 叙述，容易把已拒绝机制误认为 Parent 既有机制，或把组合修改错误归因为
  单一机制。
- **解决**：Harness 生成写一次的 `report-facts.json`，从最终 Champion 和每轮 Patch 重建
  Parent/Candidate，核对源码与哈希，并用 AST 保守描述结构变化。事实链优先于叙述；组合或不透明修改
  不作单机制归因，代码变化也不作为指标改善的因果证明。
- **验证**：覆盖接受、拒绝、首次晋级、Patch 缺失或损坏、哈希不符、组合变化及冻结附件重试。
- **风险**：结构化差异刻意保守，可能把相关修改归为组合变化，但不会把未经证据支持的因果解释写成
  事实。该机制不改变评测、Gate、Promotion 或停止条件。

#### Round 内被放弃的 Development 尝试必须进入研究记忆

- **问题**：轮末自由文本通常只描述最终候选，已实际评估但放弃的方向会丢失，后续研究可能重复试错。
- **解决**：允许同轮调整研究方向。Agent 先冻结 checkpoint，再通过 Harness-owned Attempt
  接口运行 Development；Harness 按候选哈希去重并冻结假设、指标和时间，以最终提交哈希标记
  `submitted`/`abandoned`。`learning` 只承担解释，不替代 Harness-owned 事实。
- **验证**：覆盖重复评估、放弃后提交、learning 缺失、超时 checkpoint 恢复及旧历史兼容。
- **风险**：Agent 仍可给出错误因果解释，因此 learning 仅作为研究叙述；Harness-owned 指标和
  Candidate 哈希才是审计事实。底层参数组合与 folds 不作为独立 Attempt，避免低质量信息膨胀。

### P0：曾阻断可信研发

#### 首个新增策略必须生成可重放的 Candidate Patch

- **问题**：`baseline.mode = "none"` 的首轮策略是 untracked 文件，旧
  `git diff` 不会记录它，导致候选虽能晋级，耐久 Patch 却无法重放。
- **解决**：使用隔离临时 index 生成包含新增文件的 binary patch；Decision 和晋级前，在冻结 Parent
  上重放并逐字节核对候选及 submission 哈希。首次晋级另存内容寻址的 `candidate.py` 冗余证据。
- **验证**：覆盖新增、修改、删除、空文件、二进制、真实空改动、哈希不符及重放失败，且不修改候选
  真实 index。
- **风险**：历史 Run 003 Round 001 的空 patch 无法追溯修补；当前任务级 Champion 仍存在时应另行
  冻结核对。新 Round 已具备可重放 patch 和首次晋级冗余源码。

#### 宿主固定评测环境必须进入 Champion 指标适用性契约

- **问题**：若 applicability 不含 Python、ABI、平台和依赖，不同宿主环境产生的指标可能被直接比较。
- **解决**：入口在状态变更前校验固定环境，并生成内容寻址、稳定且脱敏的环境 manifest；所有评测
  与报告引用其哈希。环境变化使旧指标 stale 并触发重评，活动 Run 禁止跨环境恢复。
- **验证**：覆盖错误环境前置失败、manifest 稳定性、环境变化、旧 schema 迁移、同环境缓存及跨环境
  恢复熔断。
- **风险**：`quant` 内包升级会主动使指标 stale 并触发重评；当前未增加跨平台 Conda lockfile，
  但每次实际评测环境均有不可歧义的耐久清单，不能静默复用异环境指标。

#### Provider 刷新令牌失效必须持久化并按基础设施错误熔断

- **问题**：隔离会话中的 refresh token 轮换若未回写，后续会继续使用已撤销凭据；若再被当作普通
  候选失败，会无意义地消耗轮次。
- **解决**：认证文件上的会话串行执行；Run/Round 分配前由受限宿主探针预检。会话结束后仅提取合法
  Provider 变化，经宿主复核后原子合并；认证失效、损坏或冲突统一归类为基础设施故障并熔断。
- **验证**：覆盖轮换、原子回写、成功/失败/超时/中断恢复、非法状态、并发冲突、脱敏和预检不分配
  研究轮次。
- **风险**：同一认证文件上的 Harness 会话会串行，锁等待计入会话硬时限；外部 OpenCode 进程不遵守
  Harness 锁时无法完全消除提交竞态，Harness 会检测已知同 Provider 冲突并拒绝覆盖。完整 refresh
  token 会进入一次性容器；这符合当前协作型 Agent 威胁模型，但不防御主动读取或替换凭据的恶意候选。

#### 容器运行时故障必须在 Run 前预检并按基础设施错误熔断

- **问题**：挂载、模型缓存或 CLI 契约故障若到 Agent 启动后才暴露，会连续消耗研究轮次。
- **解决**：使用权限受限的一次性 runtime home，并在创建耐久 Run 前用真实镜像验证挂载、读写边界、
  Research Root 遮蔽和模型可用性；运行时基础设施故障首次出现即熔断。
- **验证**：真实容器会话和隔离边界测试必须通过，容器初始化失败不得按普通候选失败重试。

#### 候选 Agent 曾可通过 Bash 访问 Research Root

- **问题**：worktree 和工具级目录限制无法阻止 Bash、Python 或符号链接访问宿主绝对路径，存在
  Gate 泄漏。
- **解决**：候选只在一次性容器中运行，仅挂载候选 worktree 和只读 Development 输入；Research
  Root、Gate runtime 和主工作区不挂载。Docker 不可用时失败，不回退宿主机。
- **验证**：真实容器测试覆盖 Bash、Python、绝对路径和符号链接，确认候选只能写自身目录。
- **风险**：不防御 Docker daemon、宿主内核或容器运行时本身被攻破；OpenCode 认证文件仍作为
  必要输入进入一次性 runtime home。

#### 搜索预算和耐久证据采用不同契约

- **问题**：Prompt 无法强制限制尝试次数；永久保留全部搜索轨迹又会放大工件，却不增强晋级证据。
- **解决**：以单调时钟强制单轮时间预算；截止后返回的成功不得进入评测。耐久证据只保留冻结输入、
  顶层 Attempt 最小事实、最终 diff、固定评测指标、Decision、Parent 和时长，内部参数与 fold 搜索
  轨迹保持 disposable。
- **验证**：覆盖超时终止、截止竞态、最小 Attempt 事实、成功日志压缩和失败原因保留。
- **风险**：不防御 Agent 主动通过 `setsid`、容器逃逸或其他系统权限规避进程组。若改为对抗型
  Agent，应升级系统沙箱，而不是恢复次数限制或宣称可核验全部内部探索。

#### 候选 Worktree 导入主工作区代码

- **问题**：editable install 可能让隔离评测导入主工作区代码，导致实际评测对象与声明候选不一致。
- **解决**：所有 Agent、测试和回测子进程都将候选 worktree 的 `src/` 放在 `PYTHONPATH` 首位；
  仅切换 `cwd` 不能覆盖 editable install 的导入映射。
- **验证**：真实子进程断言策略模块 `__file__` 位于候选路径；单元测试覆盖搜索路径顺序。

#### Gate 可行性优先于目标改善和 Development 表现

- **问题**：要求不可行 Champion 的替代者先改善目标会拒绝首个可行解；用 Development 改善替代
  Gate 证据则会接受隐藏区间违反约束的候选。
- **解决**：候选必须先通过全部 Gate 硬约束。Champion 不可行时，首个目标有限且满足约束的
  候选直接替换；Champion 可行后才要求相对目标改善。精确 Gate 指标不得反馈给后续研发轮次。
- **验证**：覆盖不可行/可行 Champion、Gate 约束失败和目标改善组合，拒绝候选不得污染 Champion。
- **风险**：Gate 通过/失败仍形成弱反馈；最终结果需要独立验证区间或前向观察。

### P1：重要可靠性问题

#### Evaluator 指纹不得写入主 Git 对象库

- **问题**：临时 `GIT_INDEX_FILE` 只能隔离 index；`git add` 和 `git write-tree` 仍会把新 blob/tree
  写入主 `.git/objects`，导致主 Git 元数据只读的受管环境在分配 Run 前失败。
- **解决**：指纹计算同时使用临时 index 和临时 object database，通过 alternate object directory
  只读复用仓库公共对象库。保留 Git 的工作树、文件模式、删除和路径语义，临时对象随计算结束清理。
- **验证**：主对象库只读时，tracked 修改、未跟踪新增和删除仍可稳定计算；主 index 和对象库内容
  均保持不变，工作树与冻结提交的指纹继续一致。
- **风险**：本修复只覆盖 Evaluator 指纹；候选快照和 worktree 管理仍按设计需要可写 Git 元数据。

#### Evaluator 契约必须显式声明固定评测输入

- **问题**：哈希整个工作树会让无关文档变化使 Champion 指标 stale；清单过窄又会漏掉真实评测依赖。
- **解决**：任务必须通过 `evaluation.contract.paths` 显式声明固定测试、回测、配置及其导入或读取
  的仓库输入。Harness 用临时 index 生成 canonical manifest；路径校验拒绝 editable 策略、runtime、
  重复、重叠、越界、缺失和空输入，并用静态导入审计补强 Python 间接依赖检查。
- **验证**：无关文件变化应保持哈希稳定，固定依赖的修改、删除和未跟踪新增应改变哈希；工作区与提交
  哈希一致且不修改用户 index。
- **风险**：任务使用非 Python 动态读取的新资源时，维护者仍须显式扩充清单；缺漏不会由静态导入
  审计发现，因此任务评测命令的资源变更必须同步评审 `evaluation.contract.paths`。

#### Agent 单次 Shell 时限与 Round 评测预算不一致

- **问题**：Shell 工具的隐式时限可能短于 Round 预算并截断合法评测；参数数量上限也不能代表实际
  运行时间。
- **解决**：显式令 Shell 时限与 Round 契约一致。Development 根据实测折耗时和剩余时间估算完整
  网格成本，预留 finalization 时间；预计超预算则提前拒绝，不静默裁剪。进度和终止原因原子落盘。
- **验证**：覆盖时限传递、checkpoint 真实性、短 Round 预留、超预算拒绝、耗时校准复用及超时事件。
- **风险**：耗时估算基于两个折的保守采样，极端非均匀策略仍可能在外层 Round 截止处被终止；
  `max_parameter_sets` 仍是结构上限，不能替代运行时估算。

#### Champion 指标值与适用性必须独立于 disposable runtime

- **问题**：若把 disposable runtime 缓存缺失等同于指标失效，正常清理会抹掉耐久 Champion 指标，
  还可能错误启动新 Round。
- **解决**：`champion.json` 升级为 schema v5，用 `champion_metrics_record` 同时保存指标值、评测来源、
  valid/stale 状态及策略、数据、环境和 evaluator 指纹。清理只影响缓存；适用性变化保留历史值但标记
  stale。晋级与目标停止只读取匹配的 valid 指标，pending promotion 原子切换策略和指标记录。
- **验证**：覆盖正常清理、预检失败、数据/evaluator 变化、旧 schema 迁移、重评、目标停止和晋级
  中断恢复。
- **风险**：旧 schema 缺少完整适用性证据，升级后的首次晋级比较必须重评 Champion；这是有意的保守
  行为，不能通过当前 runtime 反推旧指标有效。

#### Round 硬时限不能丢弃截止前已冻结的候选

- **问题**：Agent 在截止前可能已有可运行候选，但尚未来得及返回最终 JSON；直接判超时会丢失有效工作。
- **解决**：增加 Harness-owned checkpoint 协议。Agent 用显式命令提交策略和完整候选说明，Harness 在
  截止前校验并冻结到 Agent 不可写目录。正常完成以最终提交为准；超时才恢复最近有效 checkpoint 并
  执行完整固定评测。基础设施失败不恢复，正式测试失败也不回退更早版本。
- **验证**：覆盖多 checkpoint、冻结副本损坏、无 checkpoint、最终提交优先、截止后提交拒绝及恢复后
  测试失败/晋级。
- **风险**：checkpoint 接收使用协作型 Agent 信任模型；冻结边界防止确认后的版本被回写，但不防御
  主动攻击 Harness 控制协议的恶意候选。正式评测仍不计入 Agent 研发时限，并受原有命令超时约束。

#### Docker bind 可见性故障必须在 Round 分配前诊断

- **问题**：删除并重建候选父目录后，Docker Desktop 可能暂时仍认为真实存在的 bind source 不存在；
  单次重试既不足以覆盖恢复场景，也容易在 Agent 启动后才失败并覆盖首次证据。
- **解决**：活动 Run 保持候选父目录 inode 稳定。候选先暂存，再用不启动 Agent 的只读 bind 探针
  有界退避重试；仅在宿主路径存在且身份稳定、Docker 明确报告 source 不存在时重试。每次尝试保留独立
  诊断，持续失败在分配 Round 前熔断。
- **验证**：覆盖瞬态恢复、持续不可见、宿主路径消失或身份变化、失败不分配 Round、独立日志及中断
  清理后的 inode 稳定。
- **风险**：超过探针预算的 daemon 故障仍会停止 Run，但不会消耗研究轮次；有界重试避免无限阻塞，
  且必须保证 Agent 尚未启动以免产生重复副作用。

#### Run 边界及状态—工件一致性必须由 Harness 强制

- **问题**：Run 间工件混用、无工件的“幽灵 Round”或编号复用都会使状态、计数和报告互相矛盾。
- **解决**：Champion 保持任务级，Round、状态和报告按 Run 隔离；恢复未完成 Round 时补写失败工件，
  编号同时参考状态和物理目录。Decision 前校验计数与 ID，报告前确定性检查必需工件；异常只记录
  `integrity_warnings`，禁止模型补全事实。
- **验证**：回归测试覆盖连续 Run 隔离、缺目录恢复、编号不复用、计数一致性和损坏状态告警。
- **风险**：完整性检查只能暴露既有损坏，不能自动推断或修复缺失历史；失败是否占预算仍由
  “中断或基础设施失败仍会消耗研发轮次”跟踪。
