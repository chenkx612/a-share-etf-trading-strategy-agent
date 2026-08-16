# Quant Agent 简历素材与面试参考

> 用途：为简历、项目介绍和面试复盘提供可核验素材，不替代项目操作文档。
> 运行方式以 [README](../README.md) 为准，工程风险与修复记录以
> [ISSUES](../ISSUES.md) 为准。

## 1. 项目一句话

Quant Agent 是一个面向自动化量化策略研发的 Agent Loop / Harness 平台：
Agent 负责提出假设和修改单个候选策略，确定性的 Python Harness 负责隔离环境、
固定测试与回测、Development/Gate/Guard 数据边界、客观晋级、预算控制、失败恢复和证据留存。

适合在简历中归类为：

- 自动化量化研究平台
- AI Agent 工程 / LLM 工具链
- Python 数据与回测基础设施
- 可恢复、可审计的长任务编排系统

## 2. 推荐简历写法

以下默认按“负责核心实现”撰写。请根据真实参与范围，把“设计并实现”调整为
“主导”“参与”或“独立完成”，不要仅依据仓库现状推断个人职责。

### 2.1 项目名称与技术栈

**Quant Agent｜自动化量化策略研发 Harness**  
Python、pandas、NumPy、PyArrow、pytest、Docker、Git Worktree、Conda、AkShare

### 2.2 三至五条核心描述

- 设计并实现 Agent 驱动的量化策略研发闭环，将版本化任务契约、候选策略生成、固定测试、
  Walk-Forward 回测、样本外 Gate/Guard、Champion 晋级和终局报告串成可重复运行的工作流；
  模型只提出候选，评分与晋级完全由确定性 Harness 控制。
- 基于 Git Worktree 与一次性 Docker 容器隔离每轮候选，仅暴露冻结的 Development 数据并只读挂载
  固定输入；Gate runtime、Research Root 与主工作区不进入候选容器，降低数据泄漏和候选污染风险。
- 以 TOML、版本化 JSON 契约、内容哈希和原子写入管理任务、输入快照、环境、状态、指标、
  Patch 与决策；实现硬时限、心跳、Checkpoint、Attempt 去重、重试和中断恢复，支持数小时研究任务。
- 建立 Harness-owned 晋级规则：候选须先通过测试、硬约束和样本外评测，再按最小改善门槛与当前
  Champion 比较；可选 Guard 用后置区间检查超额收益退化，精确隐藏指标不会反馈给后续研究轮次。
- 当前仓库快照已实跑 16 个 Run、94 个 Round，覆盖 17 次接受、54 次拒绝和 23 次失败；完整测试在
  Conda `quant` 环境下达到 399 passed、5 skipped，验证正常、拒绝、超时、中断及基础设施故障路径。

### 2.3 适合平台 / 后端岗位的替换描述

- 将长时间 Agent 任务建模为持久化状态机，区分 accepted、rejected、failed、interrupted 和
  infrastructure failure，按轮数、总时长、连续失败与目标达成条件确定性停止，并支持原 Run 恢复。
- 设计内容寻址的输入与环境适用性契约；Python/ABI/平台/Conda 包或固定 evaluator 输入发生变化时，
  历史 Champion 指标自动标记 stale 并在比较前重评，避免跨环境静默复用结果。
- 用 Parent—Patch—Candidate—Champion 事实链重放并核验策略版本，终局报告只能基于冻结输入和
  已验证事实生成；报告 Agent 失败时仍保留最小人类可读报告。

### 2.4 适合量化研发岗位的替换描述

- 实现严格因果的信号与回测语义：交易日 t 收盘后生成目标权重，t+1 开盘按资金、费率和整手约束成交，
  输出年化收益、波动率、Sharpe、Sortino、最大回撤和平均换手率等统一指标。
- 支持固定区间与滚动 Walk-Forward 评测；训练窗内选择参数，随后不重叠验证折只做样本外评估，
  Development、Gate 与可选 Guard 区间在任务契约中显式分离。
- 打通 ETF Universe、行情更新、因子计算、策略选择、确定性回测、参数冻结、因果历史重放与逐日推荐，
  并在输出中明确标注当前成分股快照带来的幸存者偏差。

### 2.5 适合 AI Agent / LLM 工程岗位的替换描述

- 将开放式模型探索限制在“单轮、单候选、单可编辑策略文件”的受控边界内，通过只读 evaluator 门面、
  宿主 Attempt Receiver 和固定外层评测阻止模型绕过 Checkpoint、预算或晋级逻辑。
- 为模型注入脱敏研究记忆，只返回假设、开发集结果和统一拒绝原因，不暴露精确 Gate/Guard 指标；
  在保留可学习反馈的同时约束评测泄漏。
- 对 Provider 认证、容器挂载与候选工作区执行前置探针，将认证、容器和固定基线故障归类为基础设施问题，
  防止错误消耗研究失败预算或把平台故障归因于策略候选。

## 3. 30 秒项目介绍

> 我做的不是单个量化策略，而是一套自动化策略研发 Harness。模型每轮只能在隔离工作区里修改指定策略，
> Python Harness 掌握固定测试、Walk-Forward 回测、样本外 Gate、晋级规则和停止预算。系统会冻结数据、
> 环境、代码差异、指标与决策，失败或中断后可以恢复，并能证明最终 Champion 是由哪一轮、哪份 Patch
> 演进而来。它把一次性的“让模型帮我调策略”变成可重复、可审计的研究流程。

## 4. 项目要解决的问题

普通的 Agent 量化研究容易出现五类问题：

1. 模型既写策略又评价自己，评分和晋级缺少独立性。
2. 研发阶段看到隐藏区间结果，后续候选会逐渐拟合 Gate。
3. 候选直接修改当前分支、评测器或数据，失败轮会污染后续实验。
4. 数小时任务遭遇超时、Provider、容器或进程中断后无法可靠续跑。
5. 最终只剩一条更好的回测曲线，无法解释输入、代码、指标和决策之间的证据链。

Quant Agent 的核心价值，是让确定性 Harness 而不是模型拥有数据边界、评分、晋级、预算与持久化状态。

## 5. 核心架构与职责边界

| 层 | 主要职责 | 关键实现 |
| --- | --- | --- |
| Task Contract | 声明目标、Universe、区间、约束、预算、可编辑范围 | TOML 校验、固定策略接口、任务指纹 |
| Loop Controller | 分配 Run/Round、预算与停止、恢复状态 | 持久化状态机、原子写、心跳、前置检查 |
| Candidate Runtime | 让 Agent 实现一个候选 | 独立 Worktree、一次性 Docker、只读输入、硬 deadline |
| Fixed Evaluator | 测试、参数选择、回测和指标 | 固定 Python 代码、Walk-Forward、因果执行语义 |
| Promotion | 接受或拒绝候选 | 硬约束、最小目标改善、可选 Guard、Champion 原子切换 |
| Evidence & Memory | 保存可复现研究证据 | 输入/环境哈希、Checkpoint、Attempt、Patch、Result、Decision |
| Report & Production | 终局复盘与受控同步 | 事实链重放、最小降级报告、冲突检测、参数冻结、逐日推荐 |

最重要的职责分离是：

```text
Agent：提出假设 → 修改候选 → 提交 Checkpoint / Attempt
Harness：固定评测 → 判断约束 → 接受或拒绝 → 持久化证据 → 决定是否继续
```

## 6. 可重点讲的工程难点

### 6.1 防止 Gate 泄漏与评测绕过

**问题**：工具级目录限制挡不住 Bash、Python、绝对路径或模块导入；只在 Prompt 中要求“不要看 Gate”
不是可信边界。

**实现**：

- 候选 Agent 只在一次性 Docker 容器中运行，主工作区、Research Root 和 Gate runtime 不挂载。
- Development 输入由 Harness 严格解析、按截止日裁剪并生成内容寻址快照；未知格式、坏 Schema、符号链接
  或缺少日期字段时 fail closed。
- 固定 evaluator 在候选容器中被 OS 级只读门面覆盖，权威顶层 Walk-Forward 实现只在宿主 Receiver 执行。
- 后续 Prompt 只接收脱敏反馈，不接收精确 Gate/Guard 指标、逐折参数或持仓。

**面试价值**：说明安全边界不能只靠提示词或 Python 全局变量，需要把权限放到进程、挂载和宿主控制面。

### 6.2 长任务预算、超时与恢复一致性

**问题**：Agent 可能在截止前已写出可运行策略，但还未来得及返回最终 JSON；直接杀掉会丢失有效工作，
无限等待又会破坏预算。

**实现**：

- 用单调时钟维护 Round 硬时限，并给最终固定评测预留时间。
- Agent 可在截止前提交内容哈希一致的 Checkpoint；超时时只恢复最近有效快照。
- 每个顶层 Development Attempt 由 Harness 按候选哈希去重并记录事实，解释性 learning 与权威指标分离。
- Run 状态、累计时间、当前轮、计数和停止原因持久化；恢复时同时检查状态与物理工件，避免幽灵 Round。
- Agent 执行阶段定期输出不含模型内容和评测指标的心跳，方便外部监督器区分运行中与停滞。

**面试价值**：可类比工作流引擎中的 deadline、checkpoint、幂等、恢复和 durable state。

### 6.3 结果的可复现性与适用性

**问题**：同一策略若在不同 Python、依赖、数据或交易日边界下运行，指标不能直接比较；缓存清理也不应
等同于耐久指标失效。

**实现**：

- 冻结 Universe、Development/Evaluation 输入和相对区间解析结果，并记录行数、Schema、日期范围、
  文件大小与哈希。
- 对 Python、ABI、平台、Conda build、distribution 和 evaluator contract 生成稳定环境指纹。
- Champion 指标值与 applicability 独立存储；输入或环境变化时保留历史值但标记 stale，比较前强制重评。
- 历史 Walk-Forward 边界只由冻结行情日期推导，不依赖实时网络交易日历。

**面试价值**：体现“回测可复现”不仅是随机种子，还包括数据、代码、环境、区间和执行语义的完整契约。

### 6.4 防止报告把相关性写成因果性

**问题**：报告模型可能把被拒绝的机制写进最终 Champion，或把组合修改错误归因于一个单独因素。

**实现**：

- 从最终 Champion 反向重放 Accepted Patch，再正向核验每轮 Parent 与 Candidate 的内容哈希。
- 用 AST 生成保守的结构差异事实；组合或不透明变更不自动归因。
- 叙述只消费冻结的 report input 与 report facts；报告失败时生成最小事实入口，不影响固定 Decision。

**面试价值**：说明生成式报告属于非权威表现层，事实层和决策层必须可独立验证。

## 7. 当前可核验规模

以下为 **2026-08-15 当前工作区快照**，适合证明系统规模与实际运行情况；仓库继续变化后应重新统计。

| 指标 | 当前值 | 说明 |
| --- | ---: | --- |
| Python 源码 | 56 个文件 / 17,375 行 | `src/quant_core/` |
| Research Harness | 27 个模块 / 11,590 行 | 不含普通策略与数据模块 |
| 测试代码 | 20 个文件 / 12,511 行 | pytest + pandas 内存 fixture 为主 |
| 当前完整测试 | 399 passed / 5 skipped | Conda `quant`，耗时 43.18 秒 |
| 托管任务配置 | 4 个 | `tasks/*.toml` |
| 本地历史 Run | 16 个，均已停止 | 4 个任务目录 |
| 本地历史 Round | 94 个 | 17 accepted / 54 rejected / 23 failed |
| Harness 累计记录时长 | 约 31.53 小时 | 各 Run `elapsed_seconds` 求和 |
| 核心证据工件 | 94 Result / 93 Decision / 71 Patch | 失败或中断轮不保证产生 Candidate Patch |
| 开放工程风险 | P0/P1/P2 均为 0 | 以当前 `ISSUES.md` 为准 |

这里最值得写进简历的不是“接受了多少个策略”，而是系统确实经历并记录了接受、拒绝、候选失败、
基础设施失败、预算耗尽和中断等不同路径，验证了 Harness 的状态与证据模型。

## 8. 技术选型可以怎样解释

- **Python + pandas / NumPy**：策略、横截面计算与确定性回测易表达，适合用内存 DataFrame 构造测试。
- **PyArrow / Parquet**：面向本地研究数据的列式存储与高效交换。
- **TOML + JSON**：TOML 适合人工维护研究任务；JSON 适合版本化状态、指标和工件互操作。
- **Git Worktree**：低成本构造继承 Parent 的候选代码空间，并能生成可重放 Patch。
- **Docker**：提供候选进程与宿主 Research Root/Gate 数据之间的 OS 级边界。
- **Conda 环境指纹**：既满足本地量化依赖管理，也能把真实评测环境纳入指标适用性判断。
- **pytest**：覆盖纯函数、契约、恢复与失败分支；真实网络和模型调用由 fake runner 隔离。
- **AkShare**：提供 ETF 行情接入；数据层标准化字段并支持缓存与备用数据源路径。

## 9. 面试常见追问与回答要点

### 为什么不让 Agent 自己跑完回测后决定是否接受？

因为候选能修改的代码、可见的数据和评分权必须分离。模型输出只是一份不可信候选；Harness 在候选容器外
执行固定测试、Gate、Guard 和 Decision，避免模型通过改指标、改区间或选择性汇报来晋级。

### Development、Gate、Guard 各自是什么？

- Development：候选可见，用于假设验证和调试。
- Gate：与 Development 不重叠，由 Harness 隐藏执行，决定是否满足约束和晋级标准。
- Guard：Gate 通过后才查询的后置区间，用于限制相对基准的超额收益退化。

Guard 的精确结果也不反馈给后续候选，只提供统一拒绝原因，减少反复试探隐藏集。

### Walk-Forward 如何避免未来函数？

在每个调参边界仅使用此前训练窗选择参数，将参数冻结到随后不重叠验证折；信号在 t 收盘后形成，固定
回测器在 t+1 开盘执行。相对区间、行情输入与历史边界均在 Run 启动时冻结。

### 候选失败会不会污染下一轮？

不会直接修改用户当前分支。每轮候选位于独立 Worktree 和容器；被拒绝、失败或中断的代码不晋级。
只有通过固定 Decision 的候选才会原子更新任务级 Champion。

### 如何处理进程超时？

Round 使用单调时钟硬截止。截止前 Agent 可提交 Checkpoint；超时时 Harness 只恢复最后一个合法、哈希一致
且在截止前冻结的候选，再执行完整固定测试和评测。若是基础设施故障则不把旧 Checkpoint 冒充正常提交。

### 为什么需要环境指纹？

策略指标依赖 Python ABI、平台、依赖版本、数据和 evaluator 代码。若其中任一变化仍复用旧指标，候选比较
就不再同口径。环境指纹变化会令旧指标 stale，而不是静默视为有效或直接删除历史值。

### 接受规则有哪些细节？

候选必须先满足全部硬约束。若当前 Champion 不可行，首个可行候选可以直接替换；Champion 已可行后，
候选还必须达到配置的绝对目标改善点数。模型没有修改规则或跳过 Gate 的权限。

### 这个项目目前最大的边界是什么？

它是研究与推荐平台，不是自动下单或实盘交易系统；当前 Universe 是现时成分快照，历史重放仍有幸存者
偏差；回测与 Gate 结果不等于未来收益，仍需独立验证或前向观察。

## 10. 不建议写进简历的表述

| 不建议 | 更准确的写法 |
| --- | --- |
| “AI 自动找到高收益策略” | “Agent 生成候选，固定 Harness 以样本外约束和客观门槛决定晋级” |
| “彻底杜绝过拟合/未来函数” | “通过数据隔离、因果时序与 Walk-Forward 降低泄漏和过拟合风险” |
| “实现全自动实盘交易” | “实现研究闭环、Champion 同步与逐日推荐；未接入自动下单” |
| “策略年化收益达到 X%” | 若确需使用，必须写成“特定 Gate 区间回测”，并同时给区间、回撤、换手和局限 |
| “系统零故障” | “显式建模并验证候选失败、基础设施失败、超时、中断与恢复路径” |
| “消除幸存者偏差” | “输出中显式披露当前成分股快照带来的幸存者偏差” |

## 11. 提交简历前仍需本人补充

仓库无法证明下列个人或业务事实，必须按实际情况补齐：

- 项目起止时间、投入时长和团队规模。
- 你的实际角色、独立完成比例、协作对象和负责模块。
- 是否在个人设备、研究团队或生产环境中被真实使用。
- 相比原人工流程节省了多少时间、成本或重复劳动，以及统计口径。
- 单轮/单 Run 的平均耗时、模型调用成本和资源峰值。
- 是否存在真实前向观察或实盘记录；若没有，不要把回测写成实盘业绩。
- 是否可公开仓库、代码截图、架构图和具体策略指标。

可量化后再补充的句式：

> 将单次策略实验从人工执行的 `[原耗时]` 缩短为 Harness 自动运行的 `[现耗时]`，
> 人工仅处理 `[异常/最终评审]`；在 `[N]` 个任务、`[N]` 轮实验中保持统一评测契约和可重放证据。

## 12. 证据索引

| 简历主张 | 主要代码或工件证据 |
| --- | --- |
| Loop、预算、恢复和停止 | [`research/loop.py`](../src/quant_core/research/loop.py)、[`research/loop_state.py`](../src/quant_core/research/loop_state.py) |
| 容器、Worktree、固定测试和托管单轮 | [`research/runner.py`](../src/quant_core/research/runner.py)、[`research/workspace.py`](../src/quant_core/research/workspace.py) |
| Walk-Forward 与固定评测 | [`research/evaluator.py`](../src/quant_core/research/evaluator.py)、[`research/periods.py`](../src/quant_core/research/periods.py) |
| Checkpoint 与 Attempt | [`research/checkpoint.py`](../src/quant_core/research/checkpoint.py)、[`research/attempt.py`](../src/quant_core/research/attempt.py) |
| 晋级与 Guard | [`research/decision.py`](../src/quant_core/research/decision.py)、[`research/guard.py`](../src/quant_core/research/guard.py) |
| 原子状态与环境指纹 | [`research/storage.py`](../src/quant_core/research/storage.py)、[`research/environment.py`](../src/quant_core/research/environment.py) |
| 事实链与终局报告 | [`research/report_facts.py`](../src/quant_core/research/report_facts.py)、[`research/report.py`](../src/quant_core/research/report.py) |
| Champion 到生产策略的受控同步 | [`research/production_sync.py`](../src/quant_core/research/production_sync.py) |
| 因果回测与指标 | [`backtest/engine.py`](../src/quant_core/backtest/engine.py) |
| 参数冻结、因果重放与推荐 | [`recommendation/`](../src/quant_core/recommendation) |
| 任务契约示例 | [`tasks/`](../tasks) |
| 正常与异常路径测试 | [`tests/`](../tests) |

本地 `.research/` 中的 Run、Round、Result、Decision、Patch、报告和 Champion 元数据是“已实际运行”的
直接证据，但该目录属于受管研究工件，不应为了简历展示而移动、改写或复制到普通框架输出中。

## 13. 一页简历的最终精简版

**Quant Agent｜自动化量化策略研发平台**  
`Python / pandas / NumPy / Docker / Git Worktree / pytest / Conda / AkShare`

- 构建 Agent 驱动、Harness 裁决的策略研发闭环，覆盖任务契约、候选生成、固定测试、Walk-Forward
  回测、样本外 Gate/Guard、Champion 晋级、预算停止与终局复盘。
- 通过独立 Worktree、一次性容器、冻结 Development 视图和宿主固定 evaluator 隔离候选、主分支与隐藏
  评测数据；以内容哈希、原子状态、Checkpoint 和可重放 Patch 保证恢复与审计。
- 建立硬约束优先、最小目标改善和环境适用性重评机制；当前快照累计实跑 16 个 Run / 94 个 Round，
  覆盖接受、拒绝、超时、中断和基础设施失败等路径。
- 完整测试在 Conda `quant` 环境下通过 399 项、跳过 5 项；平台同时支持行情缓存、因果回测、参数冻结、
  Champion 受控同步和逐日 ETF 推荐，不包含自动下单。
